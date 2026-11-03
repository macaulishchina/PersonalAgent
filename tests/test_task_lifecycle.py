"""Synthetic task dispatch, quarantine and operator recovery, without providers/devices."""

import asyncio
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api.tool_calls import ToolFailure
from assistant_runtime.core.pi.jobs import PiJobs
from assistant_runtime.core.tasks.lifecycle import locks
from assistant_runtime.core.tasks.plugin import TasksPlugin
from assistant_runtime.core.notifications.plugin import NotificationsPlugin
from assistant_runtime.kernel.runtime import Runtime
import test_tasks as task_fixtures


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_fixtures.TasksTest.asyncSetUp
    asyncTearDown = task_fixtures.TasksTest.asyncTearDown
    create = task_fixtures.TasksTest.create

    async def queued(self, **spec):
        definition = await self.create(trigger={"kind": "date", "at": "2030-01-01T00:00:00+08:00"}, **spec)
        with self.store.db:
            run = self.plugin.repo.enqueue(definition, "fixture")
        return definition, run

    async def test_model_retry_uses_checkpoint_and_persisted_request_identity(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(
            run["id"], "failed", turn_id="failed-turn", failure={"retry_allowed": True}
        )
        self.executor.model_retry_checkpoint = AsyncMock(return_value=True)
        args = {
            "run_id": run["id"],
            "retry_request_id": str(uuid.uuid4()),
            "retry_of": "failed-turn",
            "text": "continue from confirmed results",
        }
        await self.plugin.request(TaskRequest("continue", args))
        first = self.plugin.repo.run(run["id"])
        await self.plugin.request(TaskRequest("continue", args))
        self.assertEqual(self.plugin.repo.run(run["id"])["revision"], first["revision"])
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["data"]["model_retry_target"], "failed-turn")
        self.executor.model_retry_checkpoint.assert_awaited_once()
        self.plugin.repo.update_run(run["id"], "failed", turn_id="new-turn")
        self.executor.model_retry_checkpoint.return_value = False
        with self.assertRaisesRegex(ValueError, "不能直接重试"):
            await self.plugin.request(
                TaskRequest(
                    "continue", {**args, "retry_request_id": str(uuid.uuid4()), "retry_of": "new-turn"}
                )
            )

    async def test_preparation_failure_is_failed_not_quarantined_and_keeps_first_error(self):
        _, run = await self.queued(resource_locks=["object:one"])
        self.executor.select.side_effect = ValueError("invalid model effort")
        await self.plugin.launch(run)
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["data"]["dispatch"]["state"], "not_dispatched")
        self.assertEqual(current["data"]["initial_error"]["type"], "ValueError")
        self.executor.launch.assert_not_awaited()
        self.assertTrue(self.plugin.repo.history(run["id"])["items"])

    async def test_pi_prepare_and_dispatch_loss_have_distinct_evidence(self):
        _, run = await self.queued()

        async def prepare_failed(saved, directory):
            self.plugin.provider_update(
                saved["id"], None, dispatch={"stage": "session_create", "state": "not_dispatched"}
            )
            raise TimeoutError("session setup failed")

        self.executor.launch.side_effect = prepare_failed
        await self.plugin.launch(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "failed")
        _, second = await self.queued(title="second")

        async def lost(saved, directory):
            self.plugin.provider_update(
                saved["id"], None, dispatch={"stage": "run_start", "state": "unknown"}
            )
            raise TimeoutError("run reply lost")

        self.executor.launch.side_effect = lost
        await self.plugin.launch(second)
        self.assertEqual(self.plugin.repo.run(second["id"])["state"], "unknown")
        with self.assertRaisesRegex(ValueError, "必须先核对"):
            await self.plugin.request(TaskRequest("continue", {"run_id": second["id"], "text": "continue"}))
        await asyncio.sleep(0.6)
        self.assertEqual(self.executor.launch.await_count, 2)

    async def test_legacy_unknown_remains_quarantined_with_exact_wait_reason(self):
        _, old = await self.queued(resources=["scope"])
        spec = {**old["data"]["spec"]}
        spec.pop("resource_locks")
        old = self.plugin.repo.update_run(old["id"], "unknown", spec=spec)
        _, new = await self.queued(resources=["scope"])
        reason = self.plugin.lifecycle.wait(new, [], [old, new])
        self.assertEqual(reason["blockers"][0]["run_id"], old["id"])
        self.assertEqual(reason["blockers"][0]["resources"], ["scope"])
        self.assertEqual(locks(old), ["scope"])
        saved = self.plugin.repo.run(new["id"])
        self.plugin.lifecycle.wait(saved, [], [old, saved])
        self.assertEqual(self.plugin.repo.run(new["id"])["revision"], saved["revision"])

    async def test_authorization_does_not_implicitly_lock_new_tasks(self):
        _, one = await self.queued(resources=["scope"])
        _, two = await self.queued(resources=["scope"])
        one = self.plugin.repo.update_run(one["id"], "running")
        self.assertIsNone(self.plugin.lifecycle.wait(two, [one], [one, two]))
        self.assertEqual(locks(two), [])

    async def test_explicit_mutex_blocks_and_unblocks_without_new_execution(self):
        _, one = await self.queued(resource_locks=["file:one"])
        _, two = await self.queued(resource_locks=["file:one"])
        one = self.plugin.repo.update_run(one["id"], "unknown")
        self.assertIsNotNone(self.plugin.lifecycle.wait(two, [], [one, two]))
        one = self.plugin.repo.update_run(one["id"], "canceled")
        self.assertIsNone(self.plugin.lifecycle.wait(self.plugin.repo.run(two["id"]), [], [one, two]))
        self.assertEqual(len(self.plugin.repo.runs(two["task_id"])), 1)

    async def test_recovery_requires_evidence_revision_and_operator_confirmation(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown", progress="original error")
        saved = await self.plugin.request(TaskRequest("reconcile_run", {"run_id": run["id"]}))
        self.assertEqual(saved["state"], "unknown")
        self.assertFalse(saved["data"]["recovery"]["safe_to_retry"])
        args = {
            "run_id": run["id"],
            "revision": saved["revision"],
            "confirmed": True,
            "reason": "核对外部操作记录，无在途操作；保留原有文件，不重新提交。",
        }
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("resolve_run", {**args, "revision": 0}))
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("resolve_run", args, run["id"]))
        resolved = await self.plugin.request(TaskRequest("resolve_run", args))
        self.assertEqual(resolved["state"], "canceled")
        history = self.plugin.repo.history(run["id"])
        self.assertIn("operator_resolution", [item["kind"] for item in history["items"]])
        self.executor.launch.assert_not_awaited()

    async def test_canonical_unknown_operation_prevents_automatic_release(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown")
        self.executor.reconcile.return_value = ("completed", "done")
        self.host.backend.executions = SimpleNamespace(
            task_evidence=lambda ident: [
                {
                    "id": "canonical",
                    "state": "completed",
                    "unresolved_operations": [{"id": "tool", "state": "unknown"}],
                }
            ]
        )
        saved = await self.plugin.lifecycle.reconcile({"run_id": run["id"]})
        self.assertEqual(saved["state"], "unknown")

    async def test_manual_duplicate_returns_existing_execution_receipt(self):
        definition, run = await self.queued()
        import uuid

        with self.assertRaises(ToolFailure) as caught:
            await self.plugin.request(
                TaskRequest("run", {"task_id": definition["id"], "request_id": str(uuid.uuid4())})
            )
        self.assertEqual(caught.exception.outcome.code, "task_run_conflict")
        self.assertEqual(caught.exception.outcome.receipt["runs"][0]["run_id"], run["id"])

    async def test_removed_model_rebinds_before_dispatch_never_after_provider_failure(self):
        _, run = await self.queued()
        self.config.main_model = "gpt-6.1-sol"
        self.config.main_reasoning_effort = "medium"
        models = self.executor.models.return_value
        self.executor.models.return_value = [m for m in models if m["model"] != "gpt-6-luna"]
        await self.plugin.launch(run)
        saved = self.plugin.repo.run(run["id"])
        self.assertEqual(saved["data"]["spec"]["execution"]["model"], "gpt-6.1-sol")
        self.assertEqual(
            saved["data"]["spec"]["execution"]["selection"]["prior_execution"]["model"], "gpt-6-luna"
        )
        self.assertEqual(self.executor.launch.await_count, 1)

    async def test_concurrent_launches_share_one_dispatch_boundary(self):
        _, run = await self.queued()
        await asyncio.gather(self.plugin.launch(run), self.plugin.launch(run))
        self.executor.launch.assert_awaited_once()

    async def test_nested_delegation_uses_plan_ancestry_and_rejects_ancestor_mutexes(self):
        import uuid

        _, parent = await self.queued()
        parent = self.plugin.repo.update_run(parent["id"], "running")
        for depth in (1, 2):
            child = await self.plugin.request(
                TaskRequest(
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {"title": "nested", "prompt": "synthetic delegated work"},
                    },
                    parent["id"],
                )
            )
            self.assertEqual(child["spec"]["depth"], depth)
            with self.store.db:
                parent = self.plugin.repo.enqueue(child, "nested-fixture", "running")
        spec = {**parent["data"]["spec"], "resource_locks": ["file:held"]}
        self.plugin.repo.update_run(parent["id"], spec=spec)
        with self.assertRaisesRegex(ValueError, "ancestor task mutexes"):
            await self.plugin.request(
                TaskRequest(
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {
                            "title": "mutex child",
                            "prompt": "synthetic",
                            "resource_locks": ["file:held"],
                        },
                    },
                    parent["id"],
                )
            )

    async def test_parent_stop_during_selection_prevents_child_commit(self):
        import uuid

        _, parent = await self.queued()
        self.plugin.repo.update_run(parent["id"], "running")

        async def select(model, effort):
            self.plugin.repo.update_run(parent["id"], stop_requested="canceled")
            return {"model": model, "reasoning_effort": effort}

        self.executor.select.side_effect = select
        with self.assertRaisesRegex(ValueError, "Parent stopped"):
            await self.plugin.request(
                TaskRequest(
                    "create",
                    {
                        "request_id": str(uuid.uuid4()),
                        "spec": {"title": "child", "prompt": "synthetic"},
                    },
                    parent["id"],
                )
            )
        self.assertEqual(len(self.plugin.repo.definitions()), 1)

    async def test_stop_does_not_publish_terminal_state_until_confirmed(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "running")
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(*args, **kwargs):
            entered.set()
            await release.wait()

        self.executor.stop.side_effect = stop
        task = asyncio.create_task(self.plugin.request(TaskRequest("cancel_run", {"run_id": run["id"]})))
        await entered.wait()
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "running")
        info = await self.plugin.request(TaskRequest("execution_info", {"run_ids": [run["id"]]}))
        self.assertEqual(info[run["id"]]["state"], "stopping")
        release.set()
        self.assertEqual((await task)["state"], "canceled")

    async def test_stopping_unknown_does_not_claim_external_resolution(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown")
        saved = await self.plugin.request(TaskRequest("cancel_run", {"run_id": run["id"]}))
        self.assertEqual(saved["state"], "unknown")
        self.assertIsNone(saved["data"]["stop_requested"])

    async def test_restart_of_interrupted_stop_never_leaves_queued_or_replays(self):
        _, known = await self.queued()
        _, uncertain = await self.queued(title="uncertain")
        self.plugin.repo.update_run(
            known["id"], stop_requested="canceled", dispatch={"stage": "preparing", "state": "not_dispatched"}
        )
        self.plugin.repo.update_run(uncertain["id"], stop_requested="canceled")
        await self.runtime.close()
        self.runtime = Runtime(self.store)
        for name in ("pi", "conversation", "execution", "workfiles"):
            self.runtime.add(task_fixtures.Dummy(name))
        self.host.runtime = self.runtime
        self.plugin = TasksPlugin(self.host)
        self.runtime.add(NotificationsPlugin(self.host))
        self.runtime.add(self.plugin)
        await self.runtime.start()
        self.assertEqual(self.plugin.repo.run(known["id"])["state"], "failed")
        self.assertEqual(self.plugin.repo.run(uncertain["id"])["state"], "unknown")
        self.assertIsNone(self.plugin.repo.run(uncertain["id"])["data"]["stop_requested"])
        self.executor.launch.assert_not_awaited()

    async def test_late_success_retains_unknown_until_reconciled(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown")
        self.plugin.provider_update(run["id"], "completed", result="late result", turn_id="turn")
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "unknown")
        self.assertIn("late_outcome", [x["kind"] for x in self.plugin.repo.history(run["id"])["items"]])

    async def test_archive_reports_uncertain_leftovers_without_replaying_them(self):
        definition, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown", progress="original error")
        self.executor.stop.side_effect = RuntimeError("no session identity")
        result = await self.plugin.request(TaskRequest("archive", {"task_id": definition["id"]}))
        self.assertEqual(result["state"], "archived")
        self.assertEqual(result["unfinished_runs"][0]["run_id"], run["id"])
        kinds = [x["kind"] for x in self.plugin.repo.history(run["id"])["items"]]
        self.assertIn("stop_unconfirmed", kinds)
        self.assertIn("archive_unresolved", kinds)
        self.executor.launch.assert_not_awaited()

    async def test_removed_model_without_current_binding_fails_without_dispatch(self):
        _, run = await self.queued()
        self.executor.models.return_value = []
        await self.plugin.launch(run)
        saved = self.plugin.repo.run(run["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["data"]["initial_error"]["code"], "invalid_arguments")
        self.executor.launch.assert_not_awaited()

    async def test_lost_dispatch_identity_uses_only_unique_canonical_task_attempt(self):
        _, run = await self.queued()
        self.plugin.repo.update_run(run["id"], "unknown")
        self.host.backend.executions = SimpleNamespace(
            task_evidence=lambda ident: [
                {
                    "id": "canonical",
                    "state": "unknown",
                    "session_id": "task-session",
                    "turn_id": "turn",
                    "unresolved_operations": [],
                }
            ]
        )
        saved = await self.plugin.lifecycle.reconcile({"run_id": run["id"]})
        self.assertEqual(saved["data"]["execution_session_id"], "task-session")
        self.assertEqual(saved["state"], "unknown")
        self.assertEqual(self.executor.reconcile.await_args.args[0]["data"]["turn_id"], "turn")


class PiStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_recovery_identity_is_not_overwritten_by_live_handles(self):
        async def request(command, params):
            return {"session": {"status": {"type": "idle"}}} if command == "session.read" else {"data": []}

        client = SimpleNamespace(request=AsyncMock(side_effect=request))
        jobs = PiJobs(SimpleNamespace(client=client), lambda *a, **k: True)
        jobs.handles["run"] = {"execution_session_id": "live", "turn_id": "current"}
        run = {
            "id": "run",
            "state": "unknown",
            "data": {"spec": {}, "execution_session_id": "historical", "turn_id": "old"},
        }
        await jobs.stop(run, recorded=True)
        cancel = next(call.args[1] for call in client.request.await_args_list if call.args[0] == "run.cancel")
        self.assertEqual(cancel, {"session_id": "historical", "run_id": "old"})

    async def test_no_thread_without_dispatch_receipt_is_not_cancellation_success(self):
        client = SimpleNamespace(request=AsyncMock())
        jobs = PiJobs(SimpleNamespace(client=client), lambda *a, **k: True)
        run = {"id": "run", "state": "unknown", "data": {}}
        with self.assertRaises(RuntimeError):
            await jobs.stop(run)
        await jobs.stop({**run, "data": {"dispatch": {"state": "not_dispatched"}}})
        client.request.assert_not_awaited()


class ConflictDisplayTest(unittest.TestCase):
    def test_conflict_links_to_existing_task_without_exposing_receipt_details(self):
        from assistant_runtime.core.pi.tool_display import tool_display

        task = "a" * 32
        outcome = {
            "code": "task_run_conflict",
            "dispatch": "not_dispatched",
            "stage": "validation",
            "outcome": "failed",
            "receipt": {"task_id": task, "private": "not for display"},
        }
        display = tool_display({"server": "tasks", "tool": "run_task", "tool_outcome": outcome})
        self.assertEqual(display["task_id"], task)
        self.assertNotIn("private", str(display))
        outcome["receipt"]["task_id"] = "../../outside"
        self.assertNotIn(
            "task_id", tool_display({"server": "tasks", "tool": "run_task", "tool_outcome": outcome})
        )


if __name__ == "__main__":
    unittest.main()
