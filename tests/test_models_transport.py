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
from assistant_runtime.core.models.client import ModelClient
from assistant_runtime.core.pi.accounts import NativeAccounts
from pi_gateway import sse_text_reply, sse_tool_call
from dataclasses import replace
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.models.plugin import ModelsPlugin
from assistant_runtime.api.models import ModelRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.app.store import Store
from test_models import settings


class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_timing_is_optional_and_usage_is_provider_owned(self):
        result = await self.client.complete(self.payload())
        self.assertEqual(result["usage"]["output_tokens"], 12)
        self.assertIsInstance(result["transport_timings"], dict)

    def payload(self):
        return {
            "model": "test-model",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "synthetic"}]}],
            "reasoning": {"effort": "auto"},
            "max_output_tokens": 128,
        }

    async def test_continuous_output_still_has_total_deadline_without_retry(self):
        import time

        # Load the real SDK before measuring a streaming deadline. Cold module
        # import latency must not turn this into a pre-dispatch timeout test.
        await self.client.complete(self.payload())
        before = len(self.requests)
        self.client.settings = replace(self.client.settings, timeout_seconds=0.3)
        self.mode = "streaming-slow"
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await self.client.complete(self.payload())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(self.requests), before + 1)

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
        self.area = tempfile.TemporaryDirectory()
        self.root = Path(self.area.name)
        (self.root / ".env").write_text("TEST_KEY=private-key\n")
        raw = settings(
            providers={
                "test": {
                    "protocol": "openai_responses",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "key_env": "TEST_KEY",
                    "enabled": True,
                }
            }
        )
        self.client = ModelClient(
            ModelSettings.parse(raw), accounts=NativeAccounts(self.root, ModelSettings.parse(raw))
        )
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        self.area.cleanup()

    async def handle(self, request):
        self.requests.append(await request.json())
        self.headers.append(dict(request.headers))
        self.assertEqual(request.headers["Authorization"], "Bearer private-key")
        if self.mode == "streaming-slow":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                for _ in range(30):
                    await response.write(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n')
                    await asyncio.sleep(0.03)
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response
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
        events = sse_text_reply("upstream-id", "合" * 30000 if self.mode == "large-sse" else "合成结果")
        if self.mode == "tool":
            events = sse_tool_call("call", "shell", {})
        elif self.mode in {"truncated", "incomplete"}:
            events = events[:-1]
        body = b"".join(b"event: message\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)
        return web.Response(body=body, content_type="text/event-stream")

    async def test_sse_completion_and_chunked_json(self):
        for mode in ("sse",):
            self.mode = mode
            result = await self.client.complete(self.payload())
            self.assertEqual(result["text"], "合成结果")
            self.assertEqual(result["provider_request_id"], "resp_upstream-id")
        self.assertTrue(all(r["stream"] and not r["store"] for r in self.requests))
        self.assertTrue((await self.client.health())["reachable"])
        self.mode = "json"
        before = len(self.requests)
        # A provider ignoring the requested streaming protocol is not a confirmed
        # completion. The adapter must reject it without retrying another request.
        with self.assertRaises(RuntimeError):
            await self.client.complete(self.payload())
        self.assertEqual(len(self.requests), before + 1)

    async def test_session_identity_is_a_header_not_model_content(self):
        await self.client.complete(self.payload(), session_id="bound-session")
        self.assertEqual(self.requests[-1]["prompt_cache_key"], "bound-session")
        self.assertNotIn("bound-session", json.dumps(self.requests[-1]["input"]))

    async def test_large_completion_event_within_response_budget(self):
        self.mode = "large-sse"
        result = await self.client.complete(self.payload())
        self.assertEqual(result["text"], "合" * 30000)
        self.assertEqual(len(self.requests), 1)

    async def test_failures_never_retry_redirect_or_accept_tool_calls(self):
        for mode, expected in (
            ("error", "model_http_429"),
            ("http-401", "model_http_401"),
            ("http-403", "model_http_403"),
            ("http-500", "model_http_500"),
            ("redirect", "unknown"),
            ("truncated", "unknown"),
            ("incomplete", "unknown"),
            ("tool", "model_result_invalid"),
            ("oversized", "unknown"),
        ):
            self.mode = mode
            before = len(self.requests)
            with self.subTest(mode=mode), self.assertRaisesRegex((RuntimeError, ValueError), expected):
                await self.client.complete(self.payload())
            self.assertEqual(len(self.requests), before + 1)

    async def test_deadline_closes_slow_request_and_reuses_healthy_client(self):
        self.mode = "slow"
        try:
            job = asyncio.create_task(self.client.complete(self.payload()))
            async with asyncio.timeout(5):
                while not self.requests:
                    await asyncio.sleep(0.01)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(job, 0.05)
            self.assertEqual(len(self.requests), 1)
            self.assertTrue((await self.client.health())["reachable"])
        finally:
            self.gate.set()
        self.mode = "sse"
        self.assertEqual((await self.client.complete(self.payload()))["text"], "合成结果")

    async def test_shared_http_concurrency_and_unbounded_queue_keep_management_responsive(self):
        self.mode = "slow"
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "models.sqlite3")
            runtime = Runtime(store)
            host = SimpleNamespace(
                config=SimpleNamespace(root=Path(folder), model_api=settings(), execution={"concurrency": 2}),
                store=store,
                runtime=runtime,
            )
            # A separate pooled client is owned by the real plugin runtime.
            limited = self.client.settings
            client = ModelClient(limited, accounts=NativeAccounts(self.root, limited))
            attach_usage(host, runtime)
            plugin = ModelsPlugin(host, client)
            runtime.add(plugin)
            await runtime.start()

            def request():
                return ModelRequest(
                    "infer",
                    {"request_id": str(uuid.uuid4()), "model": "test-model", "text": "Synthetic load"},
                )

            jobs = [asyncio.create_task(runtime.invoke(request())) for _ in range(5)]
            try:
                async with asyncio.timeout(8):
                    while self.inflight != 2 or plugin.pending != 5:
                        await asyncio.sleep(0.005)
                async with asyncio.timeout(0.5):
                    self.assertEqual((await runtime.invoke(ModelRequest("status", {})))["pending"], 5)
                self.assertEqual(len(self.requests), 2)
                self.gate.set()
                answers = await asyncio.gather(*jobs)
                self.assertEqual([a["state"] for a in answers], ["completed"] * 5)
                self.assertEqual(self.peak, 2)
                self.assertEqual(len(self.requests), 5)
                self.assertEqual(plugin.pending, 0)
            finally:
                self.gate.set()
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                await runtime.close()
                store.db.close()
