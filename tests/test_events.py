import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastmcp import Client
from assistant_runtime.app.bundled import context_for
from assistant_runtime.api.contracts import EventRequest, TaskRequest
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.plugins.observations.plugin import ObservationsPlugin
from assistant_runtime.core.speech.wake_events import WakeEvents
from assistant_runtime.core.speech.lifecycle import VoiceLifecycle
from tool_expectations import EVENT_TOOLS
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.core.events.tools import create_server
from assistant_runtime.core.pi.mcp import server_config
from assistant_runtime.core.events.plugin import EventsPlugin
from assistant_runtime.core.events.settings import EventSettings
from assistant_runtime.core.events.storage import Storage


def record(key="one", **updates):
    return {
        "key": key,
        "type": "test.observed",
        "version": 1,
        "subject": "device",
        "occurred_at": 100.0,
        "evidence": "observation",
        "data": {"value": 1},
        **updates,
    }


class StorageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.storage = Storage(self.directory, EventSettings())
        await self.storage.start()

    async def asyncTearDown(self):
        await self.storage.close()
        self.temp.cleanup()

    async def add(self, rec=None):
        return await self.storage.call("append", source="test", record=rec or record())

    async def test_dedup_conflict_correction_delete_changes_and_restart(self):
        first = await self.add()
        self.assertTrue((await self.add())["duplicate"])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await self.add(record(data={"value": 2}))
        revised = record(data={"value": 3}, evidence="user_statement")
        await self.storage.call("correct", id=first["id"], revision=1, record=revised)
        retried = await self.storage.call("correct", id=first["id"], revision=1, record=revised)
        self.assertTrue(retried["duplicate"])
        current = await self.storage.call("get", id=first["id"])
        self.assertEqual(current["record"], revised)
        self.assertEqual(current["revisions"][0]["record"], record())
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            await self.storage.call("delete", id=first["id"], revision=1)
        await self.storage.call("delete", id=first["id"], revision=2)
        self.assertEqual(
            [row["action"] for row in (await self.storage.call("changes"))["items"]],
            ["append", "correct", "delete"],
        )
        gone = await self.storage.call("get", id=first["id"])
        self.assertIsNone(gone["record"])
        self.assertIsNone(gone["source"])
        self.assertEqual(gone["revisions"], [])
        await self.storage.close()
        self.storage = Storage(self.directory, EventSettings())
        await self.storage.start()
        self.assertTrue((await self.add())["deleted"])
        self.assertTrue((await self.add(record(data={"value": "changed retry"})))["deleted"])
        self.assertEqual((await self.storage.call("query"))["items"], [])

    async def test_lost_ack_and_close_drains_accepted_writes(self):
        future = self.storage.writer.submit("append", {"source": "test", "record": record()})
        # An IPC caller disappears without awaiting its durable receipt.
        await self.storage.close()
        self.assertTrue(future.done())
        self.storage = Storage(self.directory, EventSettings())
        await self.storage.start()
        self.assertTrue((await self.add())["duplicate"])

    async def test_out_of_order_keyset_and_change_order(self):
        for key, at in (("a", 2), ("b", 1), ("c", 2)):
            await self.add(record(key, occurred_at=at))
        page = await self.storage.call("query", limit=2)
        self.assertEqual([r["event_time"] for r in page["items"]], [2, 2])
        next_page = await self.storage.call("query", before=page["next"], limit=2)
        self.assertEqual([r["event_time"] for r in next_page["items"]], [1])
        self.assertFalse({r["id"] for r in page["items"]} & {r["id"] for r in next_page["items"]})
        with self.assertRaises(ValueError):
            await self.storage.call("query", limit=101)
        stats = await self.storage.call("stats", source="test")
        self.assertEqual(stats["groups"][0]["count"], 3)

    async def test_nullable_occurrence_uses_interval_end_for_insert_and_correction(self):
        interval = record("interval", occurred_at=None, observed_start=10, observed_end=20)
        result = await self.add(interval)
        item = await self.storage.call("get", id=result["id"])
        self.assertEqual(item["event_time"], 20)
        self.assertEqual(item["record"], interval)
        self.assertTrue((await self.add(interval))["duplicate"])
        amended = {**interval, "observed_end": 30, "evidence": "user_statement"}
        await self.storage.call("correct", id=result["id"], revision=1, record=amended)
        self.assertEqual((await self.storage.call("get", id=result["id"]))["event_time"], 30)
        # Epoch zero is a valid occurrence, not a missing timestamp.
        await self.add(record("epoch", occurred_at=0))
        page = await self.storage.call("query", limit=1)
        self.assertEqual(page["items"][0]["event_time"], 30)
        older = await self.storage.call("query", before=page["next"], limit=1)
        self.assertEqual(older["items"][0]["event_time"], 0)

    async def test_query_cannot_claim_coverage_beyond_last_observation(self):
        await self.storage.call("snapshot", source="test", at=10, records=[])
        await self.storage.call("snapshot", source="test", at=20, records=[])
        for action, complete in (("query", "complete"), ("stats", "coverage_complete")):
            within = await self.storage.call(action, source="test", since=10, until=20)
            self.assertTrue(within[complete])
            outside = await self.storage.call(action, source="test", since=10, until=21)
            self.assertFalse(outside[complete])
            before = await self.storage.call(action, source="test", since=9, until=20)
            self.assertFalse(before[complete])
            with self.assertRaisesRegex(ValueError, "Reversed query interval"):
                await self.storage.call(action, source="test", since=20, until=10)

    async def test_snapshot_atomicity_gap_and_quota_resume(self):
        await self.storage.call("coverage", source="test", at=1, reason="offline")
        with self.assertRaises(ValueError):
            await self.storage.call("snapshot", source="test", at=2, records=[record(), record(version=0)])
        self.assertEqual((await self.storage.call("query"))["items"], [])
        self.assertEqual((await self.storage.call("status"))["coverage"][0]["gap_start"], 1)
        await self.storage.call("snapshot", source="test", at=3, records=[record()])
        self.assertEqual(len((await self.storage.call("query"))["items"]), 2)
        self.assertIsNone((await self.storage.call("status"))["coverage"][0]["gap_start"])
        self.storage.writer.settings = EventSettings(quota_bytes=1024**2)
        ids = []
        for index in range(200):
            try:
                ids.append((await self.add(record(str(index), data={"value": "x" * 14000})))["id"])
            except RuntimeError as exc:
                self.assertIn("quota", str(exc))
                break
        else:
            self.fail("quota never enforced")
        # Successful empty polling checkpoints must not hide a capacity failure.
        await self.storage.call("snapshot", source="idle", at=4, records=[])
        self.assertIn("quota", (await self.storage.call("status"))["write_error"])
        for ident in ids:
            await self.storage.call("delete", id=ident, revision=1)
        self.assertFalse((await self.add(record("after-delete", data={"value": "x" * 14000})))["duplicate"])

    async def test_v1_tombstones_migrate_without_revival(self):
        first = await self.add()
        await self.storage.close()
        with sqlite3.connect(self.directory / "events.sqlite3") as db:
            db.execute(
                "UPDATE records SET source=NULL,event_key=NULL,type=NULL,subject=NULL,event_time=NULL,received_at=NULL,fingerprint=NULL,payload=NULL,revision=2,deleted_at=123 WHERE id=?",
                (first["id"],),
            )
            db.execute("PRAGMA user_version=1")
        self.storage = Storage(self.directory, EventSettings())
        await self.storage.start()
        self.assertTrue((await self.add())["deleted"])
        self.assertEqual((await self.storage.call("get", id=first["id"]))["deleted_at"], 123)
        self.assertTrue((await self.storage.call("query"))["deleted_records_present"])

    async def test_small_record_deletions_release_capacity_without_resurrection(self):
        self.storage.writer.settings = EventSettings(quota_bytes=1024**2)
        inserted = []
        for index in range(1500):
            try:
                inserted.append((await self.add(record("small:" + str(index))))["id"])
            except RuntimeError:
                break
        else:
            self.fail("Small records did not exhaust the bounded quota")
        for ident in inserted:
            await self.storage.call("delete", id=ident, revision=1)
        self.assertFalse((await self.add(record("new-small")))["duplicate"])
        self.assertTrue((await self.add(record("small:0")))["deleted"])

    async def test_bounded_admission_cancelled_receipt_and_keyword_counts(self):
        self.storage.writer.queue.maxsize = 8
        futures, rejected = [], 0
        for index in range(100):
            try:
                futures.append(
                    asyncio.wrap_future(
                        self.storage.writer.submit(
                            "append",
                            {
                                "source": "wake",
                                "record": record(
                                    str(index),
                                    type="wake.accepted",
                                    data={"entrance": "keyword" if index % 2 else "cli"},
                                ),
                            },
                        )
                    )
                )
            except RuntimeError:
                rejected += 1
        results = await asyncio.gather(*futures)
        self.assertGreater(rejected, 0)
        self.assertGreater(len(results), 0)
        filtered = await self.storage.call("query", source="wake", entrance="keyword", evidence="observation")
        self.assertTrue(all(r["record"]["data"]["entrance"] == "keyword" for r in filtered["items"]))
        self.assertLessEqual(self.storage.writer.queue.qsize(), 8)

    async def test_read_worker_timeout_and_burst_do_not_block_loop(self):
        for index in range(30):
            await self.add(record(str(index)))
        # A real expensive SQLite recursive query in the dedicated reader, using
        # the same progress deadline as public queries.
        from assistant_runtime.core.events import storage

        original = storage.read

        def slow(db, action, args):
            if action == "slow":
                return db.execute(
                    "WITH RECURSIVE numbers(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM numbers WHERE x<100000000) SELECT SUM(x) FROM numbers"
                ).fetchone()
            return original(db, action, args)

        self.storage.reader.settings = EventSettings(query_seconds=0.1)
        storage.read = slow
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.01)
                ticks += 1

        try:
            pending = asyncio.wrap_future(self.storage.reader.submit("slow", {}))
            await heartbeat()
            with self.assertRaises(sqlite3.OperationalError):
                await pending
            self.assertEqual(ticks, 20)
        finally:
            storage.read = original
        self.assertEqual(len((await self.storage.call("query"))["items"]), 30)


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.runtime = Runtime(None)
        self.host = SimpleNamespace(
            config=SimpleNamespace(root=root, state_dir=root / "state", events={}), runtime=self.runtime
        )
        self.host.assistant = self.host
        self.plugin = EventsPlugin(self.host)
        self.sources = ObservationsPlugin(
            context_for(self.host, "observations"), {"wake": WakeEvents(self.host)}
        )
        self.task_info = {}
        test = self

        class Tasks:
            manifest = Manifest("tasks")

            async def start(self, context):
                async def handler(request):
                    return test.task_info

                context.register(TaskRequest, handler)

        self.runtime.add(self.plugin)
        self.runtime.add(self.sources)
        self.runtime.add(Tasks())
        await self.runtime.start()
        self.context = self.runtime.contexts["events"]

    async def asyncTearDown(self):
        await self.runtime.close()
        self.temp.cleanup()

    async def configure_wake(self):
        await self.plugin.request(
            EventRequest(
                "configure",
                {
                    "id": "wake",
                    "revision": 0,
                    "definition": {"adapter": "wake", "enabled": True, "config": {}},
                },
            )
        )
        for _ in range(100):
            if getattr(self.host, "wake_reporter", None):
                return
            await asyncio.sleep(0.01)
        self.fail("Wake observer did not start")

    async def test_background_permissions_and_source_cas(self):
        self.task_info = {"run": {"task_id": "job", "state": "running", "events_management": False}}

        def background(action, **args):
            return self.plugin.request(EventRequest(action, args, "run"))

        written = await background("record", record=record(evidence="assistant_summary"))
        self.assertEqual((await background("get", id=written["id"]))["source"], "task:job")
        with self.assertRaisesRegex(ValueError, "events_management"):
            await background(
                "configure",
                id="wake",
                revision=0,
                definition={"adapter": "wake", "enabled": True, "config": {}},
            )
        foreign = await self.plugin.storage.call("append", source="device", record=record())
        with self.assertRaisesRegex(ValueError, "another source"):
            await background("delete", id=foreign["id"], revision=1)
        await self.plugin.request(
            EventRequest(
                "configure",
                {
                    "id": "wake",
                    "revision": 0,
                    "definition": {"adapter": "wake", "enabled": True, "config": {}},
                },
            )
        )
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            await self.plugin.request(EventRequest("enable", {"id": "wake", "revision": 0, "enabled": False}))
        self.task_info["run"]["events_management"] = True
        await background("enable", id="wake", revision=1, enabled=False)
        self.task_info["run"]["state"] = "completed"
        with self.assertRaisesRegex(ValueError, "active"):
            await background("query")

    async def test_wake_enqueue_and_correlated_outcomes(self):
        await self.configure_wake()
        VoiceLifecycle._record_wake(
            self.host, "accepted", "test", entrance="keyword", keyword="configured phrase"
        )
        VoiceLifecycle._record_wake(self.host, "connected", "test")
        await self.plugin.ingress.drain("wake")
        rows = (await self.plugin.storage.call("query", source="wake"))["items"]
        accepted = next(r for r in rows if r["type"] == "wake.accepted")
        self.assertEqual(accepted["record"]["data"]["keyword"], "configured phrase")
        self.assertEqual(sum(r["type"] == "wake.connected" for r in rows), 1)

    async def test_disabling_wake_drains_accepted_edges_without_reopening_gap(self):
        await self.configure_wake()
        reporter = self.host.wake_reporter
        self.assertTrue(reporter.offer(type="wake.accepted", subject="assistant", data={}).accepted)
        await self.plugin.request(EventRequest("enable", {"id": "wake", "revision": 1, "enabled": False}))
        rows = (await self.plugin.storage.call("query", source="wake"))["items"]
        self.assertTrue(any(r["type"] == "wake.accepted" for r in rows))
        status = await self.plugin.storage.call("status")
        self.assertIsNotNone(status["coverage"][0]["gap_start"])
        self.assertEqual(self.sources.health()["wake"]["state"], "disabled")
        self.assertFalse(reporter.offer(type="wake.accepted", subject="assistant", data={}).accepted)
        self.assertIsNone(self.host.wake_reporter)

    async def test_reconfigure_survives_caller_disconnect_and_only_changes_target(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        original = self.sources.registry.update

        def delayed(*args):
            entered.set()
            release.wait(2)
            return original(*args)

        self.sources.registry.update = delayed
        task = asyncio.create_task(
            self.plugin.request(
                EventRequest(
                    "configure",
                    {
                        "id": "wake",
                        "revision": 0,
                        "definition": {"adapter": "wake", "enabled": True, "config": {}},
                    },
                )
            )
        )
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.sources.registry.revision, 1)
        self.assertIn("wake", self.sources.health())
        self.assertTrue(self.sources.registry.sources["wake"]["enabled"])

    async def test_mcp_schema_and_foreground_background_registration(self):
        async with Client(create_server(SimpleNamespace(invoke=self.plugin.request))) as client:
            self.assertEqual({t.name for t in await client.list_tools()}, set(EVENT_TOOLS))
            result = await client.call_tool("event_status", {})
            self.assertIn("coverage", result.structured_content)
        for run in (None, "run"):
            config = server_config(
                "/tmp/test.sock",
                task_run=run,
                plugins=["memory", "devices", "ai_usage"] + (["events"] if True else []),
            )
            self.assertNotIn("enabled_tools", config["events"])
            self.assertEqual("--task-run" in config["events"]["args"], run is not None)
        self.assertNotIn("events", server_config("/tmp/test.sock"))

    async def test_ipc_wire_routes_request_and_task_identity(self):
        client = LocalRuntimeClient("/tmp/nonexistent.sock")
        client.request = AsyncMock(return_value={})
        await client.invoke(EventRequest("query", {"limit": 3}, "run"))
        client.request.assert_awaited_once_with(
            {"command": "events", "action": "query", "arguments": {"limit": 3}, "caller_run": "run"}
        )
