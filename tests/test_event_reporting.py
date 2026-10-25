"""Real runtime/SQLite tests for the producer boundary and failure semantics."""

import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from assistant_runtime.app.bundled import context_for
from assistant_runtime.api.contracts import EventRequest, ObservationRequest
from assistant_runtime.kernel.runtime import Manifest, Runtime
from assistant_runtime.core.events.plugin import EventsPlugin
from assistant_runtime.plugins.observations.plugin import ObservationsPlugin


class PushSource:
    """A source with no polling interval; models a subscription/business callback."""

    def __init__(self, reporter):
        self.reporter = reporter
        self.health = {"state": "starting"}
        self.stopped = False

    async def run(self):
        try:
            await self.reporter.checkpoint()
            self.health = {"state": "healthy"}
            await asyncio.Event().wait()
        finally:
            self.stopped = True

    async def verify(self):
        return {"connected": True, "semantics_verified": False}


class PushProvider:
    description = {"types": ["test.pushed"], "config_schema": {"type": "object"}}
    max_instances = 64

    def validate(self, config):
        if config != {}:
            raise ValueError("No configuration")
        return {}

    def create(self, reporter, config):
        return PushSource(reporter)


class ReportingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Runtime(None)
        self.host = SimpleNamespace(
            assistant=None,
            runtime=self.runtime,
            config=SimpleNamespace(
                root=self.root, state_dir=self.root / "state", events={"queue_size": 8}, plugin_backends={}
            ),
        )
        self.events = EventsPlugin(self.host)
        self.sources = ObservationsPlugin(context_for(self.host, "observations"), {"push": PushProvider()})
        self.runtime.add(self.events)
        self.runtime.add(self.sources)
        await self.runtime.start()
        self.context = self.runtime.contexts["observations"]
        self.reporter = self.context.event_reporter("local")
        self.event = {
            "type": "test.pushed",
            "subject": "device",
            "data": {"value": 1},
            "event_id": "stable",
            "occurred_at": time.time(),
        }

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def configure(self, ident, enabled=True):
        return await self.runtime.invoke(
            ObservationRequest(
                "configure",
                {
                    "id": ident,
                    "revision": self.sources.registry.revision,
                    "definition": {"adapter": "push", "config": {}, "enabled": enabled},
                },
            )
        )

    async def test_commit_receipt_identity_conflict_and_delete_retry(self):
        first = await self.reporter.report(**self.event)
        self.assertEqual((await self.events.storage.call("get", id=first["id"]))["source"], "local")
        self.assertTrue((await self.reporter.report(**self.event))["duplicate"])
        with self.assertRaisesRegex(ValueError, "conflict"):
            await self.reporter.report(**dict(self.event, data={"value": 2}))
        await self.runtime.invoke(EventRequest("delete", {"id": first["id"], "revision": 1}))
        self.assertTrue((await self.reporter.report(**self.event))["deleted"])

    async def test_lost_commit_ack_retry_uses_same_identity(self):
        real = self.events.storage.call
        committed, release = asyncio.Event(), asyncio.Event()

        async def delayed(action, **args):
            result = await real(action, **args)
            if action == "append":
                committed.set()
                await release.wait()
            return result

        self.events.storage.call = delayed
        task = asyncio.create_task(self.reporter.report(**self.event))
        await committed.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.events.storage.call = real
        release.set()
        self.assertTrue((await self.reporter.report(**self.event))["duplicate"])

    async def test_nonblocking_overflow_mutation_safety_and_persisted_gap(self):
        original = dict(self.event, data={"value": 1})
        receipt = self.reporter.offer(**original)
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.reason, "queued")
        original["data"]["value"] = 999
        for index in range(7):
            self.assertTrue(self.reporter.offer(**dict(self.event, event_id=str(index))).accepted)
        self.assertEqual(self.reporter.offer(**dict(self.event, event_id="overflow")).reason, "full")
        await self.events.ingress.drain("local")
        await self.reporter.checkpoint()
        rows = (await self.events.storage.call("query", source="local"))["items"]
        self.assertTrue(any(row["record"]["evidence"] == "gap" for row in rows))
        self.assertTrue(all(row["record"]["data"].get("value", 1) == 1 for row in rows))
        self.assertEqual(
            self.reporter.offer(type="bad", subject="x", data={"bad": object()}).reason, "invalid"
        )

    async def test_storage_failure_is_visible_after_successful_admission(self):
        original = self.events.storage.call

        async def fail(action, **args):
            if action == "append":
                raise RuntimeError("storage unavailable")
            return await original(action, **args)

        self.events.storage.call = fail
        self.assertTrue(self.reporter.offer(**self.event).accepted)
        await self.events.ingress.drain("local")
        self.assertIn("local", self.events.ingress.status()["losses"])
        self.assertEqual(self.runtime.contexts["events"].state, "degraded")
        self.events.storage.call = original
        await self.reporter.checkpoint()
        rows = (await original("query", source="local"))["items"]
        self.assertTrue(any(row["record"]["evidence"] == "gap" for row in rows))

    async def test_reconfigure_only_target_and_shutdown_drains(self):
        await self.configure("a")
        await self.configure("b")
        await asyncio.sleep(0.05)
        old_a = self.sources.instances["a"]
        old_b = self.sources.instances["b"]
        self.assertTrue(old_a[1].offer(**self.event).accepted)
        await self.configure("a", False)
        self.assertTrue(old_a[0].stopped)
        self.assertFalse(old_a[1].offer(**self.event).accepted)
        self.assertIs(self.sources.instances["b"], old_b)
        self.assertTrue(old_b[1].offer(**dict(self.event, event_id="shutdown")).accepted)
        await self.runtime.close()
        from assistant_runtime.core.events.storage import Storage

        storage = Storage(self.host.config.state_dir / "events", self.events.settings)
        await storage.start()
        try:
            result = await storage.call("query", source="b")
            self.assertTrue(any(row["record"]["key"] == "shutdown" for row in result["items"]))
            status = await storage.call("status")
            self.assertTrue(all(row["gap_start"] is not None for row in status["coverage"]))
        finally:
            await storage.close()
        self.assertIsNone(self.runtime.event_sink)

    async def test_optional_recorder_and_bound_identity(self):
        with self.assertRaisesRegex(ValueError, "another plugin"):
            self.runtime.contexts["events"].event_reporter("local")
        self.context.generation += 1
        self.assertFalse(self.reporter.offer(**self.event).accepted)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await self.reporter.report(**self.event)
        await self.runtime.close()
        runtime = Runtime(None)

        class Producer:
            manifest = Manifest("producer")

            async def start(self, context):
                self.reporter = context.event_reporter()

        producer = Producer()
        runtime.add(producer)
        await runtime.start()
        try:
            self.assertEqual(producer.reporter.offer(**self.event).reason, "unavailable")
            self.assertEqual(runtime.contexts["producer"].state, "ready")
        finally:
            await runtime.close()

    async def test_report_admission_is_bounded_even_when_one_source_is_slow(self):
        real = self.events.storage.call
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(action, **args):
            if action == "append":
                entered.set()
                await release.wait()
            return await real(action, **args)

        self.events.storage.call = slow
        pending = [
            asyncio.create_task(self.reporter.report(**dict(self.event, event_id=str(i)))) for i in range(8)
        ]
        await entered.wait()
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "admission full"):
            await self.reporter.report(**dict(self.event, event_id="excess"))
        self.assertLessEqual(self.events.ingress.inflight, 8)
        release.set()
        await asyncio.gather(*pending)
        self.assertEqual(self.events.ingress.inflight, 0)
        self.assertEqual(self.events.ingress.locks, {})

    async def test_new_loss_during_gap_commit_is_not_cleared(self):
        ingress = self.events.ingress
        original = self.events.storage.call
        once = True

        async def race(action, **args):
            nonlocal once
            result = await original(action, **args)
            if action == "coverage" and once:
                once = False
                ingress.lost("local", 11, "another_overflow")
            return result

        ingress.lost("local", 10, "overflow")
        self.events.storage.call = race
        await self.reporter.checkpoint(at=12)
        self.assertIn("local", ingress.losses)
        await self.reporter.checkpoint(at=13)
        self.assertNotIn("local", ingress.losses)

    async def test_recorder_alone_has_no_source_configuration_requirement(self):
        await self.runtime.close()
        runtime = Runtime(None)
        host = SimpleNamespace(assistant=None, runtime=runtime, config=self.host.config)
        recorder = EventsPlugin(host)
        runtime.add(recorder)
        await runtime.start()
        try:
            self.assertEqual(runtime.contexts["events"].state, "ready")
            capabilities = await runtime.invoke(EventRequest("capabilities"))
            self.assertEqual(capabilities["adapters"], {})
            self.assertFalse(capabilities["source_management"])
            reporter = runtime.contexts["events"].event_reporter("standalone")
            result = await reporter.report(**self.event)
            self.assertTrue(result["id"])
        finally:
            await runtime.close()
