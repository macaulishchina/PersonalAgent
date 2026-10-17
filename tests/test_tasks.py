"""Durable tasks: duplicate dispatch, isolation, recovery and cleanup boundaries."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastmcp import Client
from assistant_runtime.config import Config
from assistant_runtime.store import Store
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.kernel.contracts import TaskRequest, DeviceQuery, NotificationRequest
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.mcp.tasks import create_server
from assistant_runtime.mcp.catalog import TASK_TOOLS
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
from assistant_runtime.plugins.tasks.schedules import next_time
from assistant_runtime.plugins.codex.jobs import CodexJobs
from assistant_runtime.workfiles import WorkFiles


class Dummy:
    def __init__(self, name):
        self.manifest = Manifest(name)

    async def start(self, context):
        pass


class TasksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state.sqlite")
        self.config = Config(root=self.root, workspace=str(self.root), plugins=["codex", "tasks"])
        self.chat = ConversationStore(self.store)
        self.executor = SimpleNamespace(
            models=AsyncMock(
                return_value=[
                    {
                        "model": model,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort} for effort in ("low", "medium", "high")
                        ],
                    }
                    for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")
                ]
            ),
            select=AsyncMock(side_effect=lambda model, effort: {"model": model, "reasoning_effort": effort}),
            launch=AsyncMock(),
            finalize=AsyncMock(),
            reconcile=AsyncMock(return_value=("unknown", "not replayed")),
            supplement=AsyncMock(),
            stop=AsyncMock(),
        )
        self.host = SimpleNamespace(
            config=self.config,
            store=self.store,
            backend=SimpleNamespace(
                resolved_model="test-model",
                resolved_reasoning_effort="low",
                jobs=None,
                import_voice_messages=AsyncMock(),
            ),
            task_executor=lambda notify: self.executor,
            conversation=SimpleNamespace(store=self.chat, session="main"),
            voice=SimpleNamespace(opened=False, append_context=AsyncMock(), say=AsyncMock()),
            workfiles=WorkFiles(self.root, self.store.db),
            devices=SimpleNamespace(
                snapshot=lambda ident: {"state": "available", "fresh": True, "readings": {}}
            ),
            quiet_active=lambda: False,
        )
        self.plugin = TasksPlugin(self.host)
        self.runtime = Runtime(self.store)
        for name in ("codex", "conversation"):
            self.runtime.add(Dummy(name))
        self.runtime.add(NotificationsPlugin(self.host))
        self.host.runtime = self.runtime
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.tmp.cleanup()

    async def create(self, **spec):
        return await self.plugin.request(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "spec": {"title": "test", "prompt": "authorized task", **spec},
                },
            )
        )

    async def test_definition_pages_are_bounded_stable_and_search_all_tasks(self):
        definitions = [
            await self.create(title=f"项目 {number}", trigger={"kind": "interval", "seconds": 3600})
            for number in range(47)
        ]
        with self.store.db:
            self.store.db.execute("UPDATE job_definitions SET created=100")
        with patch.object(self.plugin.repo, "definition", wraps=self.plugin.repo.definition) as decode:
            page = await self.plugin.request(TaskRequest("list", {"view": "planned"}))
            self.assertEqual(len(page["tasks"]), 20)
            self.assertEqual(decode.call_count, 20)
        ids = [d["id"] for d in page["tasks"]]
        # A concurrent new task must not displace anything across the old cursor.
        extra = await self.create(title="new", trigger={"kind": "interval", "seconds": 3600})
        while page["before"]:
            page = await self.plugin.request(
                TaskRequest("list", {"view": "planned", "before": page["before"]})
            )
            self.assertLessEqual(len(page["tasks"]), 20)
            ids.extend(d["id"] for d in page["tasks"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {d["id"] for d in definitions})
        self.assertNotIn(extra["id"], ids)
        page = await self.plugin.request(TaskRequest("list", {"query": "项目 46"}))
        self.assertEqual([d["id"] for d in page["tasks"]], [definitions[-1]["id"]])
        self.assertIsNone(page["before"])

    async def test_page_views_literal_search_and_changed_filter_removal(self):
        planned = await self.create(title="Watch 100%", trigger={"kind": "interval", "seconds": 3600})
        temporary = await self.create()
        archived = await self.create()
        self.plugin.repo.update_definition(archived["id"], state="archived")
        for view, expected in (("planned", planned), ("temporary", temporary)):
            page = await self.plugin.request(TaskRequest("list", {"view": view}))
            self.assertEqual([d["id"] for d in page["tasks"]], [expected["id"]])
        all_temporary = await self.plugin.request(
            TaskRequest("list", {"view": "temporary", "include_archived": True})
        )
        self.assertEqual({d["id"] for d in all_temporary["tasks"]}, {temporary["id"], archived["id"]})
        page = await self.plugin.request(TaskRequest("list", {"query": "100%"}))
        self.assertEqual([d["id"] for d in page["tasks"]], [planned["id"]])
        self.plugin.repo.update_definition(temporary["id"], state="archived")
        page = await self.plugin.request(
            TaskRequest("list", {"view": "temporary", "known": [temporary["id"]]})
        )
        self.assertEqual(page["removed"], [temporary["id"]])
        self.assertEqual(page["tasks"], [])

    async def test_known_older_task_refreshes_beyond_head_page_and_reports_archival(self):
        old = await self.create(title="older active plan", trigger={"kind": "interval", "seconds": 3600})
        run = self.plugin.repo.enqueue(old, "older-execution", "running")
        for number in range(8):
            await self.create(title=f"newer {number}", trigger={"kind": "interval", "seconds": 3600})
        args = {"view": "planned", "limit": 3, "known": [old["id"]]}
        page = await self.plugin.request(TaskRequest("list", args))
        self.assertNotIn(old["id"], {task["id"] for task in page["tasks"]})
        self.assertEqual([task["id"] for task in page["updates"]], [old["id"]])
        self.assertEqual(next(r for r in page["runs"] if r["id"] == run["id"])["state"], "running")
        self.plugin.repo.update_run(run["id"], "completed", progress="older work finished")
        finished = await self.plugin.request(TaskRequest("list", args))
        self.assertEqual(finished["before"], page["before"])
        self.assertEqual(next(r for r in finished["runs"] if r["id"] == run["id"])["state"], "completed")
        self.plugin.repo.update_definition(old["id"], state="archived")
        archived = await self.plugin.request(TaskRequest("list", args))
        self.assertEqual(archived["removed"], [old["id"]])
        self.assertEqual(archived["updates"], [])
        self.assertNotIn(run["id"], {r["id"] for r in archived["runs"]})

    async def test_active_plan_filter_includes_future_event_and_unfinished_only(self):
        future = await self.create(trigger={"kind": "interval", "seconds": 3600})
        inactive = []
        for state in ("paused", "archived", "terminated"):
            task = await self.create(trigger={"kind": "interval", "seconds": 3600})
            self.plugin.repo.update_definition(task["id"], state=state)
            inactive.append(task["id"])
        event = await self.create(
            trigger={"kind": "event", "event": {"source": "task", "id": future["id"], "value": "completed"}}
        )
        ended = await self.create(long_term=True, trigger={"kind": "interval", "seconds": 3600})
        self.plugin.repo.update_definition(ended["id"], next_at=None)
        self.plugin.repo.enqueue(ended, "finished", "completed")
        inactive.append(ended["id"])
        unfinished = []
        for state in ("queued", "running", "waiting_user", "paused", "unknown"):
            task = await self.create(long_term=True, trigger={"kind": "interval", "seconds": 3600})
            self.plugin.repo.update_definition(task["id"], next_at=None)
            self.plugin.repo.enqueue(task, state, state)
            unfinished.append(task["id"])
        temporary = await self.create()
        expected = {future["id"], event["id"], *unfinished}
        default_plans = await self.plugin.request(TaskRequest("list", {"view": "planned"}))
        self.assertEqual(
            {d["id"] for d in default_plans["tasks"]},
            expected
            | {ident for ident in inactive if self.plugin.repo.definition(ident)["state"] != "archived"},
        )
        all_plans = await self.plugin.request(
            TaskRequest("list", {"view": "planned", "include_archived": True})
        )
        active = await self.plugin.request(TaskRequest("list", {"view": "planned", "active_only": True}))
        self.assertEqual({d["id"] for d in all_plans["tasks"]}, expected | set(inactive))
        self.assertEqual({d["id"] for d in active["tasks"]}, expected)
        self.assertNotIn(temporary["id"], {d["id"] for d in all_plans["tasks"]})
        self.plugin.repo.update_definition(future["id"], state="paused")
        refresh = await self.plugin.request(
            TaskRequest("list", {"view": "planned", "active_only": True, "known": [future["id"]]})
        )
        self.assertEqual(refresh["removed"], [future["id"]])

    async def test_temporary_archive_checkbox_filters_every_page(self):
        active_ids, all_ids = set(), set()
        for number in range(47):
            task = await self.create(title=f"delegation {number}")
            all_ids.add(task["id"])
            if number % 2:
                self.plugin.repo.update_definition(task["id"], state="archived")
            else:
                active_ids.add(task["id"])
        ended = await self.create(auto_archive=False)
        self.plugin.repo.update_definition(ended["id"], next_at=None)
        self.plugin.repo.enqueue(ended, "finished", "failed")
        all_ids.add(ended["id"])
        for include, expected in ((False, active_ids), (True, all_ids)):
            seen, before = [], None
            while True:
                args = {"view": "temporary", "include_archived": include, "limit": 7}
                if before:
                    args["before"] = before
                page = await self.plugin.request(TaskRequest("list", args))
                self.assertLessEqual(len(page["tasks"]), 7)
                seen.extend(d["id"] for d in page["tasks"])
                before = page["before"]
                if not before:
                    break
            self.assertEqual(set(seen), expected)
            self.assertEqual(len(seen), len(expected))
        page = await self.plugin.request(TaskRequest("list", {"view": "temporary", "limit": 7}))
        with self.assertRaisesRegex(ValueError, "cursor"):
            await self.plugin.request(
                TaskRequest("list", {"view": "temporary", "include_archived": True, "before": page["before"]})
            )
        plans = [await self.create(trigger={"kind": "interval", "seconds": 3600}) for _ in range(3)]
        page = await self.plugin.request(TaskRequest("list", {"view": "planned", "limit": 1}))
        with self.assertRaisesRegex(ValueError, "cursor"):
            await self.plugin.request(
                TaskRequest("list", {"view": "planned", "active_only": True, "before": page["before"]})
            )
        self.assertTrue(plans)

    async def test_planned_archive_filter_applies_to_pages_search_and_known_ids(self):
        non_archived, all_ids = set(), set()
        states = ("enabled", "paused", "terminated", "archived")
        for number in range(47):
            task = await self.create(title=f"plan [{number}]", trigger={"kind": "interval", "seconds": 3600})
            state = states[number % len(states)]
            self.plugin.repo.update_definition(task["id"], state=state)
            all_ids.add(task["id"])
            if state != "archived":
                non_archived.add(task["id"])
        for include, expected in ((False, non_archived), (True, all_ids)):
            seen, before = [], None
            while True:
                args = {"view": "planned", "include_archived": include, "limit": 7}
                if before:
                    args["before"] = before
                page = await self.plugin.request(TaskRequest("list", args))
                self.assertLessEqual(len(page["tasks"]), 7)
                seen.extend(d["id"] for d in page["tasks"])
                before = page["before"]
                if not before:
                    break
            self.assertEqual(set(seen), expected)
            self.assertEqual(len(seen), len(expected))
        page = await self.plugin.request(TaskRequest("list", {"view": "planned", "limit": 7}))
        with self.assertRaisesRegex(ValueError, "cursor"):
            await self.plugin.request(
                TaskRequest("list", {"view": "planned", "include_archived": True, "before": page["before"]})
            )
        archived_id = next(iter(all_ids - non_archived))
        archived = self.plugin.repo.definition(archived_id)
        for include, expected in ((False, []), (True, [archived_id])):
            page = await self.plugin.request(
                TaskRequest(
                    "list",
                    {
                        "view": "planned",
                        "include_archived": include,
                        "query": archived["spec"]["title"],
                        "state": "archived",
                    },
                )
            )
            self.assertEqual([t["id"] for t in page["tasks"]], expected)
        ident = next(iter(non_archived))
        self.plugin.repo.update_definition(ident, state="archived")
        page = await self.plugin.request(TaskRequest("list", {"view": "planned", "known": [ident]}))
        self.assertEqual(page["removed"], [ident])

    async def test_run_pages_keep_old_active_and_refresh_its_terminal_result(self):
        task = await self.create(trigger={"kind": "interval", "seconds": 3600})
        old = self.plugin.repo.enqueue(task, "old", "running")
        ids = {old["id"]}
        for number in range(46):
            ids.add(self.plugin.repo.enqueue(task, f"skipped:{number}", "missed")["id"])
        with self.store.db:
            self.store.db.execute("UPDATE job_runs SET created=100 WHERE id!=?", (old["id"],))
            self.store.db.execute("UPDATE job_runs SET created=90 WHERE id=?", (old["id"],))
        page = await self.plugin.request(TaskRequest("get", {"task_id": task["id"]}))
        self.assertEqual(len(page["runs"]), 20)
        self.assertEqual([r["id"] for r in page["active_runs"]], [old["id"]])
        seen = [r["id"] for r in page["runs"]]
        cursor = page["before"]
        while cursor:
            page = await self.plugin.request(TaskRequest("get", {"task_id": task["id"], "before": cursor}))
            seen.extend(r["id"] for r in page["runs"])
            cursor = page["before"]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), ids)
        self.plugin.repo.update_run(old["id"], "failed", progress="old execution finished")
        page = await self.plugin.request(TaskRequest("get", {"task_id": task["id"], "known": [old["id"]]}))
        self.assertEqual(page["active_runs"], [])
        self.assertEqual(page["updates"][0]["state"], "failed")
        self.assertEqual(page["updates"][0]["data"]["progress"], "old execution finished")
        summaries = await self.plugin.request(TaskRequest("list"))
        self.assertLessEqual(len(summaries["runs"]), 2)
        self.assertNotIn("spec", summaries["runs"][0]["data"])

    async def test_page_rejects_bad_limits_and_cross_query_cursors(self):
        for _ in range(3):
            await self.create()
        first = await self.plugin.request(TaskRequest("list", {"limit": 1, "view": "temporary"}))
        for args in (
            {"limit": 0},
            {"limit": 51},
            {"limit": True},
            {"before": "broken"},
            {"view": "other"},
            {"query": "x" * 501},
            {"known": ["bad"]},
            {"view": "temporary", "include_archived": "true"},
            {"view": "planned", "active_only": 1},
            {"view": "all", "include_archived": True},
            {"view": "temporary", "active_only": True},
            {"view": "history", "before": first["before"]},
            {"query": "different", "view": "temporary", "before": first["before"]},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.plugin.request(TaskRequest("list", args))
        tasks = self.plugin.repo.definitions()
        for task in tasks:
            for number in range(2):
                self.plugin.repo.enqueue(task, str(number), "missed")
        page = await self.plugin.request(TaskRequest("get", {"task_id": tasks[0]["id"], "limit": 1}))
        with self.assertRaises(ValueError):
            await self.plugin.request(
                TaskRequest("get", {"task_id": tasks[1]["id"], "before": page["before"]})
            )

    async def test_temporary_tasks_archive_for_every_terminal_outcome(self):
        for outcome in ("completed", "failed", "canceled", "missed"):
            d = await self.create(notify_completion=False)
            self.plugin.due(d["id"])
            run = self.plugin.repo.runs(d["id"])[0]
            area = self.host.workfiles.area(self.plugin.owner(run))
            artifact = area / "artifacts/result.txt"
            artifact.write_text(outcome)
            self.plugin.repo.update_run(run["id"], outcome, progress="retained result")
            self.plugin.archive_finished({d["id"]})
            saved = self.plugin.repo.definition(d["id"])
            self.assertEqual(saved["category"], "temporary")
            self.assertEqual(saved["state"], "archived")
            self.assertIsNone(saved["next_at"])
            self.assertEqual(self.plugin.repo.run(run["id"])["state"], outcome)
            self.assertEqual(self.plugin.repo.run(run["id"])["data"]["progress"], "retained result")
            self.assertEqual(artifact.read_text(), outcome)
        self.executor.stop.assert_not_awaited()
        self.assertEqual(self.host.notifications.repo.list()["items"], [])

    async def test_unfinished_temporary_tasks_remain_visible(self):
        for state in ("queued", "dispatching", "running", "waiting_user", "paused", "unknown"):
            d = await self.create(notify_completion=False)
            self.plugin.due(d["id"])
            run = self.plugin.repo.runs(d["id"])[0]
            self.plugin.repo.update_run(run["id"], state)
            self.plugin.archive_finished({d["id"]})
            self.assertEqual(self.plugin.repo.definition(d["id"])["state"], "enabled")

    async def test_archive_defaults_distinguish_temporary_scheduled_and_goal_work(self):
        d = await self.create()
        self.assertTrue(d["spec"]["auto_archive"])
        self.assertEqual(d["category"], "temporary")
        scheduled = await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": d["id"],
                    "revision": d["revision"],
                    "changes": {"trigger": {"kind": "interval", "seconds": 1800}},
                },
            )
        )
        self.assertFalse(scheduled["spec"]["auto_archive"])
        self.assertEqual(scheduled["category"], "planned")
        goal = await self.create(long_term=True)
        self.assertEqual(goal["category"], "planned")
        self.assertFalse(goal["spec"]["auto_archive"])
        kept = await self.create(auto_archive=False)
        self.plugin.due(kept["id"])
        run = self.plugin.repo.runs(kept["id"])[0]
        self.plugin.repo.update_run(run["id"], "failed")
        self.plugin.archive_finished({kept["id"]})
        self.assertEqual(self.plugin.repo.definition(kept["id"])["state"], "enabled")
        with self.assertRaises(ValueError):
            await self.create(trigger={"kind": "interval", "seconds": 10}, auto_archive=True)

    async def test_recovery_sweep_archives_existing_terminal_one_shots_once(self):
        import json

        d = await self.create(notify_completion=False)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "failed", completion_delivered=True)
        spec = dict(d["spec"])
        spec.pop("auto_archive")
        with self.store.db:
            self.store.db.execute("UPDATE job_definitions SET spec=? WHERE id=?", (json.dumps(spec), d["id"]))
        self.plugin.archive_finished()
        archived = self.plugin.repo.definition(d["id"])
        self.assertEqual(archived["state"], "archived")
        self.plugin.archive_finished()
        self.assertEqual(self.plugin.repo.definition(d["id"])["revision"], archived["revision"])
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("continue", {"run_id": run["id"], "text": "again"}))

    async def test_auto_archive_waits_for_children_without_canceling_them(self):
        parent = await self.create(notify_completion=False)
        self.plugin.due(parent["id"])
        parent_run = self.plugin.repo.runs(parent["id"])[0]
        self.plugin.repo.update_run(parent_run["id"], "running")
        child = await self.plugin.request(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "spec": {
                        "title": "child",
                        "prompt": "within authorized scope",
                        "notify_completion": False,
                    },
                },
                parent_run["id"],
            )
        )
        self.plugin.due(child["id"])
        child_run = self.plugin.repo.runs(child["id"])[0]
        self.plugin.repo.update_run(child_run["id"], "running")
        self.plugin.repo.update_run(parent_run["id"], "completed")
        self.plugin.archive_finished({parent["id"]})
        self.assertEqual(self.plugin.repo.definition(parent["id"])["state"], "enabled")
        self.executor.stop.assert_not_awaited()
        self.plugin.repo.update_run(child_run["id"], "failed")
        self.plugin.archive_finished({child["id"]})
        self.assertEqual(self.plugin.repo.definition(child["id"])["state"], "archived")
        self.assertEqual(self.plugin.repo.definition(parent["id"])["state"], "archived")
        self.executor.stop.assert_not_awaited()

    async def test_completed_goal_child_does_not_keep_temporary_parent_open(self):
        parent = await self.create(notify_completion=False)
        self.plugin.due(parent["id"])
        run = self.plugin.repo.runs(parent["id"])[0]
        self.plugin.repo.update_run(run["id"], "running")
        child = await self.plugin.request(
            TaskRequest(
                "create",
                {
                    "request_id": str(uuid.uuid4()),
                    "spec": {
                        "title": "scoped goal",
                        "prompt": "authorized part",
                        "long_term": True,
                        "notify_completion": False,
                    },
                },
                run["id"],
            )
        )
        self.plugin.due(child["id"])
        child_run = self.plugin.repo.runs(child["id"])[0]
        self.plugin.repo.update_run(child_run["id"], "completed", goal={"status": "complete"})
        self.plugin.repo.update_run(run["id"], "completed")
        self.plugin.archive_finished({parent["id"]})
        self.assertEqual(self.plugin.repo.definition(parent["id"])["state"], "archived")
        self.executor.stop.assert_not_awaited()

    async def test_archival_does_not_drop_pending_result_notification(self):
        original = self.host.notifications.repo.publish

        def failed(*args, **kwargs):
            raise RuntimeError("outbox temporarily unavailable")

        self.host.notifications.repo.publish = failed
        d = await self.create(execution={"kind": "reminder"})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.launch(run)
        await asyncio.sleep(0.7)
        self.assertEqual(self.plugin.repo.definition(d["id"])["state"], "archived")
        self.assertFalse(self.plugin.repo.run(run["id"])["data"].get("completion_delivered", False))
        self.host.notifications.repo.publish = original
        for _ in range(20):
            notices = self.host.notifications.repo.list()["items"]
            if notices:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["payload"]["body"], "authorized task")

    async def test_create_is_idempotent_and_conflicts_are_rejected(self):
        args = {
            "request_id": str(uuid.uuid4()),
            "spec": {
                "title": "same",
                "prompt": "one operation",
                "trigger": {"kind": "date", "at": "2030-01-01T00:00:00+08:00"},
            },
        }
        first = await self.plugin.request(TaskRequest("create", args))
        second = await self.plugin.request(TaskRequest("create", args))
        self.assertEqual(first["id"], second["id"])
        args["spec"]["prompt"] = "different operation"
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("create", args))
        self.assertEqual(len(self.plugin.repo.definitions()), 1)

    async def test_auto_selects_at_creation_and_fixed_settings_override(self):
        d = await self.create(
            execution={"model": "fast", "reasoning_effort": "medium", "reason": "short work"}
        )
        self.assertEqual(d["spec"]["execution"]["model"], "fast")
        self.plugin.settings.execution = {"model": "fixed", "reasoning_effort": "high"}
        d = await self.create(execution={"model": "fast", "reasoning_effort": "low"})
        self.assertEqual(d["spec"]["execution"]["model"], "fixed")
        self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "high")
        self.assertEqual(d["spec"]["execution"]["kind"], "codex")

    async def test_reminders_do_not_call_models(self):
        d = await self.create(execution={"kind": "reminder"})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.launch(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "completed")
        self.executor.select.assert_not_called()
        self.executor.launch.assert_not_called()
        self.host.voice.say.assert_not_called()

    async def test_device_query_accepts_device_id_without_model(self):
        self.config.devices["sensor"] = {"enabled": True}
        self.runtime.register(
            "conversation",
            DeviceQuery,
            AsyncMock(return_value={"temperature": 23}),
        )
        d = await self.create(execution={"kind": "device_query", "device_id": "sensor"})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.launch(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "completed")
        self.assertIn("23", self.plugin.repo.run(run["id"])["data"]["progress"])
        self.executor.select.assert_not_called()
        self.executor.launch.assert_not_called()

    async def test_old_active_run_stays_visible_and_blocks_replay_after_100_misses(self):
        d = await self.create(trigger={"kind": "interval", "seconds": 10})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "running", thread_id="old", turn_id="old-turn")
        with self.store.db:
            for number in range(105):
                self.plugin.repo.enqueue(d, f"missed:{number}", "missed")
        self.assertNotIn(run["id"], [r["id"] for r in self.plugin.repo.runs(d["id"])])
        result = await self.plugin.request(TaskRequest("list"))
        self.assertIn(run["id"], [r["id"] for r in result["runs"]])
        with self.assertRaisesRegex(ValueError, "unfinished"):
            await self.plugin.request(
                TaskRequest("run", {"task_id": d["id"], "request_id": str(uuid.uuid4())})
            )
        self.plugin.repo.update_definition(d["id"], next_at=time.time())
        self.plugin.due(d["id"])
        self.assertEqual(self.plugin.repo.runs(d["id"])[0]["state"], "missed")
        await self.plugin.request(TaskRequest("terminate", {"task_id": d["id"]}))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "canceled")
        self.executor.stop.assert_awaited_once()

    async def test_cancel_during_model_selection_does_not_launch(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        started, release = asyncio.Event(), asyncio.Event()

        async def select(*args):
            started.set()
            await release.wait()

        self.executor.select.side_effect = select
        launch = asyncio.create_task(self.plugin.launch(run))
        await started.wait()
        await self.plugin.request(TaskRequest("cancel_run", {"run_id": run["id"]}))
        release.set()
        await launch
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "canceled")
        self.executor.launch.assert_not_awaited()

    async def test_concurrent_definition_change_is_not_overwritten_after_model_validation(self):
        d = await self.create()
        started, release = asyncio.Event(), asyncio.Event()

        async def select(model, effort):
            started.set()
            await release.wait()
            return {"model": model, "reasoning_effort": effort}

        self.executor.select.side_effect = select
        update = asyncio.create_task(
            self.plugin.request(
                TaskRequest(
                    "update",
                    {"task_id": d["id"], "revision": d["revision"], "changes": {"prompt": "stale edit"}},
                )
            )
        )
        await started.wait()
        self.plugin.repo.update_definition(d["id"], state="terminated")
        release.set()
        with self.assertRaisesRegex(ValueError, "changed"):
            await update
        self.assertEqual(self.plugin.repo.definition(d["id"])["spec"]["prompt"], "authorized task")

    async def test_edit_child_preserves_lineage_for_parent_termination(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "running")
        child = await self.plugin.request(
            TaskRequest(
                "create",
                {"request_id": str(uuid.uuid4()), "spec": {"title": "child", "prompt": "scoped child"}},
                run["id"],
            )
        )
        edited = await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": child["id"],
                    "revision": child["revision"],
                    "changes": {"title": "renamed child"},
                },
            )
        )
        self.assertEqual(edited["spec"]["parent_id"], d["id"])
        self.assertEqual(edited["spec"]["depth"], 1)
        await self.plugin.request(TaskRequest("terminate", {"task_id": d["id"]}))
        self.assertEqual(self.plugin.repo.definition(child["id"])["state"], "terminated")

    async def test_continued_run_delivers_new_result_and_refreshes_artifacts(self):
        d = await self.create(execution={"kind": "reminder"})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        directory = self.host.workfiles.area(self.plugin.owner(run))
        self.plugin.repo.update_run(run["id"], "failed", completion_delivered=True, artifacts=[])
        (directory / "artifacts/result.txt").write_text("new result")
        await self.plugin.request(TaskRequest("continue", {"run_id": run["id"], "text": "try again"}))
        self.assertFalse(self.plugin.repo.run(run["id"])["data"]["completion_delivered"])
        await asyncio.sleep(1.2)
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["data"]["artifacts"][0]["name"], "result.txt")
        for _ in range(30):
            if self.host.backend.import_voice_messages.await_count:
                break
            await asyncio.sleep(0.1)
        self.host.backend.import_voice_messages.assert_awaited_once()

    async def test_duplicate_due_and_late_one_shot_never_replay(self):
        d = await self.create()
        self.plugin.due(d["id"])
        self.plugin.due(d["id"])
        self.assertEqual(len(self.plugin.repo.runs(d["id"])), 1)
        late = await self.create(trigger={"kind": "date", "at": "2020-01-01T00:00:00+08:00"})
        self.plugin.due(late["id"])
        missed = self.plugin.repo.runs(late["id"])[0]
        self.assertEqual(missed["state"], "missed")
        self.assertEqual(missed["data"]["skip_reason"], "late")
        self.assertIn("超过", missed["data"]["progress"])

    async def test_interval_coalesces_and_prevents_overlap(self):
        d = await self.create(trigger={"kind": "interval", "seconds": 10})
        self.plugin.repo.update_definition(d["id"], next_at=time.time() - 95)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.assertEqual(run["state"], "queued")
        self.plugin.repo.update_definition(d["id"], next_at=time.time())
        self.plugin.due(d["id"])
        self.assertEqual({r["state"] for r in self.plugin.repo.runs(d["id"])}, {"queued", "missed"})
        skipped = next(r for r in self.plugin.repo.runs(d["id"]) if r["state"] == "missed")
        self.assertEqual(skipped["data"]["skip_reason"], "overlap")
        self.assertEqual(skipped["data"]["blocked_by"], [run["id"]])
        self.assertIn("为避免重复", skipped["data"]["progress"])

    async def test_pause_schedule_keeps_current_work_and_cancel_is_targeted(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "running", thread_id="one", turn_id="one-turn")
        await self.plugin.request(TaskRequest("pause", {"task_id": d["id"]}))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "running")
        self.executor.stop.assert_not_awaited()
        await self.plugin.request(TaskRequest("cancel_run", {"run_id": run["id"]}))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "canceled")
        self.assertEqual(self.executor.stop.await_args.args[0]["id"], run["id"])
        self.plugin.provider_update(run["id"], "completed", progress="late ignored")
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "canceled")

    async def test_unknown_dispatch_not_retried(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.executor.launch.side_effect = TimeoutError("reply lost")
        await self.plugin.launch(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "unknown")
        await asyncio.sleep(0.6)
        self.executor.launch.assert_awaited_once()

    async def test_update_revision_and_snapshot(self):
        d = await self.create()
        self.plugin.due(d["id"])
        old = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.request(
            TaskRequest(
                "update", {"task_id": d["id"], "revision": d["revision"], "changes": {"prompt": "new work"}}
            )
        )
        self.assertEqual(self.plugin.repo.run(old["id"])["data"]["spec"]["prompt"], "authorized task")
        with self.assertRaises(ValueError):
            await self.plugin.request(
                TaskRequest(
                    "update", {"task_id": d["id"], "revision": d["revision"], "changes": {"prompt": "stale"}}
                )
            )

    async def test_child_scope_and_background_management_restrictions(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "running")
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("terminate", {"task_id": d["id"]}, run["id"]))
        with self.assertRaises(ValueError):
            await self.plugin.request(
                TaskRequest(
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {
                            "title": "child",
                            "prompt": "bad",
                            "trigger": {"kind": "interval", "seconds": 10},
                        },
                    },
                    run["id"],
                )
            )

    async def test_context_and_result_are_silent(self):
        d = await self.create(execution={"kind": "reminder"})
        self.plugin.due(d["id"])
        r = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.launch(r)
        for _ in range(30):
            m = self.chat.get("run:" + r["id"])
            if m and self.host.backend.import_voice_messages.await_count:
                break
            await asyncio.sleep(0.1)
        self.assertIn("authorized task", m["text"])
        self.assertEqual(m["source"], "notification")
        self.host.voice.say.assert_not_awaited()
        self.host.backend.import_voice_messages.assert_awaited_once()

    async def test_silent_monitor_does_not_create_routine_notifications(self):
        d = await self.create(execution={"kind": "reminder"}, notify_completion=False)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.launch(run)
        await asyncio.sleep(0.7)
        self.assertEqual(self.host.notifications.repo.list()["items"], [])
        self.assertIsNone(self.chat.get("run:" + run["id"]))

    async def test_execution_duration_excludes_queue_pause_and_delivery_updates(self):
        d = await self.create()
        self.plugin.due(d["id"])
        r = self.plugin.repo.runs(d["id"])[0]
        repo = self.plugin.repo
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=100):
            repo.update_run(r["id"], "dispatching")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=110):
            repo.update_run(r["id"], "running")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=120):
            repo.update_run(r["id"], "paused")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=200):
            repo.update_run(r["id"], "queued")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=210):
            repo.update_run(r["id"], "dispatching")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=225):
            repo.update_run(r["id"], "completed", progress="done")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=300):
            result = repo.update_run(r["id"], completion_delivered=True)
        self.assertEqual(result["data"]["duration_seconds"], 35)
        self.assertEqual(result["data"]["started_at"], 100)
        self.assertEqual(result["data"]["finished_at"], 225)
        # Resuming a terminal execution accumulates new work without counting the gap.
        repo.update_run(r["id"], "queued")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=400):
            repo.update_run(r["id"], "dispatching")
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=410):
            result = repo.update_run(r["id"], "failed")
        self.assertEqual(result["data"]["duration_seconds"], 45)

    async def test_completion_preference_snapshot_and_notification_duration(self):
        d = await self.create(execution={"kind": "reminder"}, notify_completion=False, auto_archive=False)
        self.plugin.due(d["id"])
        silent = self.plugin.repo.runs(d["id"])[0]
        await self.plugin.request(
            TaskRequest(
                "update",
                {"task_id": d["id"], "revision": d["revision"], "changes": {"notify_completion": True}},
            )
        )
        self.assertFalse(self.plugin.repo.run(silent["id"])["data"]["spec"]["notify_completion"])
        # A failure also honors the explicit no-completion-notifications choice.
        self.plugin.repo.update_run(silent["id"], "failed", progress="test failure")
        await asyncio.sleep(0.7)
        self.assertEqual(self.host.notifications.repo.list()["items"], [])
        await self.plugin.request(TaskRequest("run", {"task_id": d["id"], "request_id": str(uuid.uuid4())}))
        for _ in range(40):
            notices = self.host.notifications.repo.list()["items"]
            if notices:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(len(notices), 1)
        origin = notices[0]["payload"]["origin"]
        result = self.plugin.repo.run(origin["run_id"])
        self.assertGreaterEqual(result["data"]["duration_seconds"], 0)
        self.assertEqual(origin["duration_seconds"], result["data"]["duration_seconds"])

    async def test_monitor_event_gets_final_timing_without_republication(self):
        d = await self.create(notify_completion=False)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=100):
            self.plugin.repo.update_run(run["id"], "running")
        notice = await self.runtime.invoke(
            NotificationRequest(
                "publish",
                {
                    "event_key": "monitor-recovered",
                    "title": "恢复更新",
                    "body": "已恢复",
                },
                run["id"],
            )
        )
        self.assertNotIn("duration_seconds", notice["payload"]["origin"])
        await self.runtime.invoke(NotificationRequest("read", {"id": notice["id"], "revision": 1}))
        before = await self.runtime.invoke(NotificationRequest("get", {"id": notice["id"]}))
        self.assertEqual(before["execution"], {"kind": "event", "state": "running", "duration_seconds": None})
        with patch("assistant_runtime.plugins.tasks.repository.time.time", return_value=172.45):
            self.plugin.repo.update_run(run["id"], "completed")
        after = await self.runtime.invoke(NotificationRequest("get", {"id": notice["id"]}))
        self.assertAlmostEqual(after["execution"]["duration_seconds"], 72.45)
        self.assertEqual(after["execution"]["state"], "completed")
        self.assertEqual(after["payload"], before["payload"])
        self.assertEqual(after["fingerprint"], before["fingerprint"])
        self.assertEqual(after["revision"], 1)
        self.assertFalse(after["unread"])
        await asyncio.sleep(0.7)
        notices = self.host.notifications.repo.list()["items"]
        self.assertEqual(len(notices), 1)  # no automatic completion notice
        self.assertTrue(
            all(x["attempts"] == 1 for x in self.host.notifications.repo.get(notice["id"])["deliveries"])
        )

    async def test_execution_info_resolves_old_runs_in_one_bounded_batch(self):
        d = await self.create(notify_completion=False)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        notice = await self.runtime.invoke(
            NotificationRequest(
                "publish",
                {
                    "event_key": "old-event",
                    "title": "事件",
                    "body": "内容",
                },
                run["id"],
            )
        )
        self.plugin.repo.update_run(
            run["id"], "completed", started_at=100, timing_since=None, finished_at=200, duration_seconds=42
        )
        for i in range(105):
            self.plugin.repo.enqueue(d, "history:" + str(i), state="completed")
        self.store.db.commit()
        self.assertNotIn(run["id"], [r["id"] for r in self.plugin.repo.runs(d["id"])])
        with patch.object(self.runtime, "invoke", wraps=self.runtime.invoke) as invoke:
            listing = await self.host.notifications.request(NotificationRequest("list"))
        self.assertEqual(invoke.await_count, 1)
        self.assertEqual(invoke.await_args.args[0].action, "execution_info")
        item = next(n for n in listing["items"] if n["id"] == notice["id"])
        self.assertEqual(item["execution"]["duration_seconds"], 42)
        for ids in (["x"] * 101, "x", [None]):
            with self.assertRaises(ValueError):
                await self.plugin.request(TaskRequest("execution_info", {"run_ids": ids}))

    async def test_mcp_management_grant_requires_the_scoped_run_to_still_be_active(self):
        d = await self.create(resources=["mcp_management"], trigger={"kind": "interval", "seconds": 3600})
        run = self.plugin.repo.enqueue(d, "management-check")

        async def granted():
            info = await self.plugin.request(TaskRequest("execution_info", {"run_ids": [run["id"]]}))
            return info[run["id"]]["mcp_management"]

        self.assertFalse(await granted())
        self.plugin.repo.update_run(run["id"], "running")
        self.assertTrue(await granted())
        self.plugin.repo.update_run(run["id"], "completed")
        self.assertFalse(await granted())

    async def test_lost_notification_ack_retries_without_duplicate_event(self):
        d = await self.create(execution={"kind": "reminder"})
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        original = self.host.notifications.repo.publish

        def publish(*args, **kwargs):
            original(*args, **kwargs)
            self.host.notifications.repo.publish = original
            raise RuntimeError("receipt lost after commit")

        self.host.notifications.repo.publish = publish
        await self.plugin.launch(run)
        for _ in range(30):
            if self.plugin.repo.run(run["id"])["data"].get("completion_delivered"):
                break
            await asyncio.sleep(0.1)
        self.assertTrue(self.plugin.repo.run(run["id"])["data"].get("completion_delivered"))
        self.assertEqual(len(self.host.notifications.repo.list()["items"]), 1)

    async def test_mcp_is_separate_and_routes_run_identity(self):
        client = SimpleNamespace(invoke=AsyncMock(return_value={}))
        async with Client(create_server(client, "parent-run")) as mcp:
            tools = {t.name: t for t in await mcp.list_tools()}
            self.assertEqual(set(tools), set(TASK_TOOLS))
            self.assertTrue(tools["get_task"].annotations.read_only_hint)
            await mcp.call_tool(
                "list_tasks", {"view": "planned", "active_only": True, "before": "cursor", "limit": 7}
            )
            self.assertEqual(
                client.invoke.await_args.args[0],
                TaskRequest(
                    "list",
                    {
                        "query": "",
                        "state": None,
                        "view": "planned",
                        "active_only": True,
                        "include_archived": False,
                        "before": "cursor",
                        "limit": 7,
                    },
                    "parent-run",
                ),
            )
            await mcp.call_tool("get_task", {"task_id": "t", "before": "cursor", "limit": 9})
            self.assertEqual(
                client.invoke.await_args.args[0],
                TaskRequest("get", {"task_id": "t", "before": "cursor", "limit": 9}, "parent-run"),
            )
            await mcp.call_tool("supplement_task", {"run_id": "r", "text": "new instructions"})
            self.assertEqual(
                client.invoke.await_args.args[0],
                TaskRequest("supplement", {"run_id": "r", "text": "new instructions"}, "parent-run"),
            )

    async def test_model_catalog_rejects_unsupported_selection(self):
        client = SimpleNamespace(
            request=AsyncMock(
                return_value={
                    "data": [
                        {"id": "x", "model": "x", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}
                    ]
                }
            )
        )
        runner = CodexJobs(SimpleNamespace(client=client), lambda *a, **kw: None)
        self.assertEqual((await runner.select("x", "low"))["model"], "x")
        with self.assertRaises(ValueError):
            await runner.select("x", "high")
        with self.assertRaises(ValueError):
            await runner.select("missing", "low")

    async def test_executor_only_routes_its_threads(self):
        updates = []
        runner = CodexJobs(
            SimpleNamespace(client=SimpleNamespace()), lambda *a, **kw: updates.append((a, kw))
        )
        runner.threads = {"background": "run"}
        self.assertFalse(
            await runner.event(
                {"method": "turn/started", "params": {"threadId": "foreground", "turn": {"id": "t"}}}
            )
        )
        self.assertTrue(
            await runner.event(
                {"method": "turn/started", "params": {"threadId": "background", "turn": {"id": "t"}}}
            )
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][0], ("run", "running"))

    async def test_finalization_retry_is_durable_and_does_not_change_execution(self):
        task = await self.create(auto_archive=False, notify_completion=False)
        run = self.plugin.repo.enqueue(task, "cleanup", "running")
        run = self.plugin.repo.update_run(
            run["id"],
            "failed",
            thread_id="owned-thread",
            progress="retained result",
            completion_delivered=True,
        )
        before = self.plugin.repo.run(run["id"])
        self.executor.finalize.side_effect = RuntimeError("provider unavailable")
        await self.plugin.finalize_once(run["id"])
        receipt = self.plugin.repo.finalization(run["id"])
        self.assertEqual(receipt["state"], "pending")
        self.assertEqual(receipt["attempts"], 1)
        self.assertGreater(receipt["next_at"], time.time())
        self.assertEqual(self.plugin.repo.run(run["id"]), before)
        self.plugin.repo.seed_finalizations()
        self.assertEqual(self.plugin.repo.finalization(run["id"]), receipt)
        await self.plugin.finalize_once(run["id"])
        self.assertEqual(self.executor.finalize.await_count, 1)
        self.executor.finalize.side_effect = None
        self.store.db.execute("UPDATE job_finalizations SET next_at=0 WHERE run_id=?", (run["id"],))
        self.store.db.commit()
        await self.plugin.finalize_once(run["id"])
        self.assertEqual(self.plugin.repo.finalization(run["id"])["state"], "done")
        self.assertEqual(self.plugin.repo.run(run["id"]), before)
        self.executor.launch.assert_not_awaited()

    async def test_continuation_waits_for_finalize_and_rearms_next_round(self):
        task = await self.create(auto_archive=False, notify_completion=False)
        run = self.plugin.repo.enqueue(task, "cleanup", "running")
        run = self.plugin.repo.update_run(run["id"], "failed", thread_id="owned-thread")
        started, release = asyncio.Event(), asyncio.Event()

        async def finish(_):
            started.set()
            await release.wait()

        self.executor.finalize.side_effect = finish
        cleanup = asyncio.create_task(self.plugin.finalize_once(run["id"]))
        await started.wait()
        continuation = asyncio.create_task(
            self.plugin.request(TaskRequest("continue", {"run_id": run["id"], "text": "continue"}))
        )
        await asyncio.sleep(0)
        self.assertFalse(continuation.done())
        release.set()
        await cleanup
        await continuation
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["completion_round"], 1)
        self.assertNotIn(run["id"], self.plugin.repo.finalizations())
        self.plugin.repo.update_run(run["id"], "failed")
        receipt = self.plugin.repo.finalization(run["id"])
        self.assertEqual(receipt["completion_round"], 1)
        self.assertEqual(receipt["state"], "pending")

    async def test_finalizer_does_not_block_notifications_or_main_management(self):
        task = await self.create(auto_archive=False, notify_completion=False)
        run = self.plugin.repo.enqueue(task, "cleanup", "running")
        self.plugin.repo.update_run(run["id"], "completed", thread_id="owned-thread")
        started, release = asyncio.Event(), asyncio.Event()

        async def finish(_):
            started.set()
            await release.wait()

        self.executor.finalize.side_effect = finish
        cleanup = asyncio.create_task(self.plugin.finalize_once(run["id"]))
        await started.wait()
        listing = await asyncio.wait_for(self.plugin.request(TaskRequest("list")), 0.5)
        self.assertTrue(listing["tasks"])
        notice = await self.runtime.invoke(
            NotificationRequest(
                "publish", {"event_key": "while-cleaning", "title": "result", "body": "ready"}
            )
        )
        self.assertTrue(notice["id"])
        release.set()
        await cleanup
        await self.plugin.finalize_once(run["id"])
        self.assertEqual(self.executor.finalize.await_count, 1)

    async def test_recovery_seeds_only_ended_owned_threads_and_rejects_late_reactivation(self):
        task = await self.create(auto_archive=False)
        expected = set()
        for state in (
            "completed",
            "failed",
            "canceled",
            "missed",
            "running",
            "paused",
            "unknown",
            "waiting_user",
        ):
            run = self.plugin.repo.enqueue(task, state, state, {"thread_id": "thread-" + state})
            if state in {"completed", "failed", "canceled", "missed"}:
                expected.add(run["id"])
        self.plugin.repo.enqueue(task, "no-thread", "completed")
        self.plugin.repo.seed_finalizations()
        self.assertEqual(set(self.plugin.repo.finalizations()), expected)
        run = self.plugin.repo.run(next(iter(expected)))
        self.assertFalse(self.plugin.provider_update(run["id"], "running", turn_active=True))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], run["state"])

    async def test_workfiles_cleanup_preserves_active_artifacts_and_outside(self):
        work = self.host.workfiles
        done = work.area("tasks/t/r")
        active = work.area("conversations/session")
        (done / "scratch/a").write_text("temp")
        (done / "artifacts/result").write_text("keep")
        (active / "scratch/a").write_text("active")
        with self.store.db:
            self.store.db.execute(
                "UPDATE work_areas SET ended=? WHERE owner=?", (time.time() - 8 * 86400, "tasks/t/r")
            )
        self.assertEqual(len(work.cleanup()["eligible_scratch"]), 1)
        work.cleanup(True)
        self.assertFalse((done / "scratch").exists())
        self.assertTrue((done / "artifacts/result").exists())
        self.assertTrue((active / "scratch/a").exists())
        with self.assertRaises(ValueError):
            work.area("tasks/../../outside")
        outside = self.root / "outside"
        outside.mkdir()
        (work.root / "tasks/link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            work.area("tasks/link/run")

    async def test_manual_run_retry_returns_same_execution(self):
        d = await self.create(trigger={"kind": "date", "at": "2030-01-01T00:00:00+08:00"})
        args = {"task_id": d["id"], "request_id": str(uuid.uuid4())}
        first = await self.plugin.request(TaskRequest("run", args))
        second = await self.plugin.request(TaskRequest("run", args))
        self.assertEqual(first["id"], second["id"])

    async def test_fixed_configuration_applies_to_existing_future_runs(self):
        d = await self.create()
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.settings.execution = {"model": "new-fixed", "reasoning_effort": "medium"}
        await self.plugin.launch(run)
        sent = self.executor.launch.await_args.args[0]
        self.assertEqual(sent["data"]["spec"]["execution"]["model"], "new-fixed")
        self.assertEqual(
            self.plugin.repo.run(run["id"])["data"]["spec"]["execution"]["reasoning_effort"], "medium"
        )

    async def test_output_only_notification_preserves_standby(self):
        from assistant_runtime.assistant.service import AssistantService

        host = object.__new__(AssistantService)
        host.active = False
        host.transition = asyncio.Lock()
        host.quiet_active = lambda: False
        host.notification_output = False
        host.player = SimpleNamespace(manual_muted=False, stop=AsyncMock())

        async def say(text):
            self.assertFalse(host.active)
            self.assertTrue(host.notification_output)
            host.player.manual_muted = True  # explicit local stop terminates notification

        host.voice = SimpleNamespace(open=AsyncMock(), close=AsyncMock(), say=AsyncMock(side_effect=say))
        await host.notify_task("test")
        host.voice.open.assert_awaited_once_with(notification_only=True)
        host.voice.close.assert_awaited_once()
        self.assertFalse(host.active)
        self.assertFalse(host.notification_output)
        host.quiet_active = lambda: True
        await host.notify_task("quiet")
        self.assertEqual(host.voice.say.await_count, 1)

    async def test_real_scheduler_runs_without_manual_due_callback(self):
        d = await self.create(execution={"kind": "reminder"})
        await asyncio.sleep(1.1)
        runs = self.plugin.repo.runs(d["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], "completed")
        self.assertEqual(self.runtime.contexts["tasks"].state, "ready")

    async def test_event_threshold_requires_fresh_data_and_rearms(self):
        self.config.devices["sensor"] = {"enabled": True}
        reading = {"value": 30, "fresh": False}
        self.host.devices.snapshot = lambda ident: {"readings": {"temperature": reading}, "fresh": True}
        d = await self.create(
            execution={"kind": "reminder"},
            trigger={
                "kind": "event",
                "event": {
                    "source": "device",
                    "id": "sensor",
                    "metric": "temperature",
                    "operator": "gt",
                    "value": 25,
                    "hysteresis": 2,
                    "cooldown_seconds": 0,
                },
            },
        )
        await asyncio.sleep(1.1)
        self.assertEqual(self.plugin.repo.runs(d["id"]), [])
        reading["fresh"] = True
        await asyncio.sleep(1.6)
        self.assertEqual(len(self.plugin.repo.runs(d["id"])), 1)
        reading["value"] = 24
        await asyncio.sleep(1.1)
        self.assertEqual(len(self.plugin.repo.runs(d["id"])), 1)
        reading["value"] = 22
        await asyncio.sleep(1.1)
        reading["value"] = 30
        await asyncio.sleep(1.6)
        self.assertEqual(len(self.plugin.repo.runs(d["id"])), 2)

    async def test_parallel_limit_and_resource_lock(self):
        for number in range(3):
            await self.create(title=str(number), resources=["same-project"])
        await asyncio.sleep(0.8)
        self.assertEqual(self.executor.launch.await_count, 1)
        run = next(r for r in self.plugin.repo.runs() if r["state"] == "dispatching")
        self.plugin.repo.update_run(run["id"], "completed")
        await asyncio.sleep(0.7)
        self.assertEqual(self.executor.launch.await_count, 2)

    async def test_restart_reconciles_instead_of_replaying(self):
        d = await self.create()
        await asyncio.sleep(0.8)
        self.assertEqual(self.executor.launch.await_count, 1)
        await self.runtime.close()
        self.runtime = Runtime(self.store)
        for name in ("codex", "conversation"):
            self.runtime.add(Dummy(name))
        self.plugin = TasksPlugin(self.host)
        self.runtime.add(NotificationsPlugin(self.host))
        self.host.runtime = self.runtime
        self.runtime.add(self.plugin)
        await self.runtime.start()
        await asyncio.sleep(0.6)
        self.assertEqual(self.executor.launch.await_count, 1)
        self.assertEqual(self.plugin.repo.runs(d["id"])[0]["state"], "unknown")

    async def test_long_goal_can_start_on_a_future_schedule(self):
        d = await self.create(long_term=True, trigger={"kind": "date", "at": "2030-01-01T00:00:00+08:00"})
        self.assertTrue(d["spec"]["long_term"])
        self.assertGreater(d["next_at"], time.time())
        self.assertEqual(self.plugin.repo.runs(d["id"]), [])

    async def test_goal_intermediate_turn_is_not_completion(self):
        d = await self.create(long_term=True)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.repo.update_run(run["id"], "running", goal={"status": "active"})
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "running")
        self.plugin.provider_update(run["id"], None, goal={"status": "complete"})
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "completed")

    def test_timezone_and_dst_use_calendar_trigger(self):
        now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        expected = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            next_time({"kind": "cron", "expression": "0 9 * * *"}, "Asia/Shanghai", now), expected
        )
        with self.assertRaises(ValueError):
            next_time({"kind": "date", "at": "2026-10-15T09:00:00"}, "Asia/Shanghai", now)
