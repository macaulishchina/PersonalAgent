"""Authenticated Pi transport; accounting and policy belong to ai_usage."""

import asyncio
from dataclasses import replace
import json

from aiohttp import ClientSession, ClientTimeout, web

from assistant_runtime.api.ai_usage import UsageError, UsageScope


class ExecutionTransport:
    def __init__(self, client):
        self.client = client
        self.runner = self.transport = None
        self.url = None
        self.pending = 0
        self.slots = asyncio.Semaphore(client.settings.concurrency)

    async def start(self):
        self.transport = ClientSession(
            timeout=ClientTimeout(total=self.client.settings.timeout_seconds), trust_env=False
        )
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post("/v1/responses", self.forward)
        app.router.add_post("/v1/native", self.forward)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"

    async def forward(self, request):
        from hmac import compare_digest

        key = self.client.environment.get("ASSISTANT_MODEL_GATEWAY_KEY", "")
        if not key or not compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
            raise web.HTTPUnauthorized()
        worker = self.client.workers.get(request.headers.get("x-assistant-session"))
        if (
            not worker
            or not (worker.meta.get("active_run") or worker.compacting)
            or not worker.meta.get("budget_id")
        ):
            raise web.HTTPForbidden()
        if self.pending >= self.client.settings.queue_size + self.client.settings.concurrency:
            raise web.HTTPServiceUnavailable()
        self.pending += 1
        try:
            async with self.slots:
                if request.path.endswith("/native"):
                    return await self._native(request, worker)
                return await self._forward(request, worker)
        finally:
            self.pending -= 1

    async def _forward(self, request, worker):
        if (not worker.active_turn and not worker.compacting) or request.transport.is_closing():
            raise web.HTTPConflict()
        ident = worker.meta["budget_id"]
        payload = await request.json()
        if payload.get("model") != worker.meta["model"]:
            raise web.HTTPBadRequest()
        entry = self.client.settings.entry(worker.meta["model"])
        maximum = entry.get("execution_max_output_tokens", self.client.settings.execution_max_output_tokens)
        requested = payload.get("max_output_tokens", maximum)
        if type(requested) is not int or requested < 1:
            raise web.HTTPBadRequest()
        payload["max_output_tokens"] = min(requested, maximum)
        try:
            scope = await self.client.usage.binding(worker.meta.get("budget_scope") or ident) or UsageScope(
                ident, worker.meta.get("budget_scope") or "foreground"
            )
            if (await self.client.usage.snapshot(ident)).get("unknown_execution_requests"):
                raise UsageError("usage_execution_unresolved", ident)
            ticket = await self.client.usage.admit(
                scope,
                service="execution",
                provider=entry["provider"],
                model=entry["model"],
                compaction=worker.compacting,
                bounds={
                    "input_tokens": entry.get("context_window", 32768),
                    "output_tokens": payload["max_output_tokens"],
                },
                body=payload,
                details={
                    "model_alias": worker.meta["model"],
                    "reasoning_effort": worker.meta["reasoning_effort"],
                    "max_output_tokens": payload["max_output_tokens"],
                    "input_bytes": request.content_length or 0,
                },
            )
        except UsageError as exc:
            worker.usage_error = exc.code
            return web.json_response({"error": {"message": "AI usage stopped: " + exc.code}}, status=400)
        usage = completed_body = None
        confirmed = settled = False
        response = None
        total_bytes = 0
        http_status = None
        provider_request_id = None

        async def finish():
            nonlocal settled
            await self.client.usage.settle(
                ticket["id"],
                state="completed"
                if confirmed and http_status == 200
                else "rejected"
                if confirmed
                else "unknown",
                usage=usage,
                http_status=http_status,
                provider_request_id=provider_request_id,
                response_body=completed_body,
            )
            settled = True

        try:
            headers = {
                "Authorization": request.headers["Authorization"],
                "x-assistant-session": worker.execution_session_id,
            }
            async with self.transport.post(
                self.client.settings.gateway_url + "/responses",
                json=payload,
                headers=headers,
                allow_redirects=False,
                timeout=ClientTimeout(
                    total=(
                        self.client.settings.compaction_timeout_seconds
                        if worker.compacting
                        else self.client.settings.timeout_seconds
                    )
                ),
            ) as upstream:
                worker.last_usage_call = ticket["id"]
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={"Content-Type": upstream.headers.get("Content-Type", "application/json")},
                )
                http_status = upstream.status
                provider_request_id = upstream.headers.get("x-request-id")
                await response.prepare(request)
                if upstream.status != 200:
                    confirmed = True  # Authoritative rejection; no automatic retry here.
                buffer = b""
                async for chunk in upstream.content.iter_chunked(8192):
                    total_bytes += len(chunk)
                    if total_bytes > 8 * 1024 * 1024:
                        raise ValueError("Model response exceeds bound")
                    buffer += chunk
                    if len(buffer) > 2 * 1024 * 1024:
                        raise ValueError("Model response frame exceeds bound")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.startswith(b"data:"):
                            try:
                                event = json.loads(line[5:])
                            except ValueError:
                                continue
                            if event.get("type") == "response.completed":
                                completed_body = event.get("response", {})
                                usage = completed_body.get("usage")
                                confirmed = True
                                if not isinstance(usage, dict):
                                    usage = None
                                # The next native tool/turn cannot overtake its usage receipt.
                                if not settled:
                                    await finish()
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (OSError, TimeoutError, ValueError):
            if response is None:
                return web.json_response(
                    {"error": {"message": "Execution transport outcome unknown"}}, status=502
                )
            raise
        finally:
            if not settled:
                await finish()

    def settings(self):
        return replace(self.client.settings, gateway_url=self.url)

    async def close(self):
        if self.client.accounts:
            await self.client.accounts.close()
        if self.transport:
            await self.transport.close()
        if self.runner:
            await self.runner.cleanup()

    async def _native(self, request, worker):
        from assistant_runtime.core.pi.native_transport import forward

        return await forward(self, request, worker)
