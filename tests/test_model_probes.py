"""Operator probes use real Pi adapters and local HTTP, never a paid provider."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from assistant_runtime.app.store import Store
from assistant_runtime.core.models.plugin import ModelsPlugin
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.api.ai_usage import UsageError
import test_model_connections as connections
from usage_fixture import attach_usage


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    handle = connections.ConnectionsTest.handle

    async def asyncSetUp(self):
        await connections.ConnectionsTest.asyncSetUp(self)
        self.store = Store(self.root / "assistant.sqlite3")
        self.runtime = Runtime(self.store)
        self.host = SimpleNamespace(config=SimpleNamespace(root=self.root, model_api={}), store=self.store)
        attach_usage(self.host, self.runtime)
        self.plugin = ModelsPlugin(self.host)
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        await connections.ConnectionsTest.asyncTearDown(self)

    def arguments(self, protocol="openai_responses"):
        self.protocol = protocol
        return {
            "request_id": str(uuid.uuid4()),
            "name": "draft",
            "entry": {
                "provider": "draft-connection",
                "model": "synthetic",
                "reasoning_efforts": ["low"],
                "default_reasoning_effort": "auto",
            },
            "provider": {"protocol": protocol, "base_url": self.url, "key_env": "TEST_KEY", "enabled": False},
            "key": "draft-private-key",
            "accounts": {},
        }

    async def test_draft_probe_all_protocols_does_not_save_or_grant_acceptance(self):
        before = (self.root / ".env").read_bytes()
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                args = self.arguments(protocol)
                reply = await self.plugin.test_model(args)
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["reply"], "OK")
                self.assertIn("elapsed_ms", reply)
                wire = json.dumps(self.requests[-1][2])
                self.assertIn("connectivity test", wire)
                self.assertNotIn('"tools": [', wire)
                request = self.requests[-1][2]
                if protocol == "anthropic":
                    self.assertGreater(request["max_tokens"], 16384)
                else:
                    for field in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
                        self.assertNotIn(field, request)
                    self.assertNotIn("maxOutputTokens", request.get("generationConfig", {}))
                self.assertIn(
                    "draft-private-key",
                    self.requests[-1][1].values()
                    if protocol in {"anthropic", "gemini"}
                    else [self.requests[-1][1]["Authorization"].removeprefix("Bearer ")],
                )
                self.assertNotIn("execution_validated", args["entry"])
                self.assertFalse(args["provider"]["enabled"])
                usage = await self.host.ai_usage.service.snapshot("model-test:" + args["request_id"])
                self.assertEqual(usage["requests"], 1)
        self.assertEqual((self.root / ".env").read_bytes(), before)
        self.assertIsNone(self.plugin.client)
        self.assertFalse(self.plugin.settings.enabled)
        self.assertFalse(self.plugin.active)

    async def test_receipts_prevent_duplicates_and_never_persist_credentials(self):
        args = self.arguments()
        reply = await self.plugin.test_model(args)
        self.assertTrue(reply["ok"])
        self.assertEqual(await self.plugin.test_model(args), reply)
        fresh = ModelsPlugin(self.host)
        self.assertEqual(await fresh.test_model(args), reply)
        conflict = await fresh.test_model({**args, "key": "changed-key"})
        self.assertEqual(conflict["code"], "request_conflict")
        self.assertEqual(len(self.requests), 1)
        receipts = json.dumps(self.store.db.execute("SELECT * FROM model_probes").fetchall())
        self.assertNotIn("draft-private-key", receipts)

    async def test_transient_rejections_are_bounded_and_do_not_expose_credentials(self):
        self.mode = "reject"
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                before = len(self.requests)
                reply = await self.plugin.test_model(self.arguments(protocol))
                self.assertEqual(reply["code"], "model_http_429", reply)
                self.assertNotIn("private-synthetic-key", json.dumps(reply))
                self.assertEqual(len(self.requests) - before, 3)
                self.assertEqual(reply["failure"]["attempt_count"], 3)

    async def test_explicit_quota_exhaustion_never_retries_any_protocol(self):
        self.mode = "quota"
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                before = len(self.requests)
                reply = await self.plugin.test_model(self.arguments(protocol))
                self.assertEqual(len(self.requests) - before, 1)
                self.assertEqual(reply["failure"]["category"], "quota", reply)
                self.assertEqual(reply["failure"]["provider_code"], "insufficient_quota")
                self.assertNotIn("draft-private-key", json.dumps(reply))

    async def test_usage_denial_prevents_dispatch(self):
        with patch.object(
            self.host.ai_usage.service, "admit", AsyncMock(side_effect=UsageError("usage_limit"))
        ):
            reply = await self.plugin.test_model(self.arguments())
        self.assertEqual(reply["code"], "usage_limit")
        self.assertFalse(self.requests)

    async def test_timeout_and_busy_are_bounded_and_unknown_is_not_replayed(self):
        self.mode = "slow"
        args = self.arguments()
        with patch("assistant_runtime.core.models.probe.TIMEOUT", 2):
            task = asyncio.create_task(self.plugin.test_model(args))
            async with asyncio.timeout(6):
                while not self.requests:
                    await asyncio.sleep(0.02)
            busy = await self.plugin.test_model(self.arguments())
            self.assertEqual(busy["code"], "busy")
            result = await task
        self.assertEqual(result["code"], "timeout")
        self.assertEqual(await self.plugin.test_model(args), result)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.plugin.active)

    async def test_shutdown_marks_uncertain_probe_and_restart_never_replays_it(self):
        self.mode = "slow"
        args = self.arguments()
        task = asyncio.create_task(self.plugin.test_model(args))
        async with asyncio.timeout(8):
            while not self.requests:
                await asyncio.sleep(0.02)
        self.assertTrue(self.plugin.maintenance_pending())
        await self.plugin.maintenance_cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        fresh = ModelsPlugin(self.host)
        result = await fresh.test_model(args)
        self.assertEqual(result["code"], "unknown")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.plugin.active)

    async def test_native_output_bound_does_not_prevent_single_metered_inference(self):
        fake = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            output_bound=AsyncMock(return_value=32768),
            complete=AsyncMock(
                return_value={"text": "OK", "usage": {"input_tokens": 10, "output_tokens": 1}}
            ),
        )
        args = self.arguments()
        usage = self.host.ai_usage.service
        with (
            patch("assistant_runtime.core.models.probe.ModelClient", return_value=fake),
            patch.object(usage, "admit", AsyncMock(wraps=usage.admit)) as admission,
        ):
            result = await self.plugin.test_model(args)
            self.assertEqual(await self.plugin.test_model(args), result)
        self.assertTrue(result["ok"], result)
        fake.complete.assert_awaited_once()
        self.assertEqual(admission.call_count, 1)
        self.assertEqual(admission.call_args.kwargs["bounds"]["output_tokens"], 32768)
        fake.close.assert_awaited_once()
        self.assertFalse(self.requests)

    async def test_probe_uses_configured_output_budgets_without_a_separate_test_cap(self):
        self.plugin.settings = replace(self.plugin.settings, max_output_tokens=4096)
        args = self.arguments()
        self.assertTrue((await self.plugin.test_model(args))["ok"])
        self.assertEqual(self.requests[-1][2]["max_output_tokens"], 4096)
        args = self.arguments()
        args["entry"]["max_output_tokens"] = 2048
        self.assertTrue((await self.plugin.test_model(args))["ok"])
        self.assertEqual(self.requests[-1][2]["max_output_tokens"], 2048)
