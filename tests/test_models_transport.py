"""Real HTTP transport tests; no external endpoints or paid requests."""

from usage_fixture import attach_usage

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from aiohttp import web
from assistant_runtime.plugins.models.client import GatewayClient, timing_header
from assistant_runtime.plugins.models.settings import ModelSettings
from assistant_runtime.plugins.models.plugin import ModelsPlugin
from assistant_runtime.kernel.models import ModelRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.store import Store
from test_models import settings


class TransportTest(unittest.IsolatedAsyncioTestCase):
    def test_optional_gateway_timing_cannot_invent_or_corrupt_receipts(self):
        for value in (None, "None", "NaN", "inf", "-1", "invalid"):
            self.assertIsNone(timing_header({"timing": value}, "timing"))
        self.assertIsNone(timing_header({}, "timing"))
        self.assertEqual(timing_header({"timing": "3.25"}, "timing"), 3.25)

    async def asyncSetUp(self):
        self.mode, self.requests, self.headers = "sse", [], []
        self.gate = asyncio.Event()
        self.inflight = self.peak = 0
        app = web.Application()
        app.router.add_post("/v1/responses", self.handle)
        app.router.add_get("/v1/models", lambda _: web.json_response({"data": []}))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = GatewayClient(ModelSettings(gateway_url=f"http://127.0.0.1:{port}/v1"), "private-key")
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def handle(self, request):
        self.requests.append(await request.json())
        self.headers.append(dict(request.headers))
        self.assertEqual(request.headers["Authorization"], "Bearer private-key")
        if self.mode == "error":
            return web.Response(status=429, text="secret response body")
        if self.mode.startswith("http-"):
            return web.Response(status=int(self.mode[5:]), text="private upstream detail")
        if self.mode == "slow":
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            try:
                await self.gate.wait()
            finally:
                self.inflight -= 1
        if self.mode == "oversized":
            return web.Response(body=b" " * (2 * 1024 * 1024 + 1), content_type="application/json")
        if self.mode == "redirect":
            raise web.HTTPTemporaryRedirect("/leak")
        output = [{"type": "message", "content": [{"type": "output_text", "text": "合成结果"}]}]
        if self.mode == "large-sse":
            output[0]["content"][0]["text"] = "合" * 30000
        if self.mode == "tool":
            output = [{"type": "function_call", "name": "shell"}]
        result = {"status": "completed", "id": "upstream-id", "output": output, "usage": {"output_tokens": 8}}
        if self.mode == "json":
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            raw = json.dumps(result).encode()
            for i in range(0, len(raw), 3):
                await response.write(raw[i : i + 3])
            await response.write_eof()
            return response
        events = [{"type": "response.output_text.delta", "delta": "合成结果"}]
        if self.mode == "incomplete":
            events.append({"type": "response.incomplete"})
        elif self.mode != "truncated":
            events.append({"type": "response.completed", "response": result})
        body = b"".join(b"event: message\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)
        return web.Response(body=body, content_type="text/event-stream")

    async def test_sse_completion_and_chunked_json(self):
        for mode in ("sse", "json"):
            self.mode = mode
            result = await self.client.complete({"model": "test", "store": False, "input": "synthetic"})
            self.assertEqual(result["text"], "合成结果")
            self.assertEqual(result["provider_request_id"], "upstream-id")
        self.assertTrue(all(r["stream"] and not r["store"] for r in self.requests))
        self.assertTrue((await self.client.health())["reachable"])

    async def test_session_identity_is_a_header_not_model_content(self):
        await self.client.complete({"model": "test", "input": "synthetic"}, session_id="bound-session")
        self.assertEqual(self.headers[-1]["x-assistant-session"], "bound-session")
        self.assertNotIn("bound-session", json.dumps(self.requests[-1]))

    async def test_large_completion_event_within_response_budget(self):
        self.mode = "large-sse"
        result = await self.client.complete({"model": "test", "input": "synthetic"})
        self.assertEqual(result["text"], "合" * 30000)
        self.assertEqual(len(self.requests), 1)

    async def test_failures_never_retry_redirect_or_accept_tool_calls(self):
        for mode, expected in (
            ("error", "model_http_429"),
            ("http-401", "model_http_401"),
            ("http-403", "model_http_403"),
            ("http-500", "model_http_500"),
            ("redirect", "model_http_307"),
            ("truncated", "without confirmed"),
            ("incomplete", "failed or incomplete"),
            ("tool", "cannot request"),
            ("oversized", "too large"),
        ):
            self.mode = mode
            before = len(self.requests)
            with self.subTest(mode=mode), self.assertRaisesRegex((RuntimeError, ValueError), expected):
                await self.client.complete({"model": "test"})
            self.assertEqual(len(self.requests), before + 1)

    async def test_deadline_closes_slow_request_and_reuses_healthy_client(self):
        self.mode = "slow"
        try:
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await self.client.complete({"model": "test"})
            self.assertEqual(len(self.requests), 1)
            self.assertTrue((await self.client.health())["reachable"])
        finally:
            self.gate.set()
        self.mode = "json"
        self.assertEqual((await self.client.complete({"model": "test"}))["text"], "合成结果")

    async def test_real_http_overload_is_bounded_and_management_stays_responsive(self):
        self.mode = "slow"
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "models.sqlite3")
            runtime = Runtime(store)
            host = SimpleNamespace(
                config=SimpleNamespace(root=Path(folder), model_api=settings(concurrency=2, queue_size=2)),
                store=store,
                runtime=runtime,
            )
            # A separate pooled client is owned by the real plugin runtime.
            client = GatewayClient(
                ModelSettings(gateway_url=self.client.settings.gateway_url, concurrency=2), "private-key"
            )
            attach_usage(host, runtime)
            plugin = ModelsPlugin(host, client)
            runtime.add(plugin)
            await runtime.start()

            def request():
                return ModelRequest(
                    "infer",
                    {"request_id": str(uuid.uuid4()), "model": "test-model", "text": "Synthetic load"},
                )

            jobs = [asyncio.create_task(runtime.invoke(request())) for _ in range(4)]
            try:
                async with asyncio.timeout(2):
                    while self.inflight != 2 or plugin.pending != 4:
                        await asyncio.sleep(0.005)
                with self.assertRaisesRegex(RuntimeError, "queue full"):
                    await runtime.invoke(request())
                async with asyncio.timeout(0.5):
                    self.assertEqual((await runtime.invoke(ModelRequest("status", {})))["pending"], 4)
                self.assertEqual(len(self.requests), 2)
                self.gate.set()
                answers = await asyncio.gather(*jobs)
                self.assertEqual([a["state"] for a in answers], ["completed"] * 4)
                self.assertEqual(self.peak, 2)
                self.assertEqual(len(self.requests), 4)
                self.assertEqual(plugin.pending, 0)
            finally:
                self.gate.set()
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                await runtime.close()
                store.db.close()
