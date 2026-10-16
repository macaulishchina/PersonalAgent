"""Durable events -> transactional task inbox -> bounded execution trace."""

import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from assistant_runtime.config import Config
from assistant_runtime.store import Store
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.kernel.contracts import EventRequest, TaskRequest
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.plugins.events.plugin import EventsPlugin
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
from assistant_runtime.plugins.tasks.event_inbox import validate_subscription, EventInbox
from assistant_runtime.plugins.codex.jobs import tool_trace
from assistant_runtime.workfiles import WorkFiles


class Dummy:
    def __init__(self, name):
        self.manifest = Manifest(name)

    async def start(self, context):
        pass


class TaskEventsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        await self.open_runtime()

    async def open_runtime(self):
        self.store = Store(self.root / "state.sqlite")
        self.config = Config(root=self.root, workspace=str(self.root), plugins=["tasks", "events"])
        self.executor = SimpleNamespace(
            select=AsyncMock(side_effect=lambda model, effort: {"model": model, "reasoning_effort": effort}),
            launch=AsyncMock(),
            reconcile=AsyncMock(return_value=("unknown", "not replayed")),
            stop=AsyncMock(),
            finalize=AsyncMock(),
        )
        self.runtime = Runtime(self.store)
        self.host = SimpleNamespace(
            config=self.config,
            store=self.store,
            runtime=self.runtime,
            backend=SimpleNamespace(resolved_model="test", resolved_reasoning_effort="low", jobs=None),
            task_executor=lambda notify: self.executor,
            conversation=SimpleNamespace(store=ConversationStore(self.store), session="main"),
            workfiles=WorkFiles(self.root, self.store.db),
        )
        for name in ("codex", "conversation"):
            self.runtime.add(Dummy(name))
        self.events = EventsPlugin(self.host)
        self.runtime.add(self.events)
        self.runtime.add(NotificationsPlugin(self.host))
        self.tasks = TasksPlugin(self.host)
        self.runtime.add(self.tasks)
        await self.runtime.start()
        # Deterministic consumer ticks; use the real runtime, SQLite and recorder.
        workers = list(self.runtime.contexts["tasks"].tasks)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.inbox = self.tasks.event_inbox
        self.reporter = self.runtime.contexts["events"].event_reporter("sensor")

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.tmp.cleanup()

    async def create(self, **overrides):
        event = {
            "source": "records",
            "source_id": "sensor",
            "types": ["presence.changed"],
            "merge_seconds": 0,
            "cooldown_seconds": 0,
            **overrides,
        }
        return await self.tasks.request(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "spec": {
                        "title": "Observe",
                        "prompt": "User authorized read-only review; do not act.",
                        "trigger": {"kind": "event", "event": event},
                    },
                },
            )
        )

    async def emit(self, key=None, **overrides):
        return await self.reporter.report(
            type="presence.changed",
            subject="room",
            data={"present": True},
            event_id=key or uuid.uuid4().hex,
            **overrides,
        )

    async def test_create_tail_skip_baseline_and_coalesce_then_buffer_while_running(self):
        await self.emit("history")
        d = await self.create()
        self.assertFalse(d["spec"]["notify_completion"])
        await self.emit("baseline", evidence="baseline")
        first = await self.emit("one")
        await self.emit("two")
        await self.inbox.cycle()
        runs = self.tasks.repo.runs(d["id"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["data"]["trigger_context"]["count"], 2)
        self.assertIn(first["id"], [r["id"] for r in run["data"]["trigger_context"]["events"]])
        self.tasks.repo.update_run(run["id"], "running")
        await self.emit("during-run")
        await self.inbox.cycle()
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 1)
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 1)
        self.tasks.repo.update_run(run["id"], "completed")
        await self.inbox.cycle()
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 2)

    async def test_duplicate_reports_restart_and_atomic_cursor_assignment(self):
        d = await self.create()
        at = time.time()
        await self.emit("same", occurred_at=at)
        await self.emit("same", occurred_at=at)
        original = self.tasks.repo.enqueue

        def fail_after_enqueue(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Injected transaction failure")

        cursor = self.inbox.describe(d["id"])["subscription"]["cursor"]
        with patch.object(self.tasks.repo, "enqueue", side_effect=fail_after_enqueue):
            await self.inbox.cycle()
        self.assertEqual(self.tasks.repo.runs(d["id"]), [])
        self.assertEqual(self.inbox.describe(d["id"])["subscription"]["cursor"], cursor)
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 0)
        restarted = EventInbox(self.tasks)
        with self.store.db:
            self.store.db.execute("UPDATE job_subscriptions SET retry_at=0")
        await restarted.cycle()
        await restarted.cycle()
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 1)
        self.assertEqual(restarted.describe(d["id"])["counts"]["assigned"], 1)

    async def test_corrections_deletions_expiry_and_no_revival(self):
        d = await self.create(merge_seconds=10)
        entry = await self.emit("delete")
        await self.inbox.cycle()
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 1)
        await self.runtime.invoke(EventRequest("delete", {"id": entry["id"], "revision": 1}))
        await self.inbox.cycle()
        self.assertEqual(self.inbox.describe(d["id"])["counts"]["invalidated"], 1)
        changed = await self.emit("correct")
        await self.inbox.cycle()
        rec = (await self.runtime.invoke(EventRequest("get", {"id": changed["id"]})))["record"]
        rec.update(evidence="user_statement", data={"present": False})
        await self.runtime.invoke(
            EventRequest("correct", {"id": changed["id"], "revision": 1, "record": rec})
        )
        await self.emit("old", occurred_at=time.time() - 1000)
        await self.emit("future", occurred_at=time.time() + 60)
        await self.inbox.cycle()
        status = self.inbox.describe(d["id"])
        self.assertEqual(status["pending_count"], 0)
        self.assertEqual(status["counts"]["invalidated"], 2)
        self.assertEqual(status["counts"]["expired"], 1)
        self.assertEqual(status["counts"]["future_timestamp"], 1)
        self.assertFalse(self.tasks.repo.runs(d["id"]))

    async def test_bounded_inbox_overflow_and_merge_cooldown(self):
        d = await self.create(capacity=2, merge_seconds=5, cooldown_seconds=30)
        for _ in range(3):
            await self.emit()
        await self.inbox.cycle()
        self.assertFalse(self.tasks.repo.runs(d["id"]))
        self.assertEqual(self.inbox.describe(d["id"])["counts"]["overflow"], 1)
        with self.store.db:
            self.store.db.execute("UPDATE job_inbox SET received=received-6")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(d["id"])[0]
        self.tasks.repo.update_run(run["id"], "completed")
        await self.emit()
        with self.store.db:
            self.store.db.execute("UPDATE job_inbox SET received=received-6")
        await self.inbox.cycle()
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 1)
        self.assertLessEqual(self.inbox.describe(d["id"])["pending_count"], 2)

    async def test_pause_resume_and_edit_do_not_replay_or_use_old_page(self):
        d = await self.create(merge_seconds=10)
        await self.emit()
        await self.inbox.cycle()
        await self.tasks.request(TaskRequest("pause", {"task_id": d["id"]}))
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 0)
        await self.emit("while-paused")
        await self.tasks.request(TaskRequest("resume", {"task_id": d["id"]}))
        await self.inbox.cycle()
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 0)
        await self.emit("before-edit")
        original = self.inbox.call

        async def edit_during_read(action, **arguments):
            result = await original(action, **arguments)
            if action == "changes":
                current = self.tasks.repo.definition(d["id"])
                await self.tasks.request(
                    TaskRequest(
                        "update",
                        {
                            "task_id": d["id"],
                            "revision": current["revision"],
                            "changes": {"prompt": "Updated authorized read-only goal"},
                        },
                    )
                )
            return result

        with patch.object(self.inbox, "call", side_effect=edit_during_read):
            await self.inbox.cycle()
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 0)
        self.assertFalse(self.tasks.repo.runs(d["id"]))

    async def test_queued_revalidation_and_unavailable_recorder_do_not_execute(self):
        d = await self.create()
        e = await self.emit()
        await self.inbox.cycle()
        run = self.tasks.repo.runs(d["id"])[0]
        with patch.object(self.inbox, "call", side_effect=RuntimeError("Unavailable")):
            await self.tasks.launch(run)
        self.assertEqual(self.tasks.repo.run(run["id"])["state"], "queued")
        self.executor.launch.assert_not_awaited()
        await self.runtime.invoke(EventRequest("delete", {"id": e["id"], "revision": 1}))
        await self.tasks.launch(self.tasks.repo.run(run["id"]))
        self.assertEqual(self.tasks.repo.run(run["id"])["state"], "missed")
        self.executor.launch.assert_not_awaited()

    async def test_unknown_run_blocks_followup_and_execution_receives_context(self):
        d = await self.create()
        await self.emit()
        await self.inbox.cycle()
        run = self.tasks.repo.runs(d["id"])[0]
        await self.tasks.launch(run)
        sent = self.executor.launch.await_args.args[0]
        self.assertEqual(len(sent["data"]["trigger_context"]["valid_events"]), 1)
        self.tasks.repo.update_run(run["id"], "unknown")
        await self.emit()
        await self.inbox.cycle()
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 1)
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 1)

    async def test_trace_is_deduplicated_bounded_paginated_and_redacted(self):
        d = await self.create()
        await self.emit()
        await self.inbox.cycle()
        run = self.tasks.repo.runs(d["id"])[0]
        event = {
            "turnId": "turn",
            "item": {
                "id": "tool",
                "type": "mcpToolCall",
                "server": "device",
                "tool": "set_audio_volume",
                "arguments": {"device_id": "speaker", "operation_id": "op", "secret": "never-store"},
                "result": {"private": "never-store"},
                "status": "completed",
            },
        }
        trace = tool_trace("item/completed", event)
        self.tasks.provider_update(run["id"], None, tool_trace=trace)
        self.tasks.provider_update(run["id"], None, tool_trace=trace)
        result = await self.tasks.request(TaskRequest("trace", {"run_id": run["id"]}))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["operation_id"], "op")
        self.assertNotIn("never-store", str(result))
        for index in range(501):
            self.tasks.trace.append(run["id"], {**trace, "call_id": str(index)})
        page = self.tasks.trace.read({"run_id": run["id"], "limit": 100})
        self.assertEqual(len(page["items"]), 100)
        self.assertTrue(page["truncated"])
        self.assertIsNotNone(page["next"])

    def test_subscription_rejects_invalid_unbounded_policy(self):
        good = {"source": "records", "types": ["presence.changed"]}
        for extra in (
            {"capacity": 0},
            {"capacity": 257},
            {"merge_seconds": 300},
            {"max_age_seconds": float("nan")},
            {"types": []},
            {"predicate": "arbitrary code"},
        ):
            with self.assertRaises(ValueError):
                validate_subscription({**good, **extra})

    async def test_real_runtime_restart_preserves_pending_cursor_and_unknown_run(self):
        d = await self.create(merge_seconds=10)
        e = await self.emit()
        await self.inbox.cycle()
        cursor = self.inbox.describe(d["id"])["subscription"]["cursor"]
        await self.runtime.close()
        self.store.db.close()
        await self.open_runtime()
        self.assertEqual(self.inbox.describe(d["id"])["subscription"]["cursor"], cursor)
        self.assertEqual(self.inbox.describe(d["id"])["events"][0]["id"], e["id"])
        with self.store.db:
            self.store.db.execute("UPDATE job_inbox SET received=received-11")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(d["id"])[0]
        self.tasks.repo.update_run(run["id"], "running")
        await self.runtime.close()
        self.store.db.close()
        await self.open_runtime()
        self.assertEqual(self.tasks.repo.run(run["id"])["state"], "unknown")
        self.executor.launch.assert_not_awaited()

    async def test_multi_page_backlog_and_pending_expiry(self):
        d = await self.create(capacity=128, merge_seconds=60)
        for i in range(57):
            await self.emit(str(i))
        self.assertTrue(await self.inbox.cycle())
        self.assertFalse(await self.inbox.cycle())
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 57)
        with self.store.db:
            self.store.db.execute("UPDATE job_inbox SET expires=0")
        await self.inbox.cycle()
        self.assertEqual(self.inbox.describe(d["id"])["pending_count"], 0)
        self.assertEqual(self.inbox.describe(d["id"])["counts"]["expired"], 57)

    async def test_log_regression_is_not_silent_replay(self):
        d = await self.create()
        await self.emit()
        await self.inbox.cycle()
        with patch.object(
            self.inbox, "call", return_value={"head": 0, "next": 0, "items": [], "has_more": False}
        ):
            await self.inbox.cycle()
        self.assertIn("moved backwards", self.inbox.describe(d["id"])["subscription"]["error"])
        self.assertEqual(len(self.tasks.repo.runs(d["id"])), 1)

    async def test_consumer_recovers_without_commit_hints_and_runs_in_background(self):
        d = await self.create()
        # No hint listener: the durable log must recover a dropped bus notification.
        context = self.runtime.contexts["tasks"]
        context.spawn(self.inbox.run(), "acceptance-consumer")
        context.spawn(self.tasks.worker(), "acceptance-dispatch")
        await self.emit("without-hint")
        async with asyncio.timeout(4):
            while not self.executor.launch.await_count:
                await asyncio.sleep(0.02)
        self.assertEqual(self.executor.launch.await_count, 1)
        self.assertEqual(self.executor.launch.await_args.args[0]["task_id"], d["id"])
        self.assertEqual(self.inbox.describe(d["id"])["counts"]["assigned"], 1)
        # Let another tick happen: a dispatched run cannot be automatically repeated.
        await asyncio.sleep(1.1)
        self.assertEqual(self.executor.launch.await_count, 1)
