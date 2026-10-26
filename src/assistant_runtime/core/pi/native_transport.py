"""Meter native Pi model streaming under the same host-bound execution identity."""

import asyncio
import logging
import time
import json
from contextlib import aclosing
from aiohttp import web

from assistant_runtime.api.ai_usage import UsageError, UsageScope


def telemetry(message):
    usage = message.get("usage")
    if not isinstance(usage, dict) or not any(
        isinstance(usage.get(key), (int, float)) and usage[key] > 0
        for key in ("input", "output", "cacheRead", "cacheWrite")
    ):
        return None
    return {
        "input_tokens": usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0),
        "output_tokens": usage.get("output", 0),
        "cached_input_tokens": usage.get("cacheRead", 0),
    }


async def forward(transport, request, worker):
    client = transport.client
    if (not worker.active_turn and not worker.compacting) or request.transport.is_closing():
        raise web.HTTPConflict()
    payload = await request.json()
    if not isinstance(payload, dict) or set(payload) - {"model", "context", "effort"}:
        raise web.HTTPBadRequest()
    alias = worker.meta["model"]
    if worker.meta["reasoning_effort"] == "auto" and payload.get("effort") == "none":
        payload["effort"] = "auto"
    if payload.get("model") != alias or payload.get("effort") != worker.meta["reasoning_effort"]:
        raise web.HTTPBadRequest()
    entry = client.settings.entry(alias)
    if not client.accounts:
        raise web.HTTPBadRequest()
    context = payload.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("messages"), list):
        raise web.HTTPBadRequest()
    images = any(
        part.get("type") == "image"
        for message in context["messages"]
        if isinstance(message, dict)
        for part in message.get("content", [])
        if isinstance(part, dict)
    )
    if images and not entry.get("images"):
        raise web.HTTPBadRequest()
    account, args, native_bound = await client.accounts.route(alias, payload["effort"], images=images)
    maximum = entry.get("execution_max_output_tokens", client.settings.execution_max_output_tokens)
    ident = worker.meta["budget_id"]
    try:
        scope = await client.usage.binding(worker.meta.get("budget_scope") or ident) or UsageScope(
            ident, worker.meta.get("budget_scope") or "foreground"
        )
        if (await client.usage.snapshot(ident)).get("unknown_execution_requests"):
            raise UsageError("usage_execution_unresolved", ident)
        ticket = await client.usage.admit(
            scope,
            service="execution",
            provider=entry["provider"],
            model=entry["model"],
            compaction=worker.compacting,
            bounds={
                "input_tokens": entry.get("context_window", 32768),
                "output_tokens": native_bound or maximum,
            },
            body=payload,
            details={
                "model_alias": alias,
                "reasoning_effort": payload["effort"],
                "max_output_tokens": native_bound or maximum,
            },
        )
    except UsageError as exc:
        worker.usage_error = exc.code
        return web.json_response({"error": {"message": "AI usage stopped: " + exc.code}}, status=400)
    timeout = (
        client.settings.compaction_timeout_seconds if worker.compacting else client.settings.timeout_seconds
    )
    state, usage, status, request_id, settled = "not_sent", None, None, None, False
    response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})

    async def finish():
        nonlocal settled
        await client.usage.settle(
            ticket["id"], state=state, usage=usage, http_status=status, provider_request_id=request_id
        )
        settled = True

    worker.last_usage_call = ticket["id"]
    started = time.monotonic()
    try:
        await response.prepare(request)
        args.update(
            context=payload["context"],
            max_tokens=maximum,
            timeout_ms=timeout * 1000,
            session_id=worker.execution_session_id,
        )
        async with asyncio.timeout(timeout):
            async with aclosing(account.events("stream", args)) as events:
                async for item in events:
                    event = item.get("event")
                    if not event:
                        continue
                    if event["type"] == "dispatch":
                        state = "unknown"
                    elif event["type"] == "response":
                        status, request_id = event["status"], event.get("request_id")
                    elif event["type"] == "done":
                        # Length-limited generation is observed but is not a completed business operation.
                        state, usage = "completed", telemetry(event["message"])
                        request_id = request_id or event["message"].get("responseId")
                        logging.getLogger(__name__).info(
                            "native_model_timing call=%s model=%s input_tokens=%s cached_input_tokens=%s timings=%s",
                            ticket["id"],
                            alias,
                            (usage or {}).get("input_tokens"),
                            (usage or {}).get("cached_input_tokens"),
                            event.get("transport_timings", {}),
                        )
                        await finish()
                    elif event["type"] == "error":
                        status = event.get("http_status", status)
                        state = "rejected" if status and status != 200 else state
                        await finish()
                    await response.write((json.dumps(event) + "\n").encode())
        await response.write_eof()
        return response
    except TimeoutError:
        logging.getLogger(__name__).warning(
            "model_timeout layer=native compaction=%s timeout_seconds=%s elapsed_seconds=%.3f",
            worker.compacting,
            timeout,
            time.monotonic() - started,
        )
        raise
    finally:
        if not settled:
            await finish()
