"""Authorized self scheduling through real task storage, runtime and MCP."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import time
import unittest
from unittest.mock import patch
import uuid

from fastmcp import Client
from fastmcp.exceptions import ToolError

import test_tasks as fixtures
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.core.tasks.tools import create_server
from assistant_runtime.core.notifications.plugin import NotificationsPlugin
from assistant_runtime.core.tasks.plugin import TasksPlugin


DAILY = {"kind": "interval", "seconds": 86400}
WEEKLY = {"kind": "interval", "seconds": 604800}
GRANT = {"allowed_triggers": [DAILY, WEEKLY], "condition": "After the authorized phase is complete."}


class SelfScheduleTest(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    async def asyncSetUp(self):
        await fixtures.TasksTest.asyncSetUp(self)
        self.plugin.scheduler.pause()
        workers = list(self.runtime.contexts["tasks"].tasks)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def running(self, **spec):
        definition = await self.create(
            trigger={"kind": "interval", "seconds": 1800},
            self_schedule=copy.deepcopy(GRANT),
            notify_completion=False,
            **spec,
        )
        with self.store.db:
            run = self.plugin.repo.enqueue(definition, "synthetic:" + uuid.uuid4().hex)
        run = self.plugin.repo.update_run(
            run["id"],
            "running",
            turn_id="synthetic-turn",
            turn_active=True,
        )
        return definition, run

    def arguments(self, definition, **changes):
        return {
            "request_id": str(uuid.uuid4()),
            "revision": definition["revision"],
            "trigger": dict(DAILY),
            "reason": "The initial phase is complete.",
            "evidence": "The authorized progress artifact reports complete coverage; pending errors=0.",
            **changes,
        }

    async def apply(self, run, args):
        return await self.runtime.invoke(TaskRequest("reschedule_self", args, run["id"]))

    async def test_complete_own_plan_preserves_current_run_and_replays_receipt(self):
        definition, run = await self.running(self_completion={"condition": "All requested files verified"})
        args = self.arguments(definition)
        args.pop("trigger")
        receipt = await self.plugin.request(TaskRequest("complete_self", args, run["id"]))
        self.assertEqual(self.plugin.repo.definition(definition["id"])["state"], "archived")
        self.assertIsNone(self.plugin.repo.definition(definition["id"])["next_at"])
        self.assertEqual(self.plugin.repo.run(run["id"]), run)
        repeated = await self.plugin.request(TaskRequest("complete_self", args, run["id"]))
        self.assertEqual(receipt, repeated)
        self.assertEqual(receipt["action"], "complete_self")

    async def test_completion_requires_explicit_grant_current_revision_and_active_identity(self):
        definition, run = await self.running()
        args = self.arguments(definition)
        args.pop("trigger")
        with self.assertRaisesRegex(ValueError, "explicit authorization"):
            await self.plugin.request(TaskRequest("complete_self", args, run["id"]))
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("complete_self", args))
        definition, run = await self.running(self_completion={"condition": "Files verified"})
        updated = await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": definition["id"],
                    "revision": definition["revision"],
                    "changes": {"self_completion": None},
                },
            )
        )
        args = self.arguments(updated)
        args.pop("trigger")
        with self.assertRaisesRegex(ValueError, "User changed"):
            await self.plugin.request(TaskRequest("complete_self", args, run["id"]))

    async def test_changes_future_schedule_only_and_retains_execution_snapshot_and_trace(self):
        definition, run = await self.running()
        original = copy.deepcopy(run["data"])
        before = time.time()
        receipt = await self.apply(run, self.arguments(definition))
        saved = self.plugin.repo.definition(definition["id"])
        self.assertEqual(saved["spec"], {**definition["spec"], "trigger": DAILY})
        self.assertEqual(saved["revision"], definition["revision"] + 1)
        self.assertGreaterEqual(saved["next_at"], before + 86400)
        self.assertLess(saved["next_at"], time.time() + 86401)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"], original)
        self.assertEqual(len(self.plugin.repo.runs(definition["id"])), 1)
        self.executor.launch.assert_not_awaited()
        trace = await self.plugin.request(TaskRequest("trace", {"run_id": run["id"]}))
        self.assertEqual(trace["schedule_changes"], [receipt])
        self.assertEqual(receipt["evidence_scope"], "executor_reported")

    async def test_retries_confirm_receipt_and_same_run_can_iterate_without_moving_old_receipts(self):
        definition, run = await self.running()
        args = self.arguments(definition)
        first, concurrent = await asyncio.gather(self.apply(run, args), self.apply(run, args))
        self.assertEqual(concurrent, first)
        self.assertEqual(await self.apply(run, args), first)
        current = self.plugin.repo.definition(definition["id"])
        with self.assertRaisesRegex(ValueError, "different contents"):
            await self.apply(run, {**args, "reason": "Changed reason"})
        with self.assertRaisesRegex(ValueError, "already matches"):
            await self.apply(run, self.arguments(current))
        second = await self.apply(run, self.arguments(current, trigger=WEEKLY))
        self.assertEqual(await self.apply(run, args), first)
        current = self.plugin.repo.definition(definition["id"])
        self.assertEqual(current["revision"], second["revision"])
        self.assertEqual(current["next_at"], second["next_at"])
        self.assertEqual(current["spec"]["trigger"], WEEKLY)
        self.assertEqual(len(self.plugin.self_schedule.history(run["id"])["schedule_changes"]), 2)

    async def test_foreground_and_general_background_management_cannot_bypass_scope(self):
        definition, run = await self.running()
        args = self.arguments(definition)
        with self.assertRaisesRegex(ValueError, "bound background"):
            await self.plugin.request(TaskRequest("reschedule_self", args))
        other, _ = await self.running()
        for target in (definition, other):
            with self.assertRaisesRegex(ValueError, "general task management"):
                await self.plugin.request(
                    TaskRequest(
                        "update",
                        {
                            "task_id": target["id"],
                            "revision": target["revision"],
                            "changes": {"trigger": DAILY},
                        },
                        run["id"],
                    )
                )
        for extra in (
            {"task_id": other["id"]},
            {"run_id": run["id"]},
            {"prompt": "new goal"},
            {"self_schedule": GRANT},
        ):
            with self.assertRaises(ValueError):
                await self.apply(run, {**args, **extra})
        self.assertEqual(self.plugin.repo.definition(other["id"]), other)
        self.assertEqual(self.plugin.repo.definition(definition["id"]), definition)

    async def test_ungranted_targets_and_invalid_versions_or_requests_do_not_mutate(self):
        definition, run = await self.running()
        for changes in (
            {"trigger": {"kind": "interval", "seconds": 10}},
            {"trigger": {"kind": "immediate"}},
            {"trigger": {"kind": "event", "event": {}}},
            {"revision": True},
            {"revision": 0},
            {"revision": 999},
            {"request_id": "not-a-uuid"},
            {"reason": ""},
            {"evidence": ""},
            {"trigger": {"kind": "interval"}},
            {"trigger": {"kind": {}}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.apply(run, self.arguments(definition, **changes))
        self.assertEqual(self.plugin.repo.definition(definition["id"]), definition)

    async def test_user_edit_revocation_and_pause_win_even_when_latest_revision_is_supplied(self):
        for changes in ({"prompt": "User revised objective"}, {"self_schedule": None}):
            definition, run = await self.running()
            updated = await self.plugin.request(
                TaskRequest(
                    "update",
                    {
                        "task_id": definition["id"],
                        "revision": definition["revision"],
                        "changes": changes,
                    },
                )
            )
            self.assertEqual(updated["next_at"], definition["next_at"])
            with self.assertRaisesRegex(ValueError, "User changed this plan"):
                await self.apply(run, self.arguments(updated))
        definition, run = await self.running()
        paused = await self.plugin.request(TaskRequest("pause", {"task_id": definition["id"]}))
        with self.assertRaisesRegex(ValueError, "Paused or ended"):
            await self.apply(run, self.arguments(paused))
        self.assertEqual(self.plugin.repo.definition(definition["id"])["state"], "paused")

    async def test_inactive_and_unbound_provider_turns_cannot_change_plan(self):
        for state in ("queued", "paused", "waiting_user", "unknown", "completed", "failed", "canceled"):
            definition, run = await self.running()
            self.plugin.repo.update_run(run["id"], state)
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "running provider turn"):
                await self.apply(run, self.arguments(definition))
        definition, run = await self.running()
        self.plugin.repo.update_run(run["id"], turn_active=False)
        with self.assertRaisesRegex(ValueError, "running provider turn"):
            await self.apply(run, self.arguments(definition))

    async def test_explicit_grant_cannot_be_inferred_or_given_to_children(self):
        definition = await self.create(
            trigger={"kind": "interval", "seconds": 1800}, prompt="Please adjust your schedule later"
        )
        with self.store.db:
            run = self.plugin.repo.enqueue(definition, "no-grant")
        self.plugin.repo.update_run(run["id"], "running", turn_id="turn", turn_active=True)
        with self.assertRaisesRegex(ValueError, "explicit authorization"):
            await self.apply(run, self.arguments(definition))
        with self.assertRaisesRegex(ValueError, "cannot grant"):
            await self.plugin.request(
                TaskRequest(
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {"title": "child", "prompt": "child", "self_schedule": GRANT},
                    },
                    run["id"],
                )
            )

    async def test_policy_is_explicit_and_only_reusable_timed_plans_may_receive_it(self):
        invalid = (
            {},
            {"allowed_triggers": [DAILY]},
            {"allowed_triggers": [], "condition": "yes"},
            {"allowed_triggers": [DAILY, DAILY], "condition": "yes"},
            {"allowed_triggers": [{"kind": "date", "at": "2030-01-01"}], "condition": "yes"},
            {"allowed_triggers": [{"kind": "cron", "expression": 1}], "condition": "yes"},
        )
        for grant in invalid:
            with self.subTest(grant=grant), self.assertRaises(ValueError):
                await self.create(trigger={"kind": "interval", "seconds": 1800}, self_schedule=grant)
        with self.assertRaisesRegex(ValueError, "reusable timed plan"):
            await self.create(self_schedule=GRANT)
        with self.assertRaisesRegex(ValueError, "user-authorized"):
            await self.create(trigger=DAILY, authorization="derived", self_schedule=GRANT)

    async def test_date_and_cron_choices_are_validated_and_must_be_future(self):
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        choices = (
            {"kind": "date", "at": future},
            {"kind": "cron", "expression": "0 9 * * *", "timezone": "Asia/Shanghai"},
        )
        for target in choices:
            grant = {"allowed_triggers": [target], "condition": "User approved this future check"}
            definition = await self.create(trigger=DAILY, self_schedule=grant)
            with self.store.db:
                run = self.plugin.repo.enqueue(definition, uuid.uuid4().hex)
            self.plugin.repo.update_run(run["id"], "running", turn_id="turn", turn_active=True)
            receipt = await self.apply(run, self.arguments(definition, trigger=target))
            self.assertGreater(receipt["next_at"], time.time())
        past = {"kind": "date", "at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
        definition = await self.create(
            trigger=DAILY, self_schedule={"allowed_triggers": [past], "condition": "Expired choice"}
        )
        with self.store.db:
            run = self.plugin.repo.enqueue(definition, uuid.uuid4().hex)
        self.plugin.repo.update_run(run["id"], "running", turn_id="turn", turn_active=True)
        with self.assertRaisesRegex(ValueError, "future occurrence"):
            await self.apply(run, self.arguments(definition, trigger=past))

    async def test_receipt_failure_rolls_back_definition_and_projection_failure_is_recoverable(self):
        definition, run = await self.running()
        args = self.arguments(definition)
        with patch.object(self.plugin.self_schedule, "record", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                await self.apply(run, args)
        self.assertEqual(self.plugin.repo.definition(definition["id"]), definition)
        self.assertEqual(self.plugin.self_schedule.history(run["id"])["schedule_changes"], [])
        with patch.object(self.plugin, "schedule", side_effect=RuntimeError("projection unavailable")):
            with self.assertRaises(RuntimeError):
                await self.apply(run, args)
        committed = self.plugin.repo.definition(definition["id"])
        receipt = await self.apply(run, args)
        self.assertEqual(self.plugin.repo.definition(definition["id"]), committed)
        self.assertEqual(receipt["revision"], committed["revision"])

    async def test_restart_keeps_idempotency_and_never_replays_the_execution(self):
        definition, run = await self.running()
        args = self.arguments(definition)
        receipt = await self.apply(run, args)
        await self.runtime.close()
        self.runtime = Runtime(self.store)
        self.host.runtime = self.runtime
        for name in ("pi", "conversation", "execution", "workfiles"):
            self.runtime.add(fixtures.Dummy(name))
        self.runtime.add(NotificationsPlugin(self.host))
        self.plugin = TasksPlugin(self.host)
        self.runtime.add(self.plugin)
        await self.runtime.start()
        self.assertEqual(await self.apply(run, args), receipt)
        self.assertEqual(self.plugin.repo.definition(definition["id"])["revision"], receipt["revision"])
        self.executor.launch.assert_not_awaited()

    async def test_version_is_checked_again_at_the_storage_boundary(self):
        definition, run = await self.running()
        update = self.plugin.repo.update_definition

        def interleaved(ident, **changes):
            with self.store.db:
                self.store.db.execute("UPDATE job_definitions SET revision=revision+1 WHERE id=?", (ident,))
            return update(ident, **changes)

        with patch.object(self.plugin.repo, "update_definition", side_effect=interleaved):
            with self.assertRaisesRegex(ValueError, "Task changed"):
                await self.apply(run, self.arguments(definition))
        self.assertEqual(self.plugin.repo.definition(definition["id"])["spec"], definition["spec"])
        self.assertEqual(self.plugin.self_schedule.history(run["id"])["schedule_changes"], [])

    async def test_mcp_uses_bound_identity_and_exposes_no_arbitrary_target(self):
        definition, run = await self.running()
        async with Client(create_server(self.runtime, run["id"])) as client:
            tools = {t.name: t for t in await client.list_tools()}
            schema = tools["update_own_task_schedule"].input_schema["properties"]
            self.assertNotIn("task_id", schema)
            self.assertNotIn("run_id", schema)
            self.assertFalse(tools["update_own_task_schedule"].annotations.read_only_hint)
            receipt = (
                await client.call_tool("update_own_task_schedule", self.arguments(definition))
            ).structured_content
            self.assertEqual(receipt["task_id"], definition["id"])
        async with Client(create_server(self.runtime)) as client:
            with self.assertRaises(ToolError):
                await client.call_tool("update_own_task_schedule", self.arguments(definition))
