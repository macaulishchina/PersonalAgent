"""Connection lifetime, effect-free startup warmup and bounded fallback."""

import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.kernel.contracts import DecisionQuery, DecisionRequest
from assistant_runtime.kernel.runtime import Manifest, Runtime
from assistant_runtime.plugins.decisions.errors import DecisionError
from assistant_runtime.plugins.decisions.questions import prepare
from assistant_runtime.plugins.decisions.settings import DecisionSettings
import test_decisions as fixtures
from test_decisions import FakeProvider, sample


class TransportTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.HttpTest.asyncSetUp
    asyncTearDown = fixtures.HttpTest.asyncTearDown

    async def test_warmup_and_judgments_reuse_one_pool_without_sending_inference_to_warmup(self):
        await self.provider.warmup()
        self.assertEqual(self.provider.diagnostics()["warmup"]["state"], "completed")
        self.assertEqual(self.connections[0][0], "GET")
        self.assertIsNone(self.seen[0][1])
        job = prepare(sample(), "test", "jev-1.13.0")
        for _ in range(2):
            await self.provider.evaluate(job)
            metrics = self.provider.diagnostics()["last_judgment"]
            self.assertTrue(metrics["reused_connection"])
            self.assertIsNone(metrics["connection_ms"])
            self.assertIsNone(metrics["dns_ms"])
            self.assertIsNotNone(metrics["response_headers_ms"])
        self.assertEqual(len({identity for _, identity in self.connections}), 1)
        await self.provider.warmup()
        self.provider.reload()
        await self.provider.warmup()
        self.assertEqual([method for method, _ in self.connections], ["GET", "POST", "POST"])
        self.assertNotIn("test-local-key", json.dumps(self.provider.diagnostics()))

    async def test_slow_warmup_does_not_serialize_a_real_request(self):
        self.warmup_release.clear()
        task = asyncio.create_task(self.provider.warmup())
        try:
            await asyncio.wait_for(self.warmup_entered.wait(), 1)
            result = await self.provider.evaluate(prepare(sample(), "test", "jev-1.13.0"))
            self.assertEqual(result["choice"], "sleep")
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.provider.diagnostics()["warmup"]["state"], "cancelled")
        finally:
            self.warmup_release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_warmup_is_bounded_sanitized_and_not_retried(self):
        for mode, status, error in (
            ("normal", 401, "authentication_failed"),
            ("redirect", 200, "upstream_unavailable"),
            ("large", 200, "response_too_large"),
            ("timeout", 200, "timeout"),
        ):
            with self.subTest(mode=mode, status=status):
                self.provider.warmup_result = {"state": "not_started"}
                self.mode, self.status = mode, status
                before = len(self.seen)
                started = time.monotonic()
                await self.provider.warmup()
                self.assertLess(time.monotonic() - started, 0.5)
                info = self.provider.diagnostics()["warmup"]
                self.assertEqual((info["state"], info["error"]), ("failed", error))
                await self.provider.warmup()
                self.assertEqual(len(self.seen), before + 1)
        self.mode, self.status = "normal", 200
        result = await self.provider.evaluate(prepare(sample(), "test", "jev-1.13.0"))
        self.assertEqual(result["choice"], "sleep")

    async def test_unconsented_disabled_or_missing_key_never_warm_up(self):
        for field in ("enabled", "external_data_consent", "warmup_enabled", "key"):
            with self.subTest(field=field):
                self.provider.warmup_result = {"state": "not_started"}
                owner = self.provider if field == "key" else self.provider.settings
                before = getattr(owner, field)
                setattr(owner, field, None if field == "key" else False)
                try:
                    await self.provider.warmup()
                    self.assertNotEqual(self.provider.warmup_result["state"], "completed")
                finally:
                    setattr(owner, field, before)
        self.assertEqual(self.seen, [])


class StartupTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ShadowTest.asyncSetUp
    asyncTearDown = fixtures.ShadowTest.asyncTearDown

    async def restart(self, **settings):
        await self.runtime.close()
        self.config.decisions.update(settings)
        self.runtime = type(self.runtime)(self.store)
        self.plugin = type(self.plugin)(SimpleNamespace(config=self.config), self.provider)
        self.runtime.add(self.plugin)
        await asyncio.wait_for(self.runtime.start(), 0.5)

    async def test_startup_warmup_is_owned_nonblocking_and_cancelled_before_close(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.provider.warmup = AsyncMock(side_effect=slow)
        await self.restart(mode="active")
        await asyncio.wait_for(entered.wait(), 0.5)
        response = await self.runtime.invoke(DecisionQuery("voice_intent", "while-warming", sample().state))
        self.assertEqual(response["status"], "ok")
        await self.runtime.invoke(DecisionRequest("reload", {}))
        self.provider.warmup.assert_awaited_once()
        await self.runtime.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.provider.closed)

    async def test_startup_respects_each_gate_and_never_schedules_a_timer(self):
        for settings in (
            {"enabled": False},
            {"external_data_consent": False},
            {"warmup_enabled": False},
        ):
            self.provider = FakeProvider()
            self.provider.warmup = AsyncMock()
            await self.restart(
                **{"enabled": True, "external_data_consent": True, "warmup_enabled": True, **settings}
            )
            await asyncio.sleep(0)
            self.provider.warmup.assert_not_awaited()
            tasks = self.runtime.contexts["decisions"].tasks
            self.assertFalse(any(task.get_name().endswith(":warmup") for task in tasks))

    async def test_missing_credentials_never_schedules_warmup(self):
        self.provider = FakeProvider()
        self.provider.reload = lambda: (_ for _ in ()).throw(DecisionError("credential_missing"))
        self.provider.warmup = AsyncMock()
        await self.restart()
        self.provider.warmup.assert_not_awaited()

    async def test_network_warmup_waits_for_other_plugins_without_gating_judgments(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Initializing:
            manifest = Manifest("initializing")

            async def start(self, context):
                entered.set()
                await release.wait()

        await self.runtime.close()
        self.provider.warmup = AsyncMock()
        self.config.decisions["mode"] = "active"
        self.runtime = Runtime(self.store)
        self.plugin = type(self.plugin)(SimpleNamespace(config=self.config), self.provider)
        self.runtime.add(self.plugin)
        self.runtime.add(Initializing())
        startup = asyncio.create_task(self.runtime.start())
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            self.provider.warmup.assert_not_awaited()
            response = await self.runtime.invoke(
                DecisionQuery("voice_intent", "before-all-ready", sample().state)
            )
            self.assertEqual(response["status"], "ok")
            release.set()
            await asyncio.wait_for(startup, 0.5)
            await asyncio.sleep(0)
            self.provider.warmup.assert_awaited_once()
        finally:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)

    async def test_timeout_still_falls_back_with_a_three_second_total_default_budget(self):
        self.assertEqual(DecisionSettings.parse({}).active_timeout_seconds, 3)
        self.provider.release.clear()
        await self.restart(mode="active")
        started = time.monotonic()
        response = await self.runtime.invoke(DecisionQuery("voice_intent", "bounded", sample().state))
        self.assertEqual(response["reason"], "timeout")
        self.assertGreater(time.monotonic() - started, 2.8)
        self.assertLess(time.monotonic() - started, 3.5)


class SettingsTest(unittest.TestCase):
    def test_transport_settings_are_bounded(self):
        settings = DecisionSettings.parse({})
        self.assertEqual((settings.keepalive_seconds, settings.dns_cache_seconds), (120, 300))
        for invalid in (
            {"warmup_enabled": 1},
            {"keepalive_seconds": True},
            {"keepalive_seconds": 0},
            {"keepalive_seconds": 601},
            {"dns_cache_seconds": -1},
            {"dns_cache_seconds": float("inf")},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                DecisionSettings.parse(invalid)
