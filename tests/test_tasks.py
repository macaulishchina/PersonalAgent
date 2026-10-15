"""Durable tasks: duplicate dispatch, isolation, recovery and cleanup boundaries."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from fastmcp import Client
from assistant_runtime.config import Config
from assistant_runtime.store import Store
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.mcp.tasks import create_server
from assistant_runtime.mcp.catalog import TASK_TOOLS
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
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
            models=AsyncMock(return_value=[{"model": "test-model"}]),
            select=AsyncMock(side_effect=lambda model, effort: {"model": model, "reasoning_effort": effort}),
            launch=AsyncMock(),
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
        self.host.runtime = SimpleNamespace(invoke=AsyncMock(return_value={"temperature": 23}))
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
        self.host.backend.import_voice_messages.assert_awaited_once()

    async def test_duplicate_due_and_late_one_shot_never_replay(self):
        d = await self.create()
        self.plugin.due(d["id"])
        self.plugin.due(d["id"])
        self.assertEqual(len(self.plugin.repo.runs(d["id"])), 1)
        late = await self.create(trigger={"kind": "date", "at": "2020-01-01T00:00:00+08:00"})
        self.plugin.due(late["id"])
        self.assertEqual(self.plugin.repo.runs(late["id"])[0]["state"], "missed")

    async def test_interval_coalesces_and_prevents_overlap(self):
        d = await self.create(trigger={"kind": "interval", "seconds": 10})
        self.plugin.repo.update_definition(d["id"], next_at=time.time() - 95)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.assertEqual(run["state"], "queued")
        self.plugin.repo.update_definition(d["id"], next_at=time.time())
        self.plugin.due(d["id"])
        self.assertEqual({r["state"] for r in self.plugin.repo.runs(d["id"])}, {"queued", "missed"})

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
        await asyncio.sleep(0.7)
        m = self.chat.get("run:" + r["id"])
        self.assertIn("authorized task", m["text"])
        self.assertEqual(m["source"], "background")
        self.host.voice.say.assert_not_awaited()
        self.host.backend.import_voice_messages.assert_awaited_once()

    async def test_mcp_is_separate_and_routes_run_identity(self):
        client = SimpleNamespace(invoke=AsyncMock(return_value={}))
        async with Client(create_server(client, "parent-run")) as mcp:
            tools = {t.name: t for t in await mcp.list_tools()}
            self.assertEqual(set(tools), set(TASK_TOOLS))
            self.assertTrue(tools["get_task"].annotations.read_only_hint)
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
