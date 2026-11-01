"""Selection policy and durable handoffs: real task engine, isolated provider protocol."""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

import test_tasks as task_fixtures
import test_job_lifecycle as provider_fixtures

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.core.notifications.plugin import NotificationsPlugin
from assistant_runtime.core.tasks.plugin import TasksPlugin
from assistant_runtime.core.tasks.selection import ModelPolicy


class SelectionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_fixtures.TasksTest.asyncSetUp
    asyncTearDown = task_fixtures.TasksTest.asyncTearDown
    create = task_fixtures.TasksTest.create

    async def test_defaults_need_no_recommendations_and_follow_main_configuration(self):
        self.plugin.model_policy = ModelPolicy.parse({})
        self.config.main_model = "gpt-6.1-sol"
        self.config.main_reasoning_effort = "medium"
        result = (await self.create())["spec"]["execution"]
        self.assertEqual((result["model"], result["reasoning_effort"]), ("gpt-6.1-sol", "medium"))
        self.assertNotIn("recommendation", result)
        self.assertEqual(set(result["selection"]["sources"].values()), {"main_conversation"})

    async def test_nested_execution_patch_preserves_reason_and_running_snapshot(self):
        run = await self.make_running(
            execution={
                "model": "gpt-6-luna",
                "model_source": "assistant",
                "reason": "stable reason",
                "assessment": {"acceptance": "verify", "uncertainties": ["old"]},
            }
        )
        before = copy.deepcopy(run["data"]["spec"])
        definition = self.plugin.repo.definition(run["task_id"])
        updated = await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": definition["id"],
                    "revision": definition["revision"],
                    "changes": {"execution": {"assessment": {"uncertainties": ["new"]}}},
                },
            )
        )
        execution = updated["spec"]["execution"]
        self.assertEqual(execution["reason"], "stable reason")
        self.assertEqual(execution["assessment"]["acceptance"], "verify")
        self.assertEqual(execution["assessment"]["uncertainties"], ["new"])
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["spec"], before)

    async def test_auto_effort_requires_caller_choice_when_main_is_auto(self):
        self.config.main_reasoning_effort = "auto"
        with self.assertRaisesRegex(ValueError, "initiating model"):
            await self.create()
        result = await self.create(execution={"reasoning_effort": "medium", "reason": "Compare evidence"})
        self.assertEqual(result["spec"]["execution"]["reasoning_effort"], "medium")
        self.executor.models.return_value = [
            {"model": "plain", "supported_reasoning_efforts": [{"reasoning_effort": "auto"}]}
        ]
        self.config.main_model = "plain"
        result = await self.create()
        self.assertEqual(result["spec"]["execution"]["reasoning_effort"], "auto")

    async def make_running(self, **spec):
        definition = await self.create(auto_archive=False, **spec)
        self.plugin.due(definition["id"])
        run = self.plugin.repo.runs(definition["id"])[0]
        await self.plugin.launch(run)
        self.plugin.provider_update(
            run["id"], "running", execution_session_id="provider-thread", turn_id="old-turn", turn_active=True
        )
        self.executor.upgrade_ready = AsyncMock()
        return self.plugin.repo.run(run["id"])

    def arguments(self, run, **changes):
        return {
            "run_id": run["id"],
            "request_id": str(uuid.uuid4()),
            "recommendation": "explore",
            "cause": "unfamiliar_workflow",
            "reason": "界面与已有流程不一致，需要探索并读回验收",
            "evidence": "已核实页面结构与已保存流程不同，尚未执行下一步写操作",
            "checkpoint": {
                "completed": ["已执行动作 operation_id=stable-id；不得重复"],
                "next_step": "读取当前状态后继续未完成的步骤",
                "active_operations": [],
                "uncertain_operations": [],
            },
            **changes,
        }

    async def upgrade(self, run, **changes):
        return await self.plugin.request(
            TaskRequest("upgrade_model", self.arguments(run, **changes), run["id"])
        )

    async def test_children_use_main_defaults_not_incidental_parent_model(self):
        self.host.backend.resolved_reasoning_effort = "high"
        d = await self.create()
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "low")
        parent = await self.make_running(
            execution={"recommendation": "investigate", "reason": "复杂诊断，需要验证冲突证据"}
        )
        child = await self.plugin.request(
            TaskRequest(
                "create",
                {"request_id": str(uuid.uuid4()), "spec": {"title": "child", "prompt": "authorized child"}},
                parent["id"],
            )
        )
        self.assertEqual(child["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertNotIn("recommendation", child["spec"]["execution"])
        self.assertEqual(child["spec"]["depth"], 1)

    async def test_agent_reason_and_unrelated_update_preserve_real_provenance(self):
        with self.assertRaisesRegex(ValueError, "execution.reason"):
            await self.create(execution={"recommendation": "explore"})
        d = await self.create()
        receipt = d["spec"]["execution"]["selection"]
        updated = await self.plugin.request(
            TaskRequest(
                "update", {"task_id": d["id"], "revision": d["revision"], "changes": {"title": "new title"}}
            )
        )
        self.assertEqual(updated["spec"]["execution"]["selection"], receipt)
        custom = await self.create(
            execution={"model": "custom", "reasoning_effort": "low", "reason": "explicit configured task"}
        )
        await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": custom["id"],
                    "revision": custom["revision"],
                    "changes": {"title": "updated custom"},
                },
            )
        )

    async def test_model_catalog_exposes_policy_and_selection_records_fixed_overrides(self):
        models = await self.plugin.request(TaskRequest("models", {}))
        self.assertEqual(models["model_policy"]["defaults"]["model"], "gpt-6-luna")
        self.plugin.settings.execution = {"model": "fixed", "reasoning_effort": "high"}
        run = await self.make_running()
        receipt = run["data"]["model_selections"][0]
        self.assertEqual(receipt["model"], "fixed")
        self.assertEqual(receipt["selection"]["sources"]["model"], "instance_fixed")
        trace = await self.plugin.request(TaskRequest("trace", {"run_id": run["id"]}))
        self.assertEqual(trace["model_selections"], run["data"]["model_selections"])

    async def test_custom_recommendation_has_no_rank_and_cannot_conflict_with_model(self):
        with self.assertRaisesRegex(ValueError, "execution.reason"):
            await self.create(execution={"recommendation": "research"})
        definition = await self.create(
            execution={"recommendation": "research", "reason": "Compare multiple hypotheses"}
        )
        self.assertEqual(definition["spec"]["execution"]["model"], "gpt-6-astra")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await self.create(
                execution={"recommendation": "research", "model": "gpt-6-luna", "reason": "Invalid pair"}
            )
        self.assertNotIn("recommendation", (await self.create())["spec"]["execution"])

    async def test_request_is_idempotent_and_waits_for_clean_boundary(self):
        run = await self.make_running()
        args = self.arguments(run)
        first = await self.plugin.request(TaskRequest("upgrade_model", args, run["id"]))
        again = await self.plugin.request(TaskRequest("upgrade_model", args, run["id"]))
        self.assertEqual(first, again)
        self.assertEqual(self.executor.launch.await_count, 1)
        with self.assertRaisesRegex(ValueError, "different contents"):
            await self.plugin.request(TaskRequest("upgrade_model", {**args, "reason": "changed"}, run["id"]))
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        ready = self.plugin.repo.run(run["id"])
        self.assertEqual(ready["state"], "running")
        self.assertEqual(ready["data"]["model_upgrades"][-1]["phase"], "ready")
        self.assertIsNone(self.plugin.repo.finalization(run["id"]))
        self.plugin.provider_update(run["id"], "completed", turn_active=False)  # duplicate event
        await self.plugin.upgrades.apply(run["id"])
        dispatched = self.executor.launch.await_args.args[0]
        self.assertEqual(dispatched["data"]["execution_session_id"], "provider-thread")
        self.assertIn("stable-id", dispatched["data"]["continuation"])
        self.assertEqual(dispatched["data"]["spec"]["execution"]["recommendation"], "explore")
        self.assertEqual(dispatched["task_id"], run["task_id"])
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_selections"][-1]["segment"], 2)
        self.plugin.provider_update(run["id"], "running", turn_id="new-turn", turn_active=True)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_upgrades"][-1]["phase"], "applied")
        with self.assertRaisesRegex(ValueError, "limit"):
            await self.upgrade(self.plugin.repo.run(run["id"]), recommendation="investigate")

    async def test_scope_unsafe_checkpoints_external_failures_and_goal_are_rejected(self):
        run = await self.make_running()
        with self.assertRaisesRegex(ValueError, "only its own"):
            await self.plugin.request(TaskRequest("upgrade_model", self.arguments(run), "another-run"))
        for key in ("active_operations", "uncertain_operations"):
            args = self.arguments(run)
            args["checkpoint"][key] = ["operation-id"]
            with self.assertRaisesRegex(ValueError, "Reconcile"):
                await self.plugin.request(TaskRequest("upgrade_model", args, run["id"]))
        with self.assertRaisesRegex(ValueError, "not upgrade causes"):
            await self.upgrade(run, cause="network_timeout")
        with self.assertRaisesRegex(ValueError, "configured recommendation"):
            await self.upgrade(run, recommendation="missing")
        goal = await self.make_running(long_term=True)
        with self.assertRaisesRegex(ValueError, "Goal"):
            await self.upgrade(goal)
        self.plugin.provider_update(run["id"], None, goal={"status": "active"})
        with self.assertRaisesRegex(ValueError, "Goal"):
            await self.upgrade(self.plugin.repo.run(run["id"]))

    async def test_goal_created_after_upgrade_request_blocks_handoff(self):
        run = await self.make_running()
        await self.upgrade(run)
        self.plugin.provider_update(run["id"], None, goal={"status": "active"})
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["state"], "waiting_user")
        self.assertEqual(current["data"]["model_upgrades"][-1]["error"], "native_goal_started")
        await self.plugin.upgrades.apply(run["id"])
        self.assertEqual(self.executor.launch.await_count, 1)

    async def test_failed_turn_and_user_pause_do_not_handoff(self):
        run = await self.make_running()
        await self.upgrade(run)
        self.plugin.provider_update(run["id"], "failed", turn_active=False)
        await self.plugin.upgrades.apply(run["id"])
        self.assertEqual(self.executor.launch.await_count, 1)
        other = await self.make_running()
        await self.upgrade(other)
        await self.plugin.request(TaskRequest("pause_run", {"run_id": other["id"]}))
        self.plugin.provider_update(other["id"], "completed", turn_active=False)
        await self.plugin.upgrades.apply(other["id"])
        self.assertEqual(self.plugin.repo.run(other["id"])["state"], "paused")
        self.assertEqual(self.executor.launch.await_count, 2)

    async def test_live_terminal_blocks_preflight_without_killing_it(self):
        run = await self.make_running()
        await self.upgrade(run)
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        self.executor.upgrade_ready.side_effect = RuntimeError("terminal still live")
        await self.plugin.upgrades.apply(run["id"])
        stopped = self.plugin.repo.run(run["id"])
        self.assertEqual(stopped["state"], "paused")
        self.assertEqual(stopped["data"]["model_upgrades"][-1]["phase"], "blocked")
        self.executor.stop.assert_not_awaited()
        self.assertEqual(self.executor.launch.await_count, 1)

    async def test_lost_handoff_reply_remains_unknown_and_is_never_replayed(self):
        run = await self.make_running()
        await self.upgrade(run)
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        self.executor.launch.side_effect = TimeoutError("accepted reply lost")
        await self.plugin.upgrades.apply(run["id"])
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "unknown")
        await self.plugin.upgrades.apply(run["id"])
        self.assertEqual(self.executor.launch.await_count, 2)

    async def test_fixed_policy_change_blocks_pending_handoff(self):
        run = await self.make_running()
        await self.upgrade(run)
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        self.plugin.settings.execution = {"model": "fixed", "reasoning_effort": "low"}
        await self.plugin.upgrades.apply(run["id"])
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "paused")
        self.assertEqual(self.executor.launch.await_count, 1)

    async def test_management_stop_during_preflight_does_not_launch_new_segment(self):
        for action, target in (("cancel_run", "canceled"), ("pause_run", "paused")):
            with self.subTest(action=action):
                run = await self.make_running()
                await self.upgrade(run)
                self.plugin.provider_update(run["id"], "completed", turn_active=False)
                entered, release = asyncio.Event(), asyncio.Event()
                launches = self.executor.launch.await_count

                async def check(current):
                    entered.set()
                    await release.wait()
                    if action == "pause_run":
                        raise TimeoutError("Provider check failed after user pause")

                self.executor.upgrade_ready.side_effect = check
                applying = asyncio.create_task(self.plugin.upgrades.apply(run["id"]))
                await entered.wait()
                try:
                    # Exercise the public lock-taking entry, not a direct DB edit.
                    await asyncio.wait_for(
                        self.plugin.request(TaskRequest(action, {"run_id": run["id"]})), 0.5
                    )
                    self.assertEqual(self.plugin.repo.run(run["id"])["state"], target)
                finally:
                    release.set()
                    await applying
                self.assertEqual(self.executor.launch.await_count, launches)
                self.assertEqual(self.plugin.repo.run(run["id"])["state"], target)

    async def test_restart_during_dispatch_does_not_reconcile_old_completed_turn_as_success(self):
        run = await self.make_running()
        await self.upgrade(run)
        data = self.plugin.repo.run(run["id"])["data"]
        history = copy.deepcopy(data["model_upgrades"])
        history[-1]["phase"] = "dispatching"
        self.plugin.repo.update_run(run["id"], "dispatching", model_upgrades=history)
        await self.runtime.close()
        self.executor.reconcile.return_value = ("completed", "old turn completed")
        self.runtime = Runtime(self.store)
        self.host.runtime = self.runtime
        self.runtime.add(task_fixtures.Dummy("pi"))
        self.runtime.add(task_fixtures.Dummy("conversation"))
        self.runtime.add(task_fixtures.Dummy("execution"))
        self.runtime.add(task_fixtures.Dummy("workfiles"))
        self.runtime.add(NotificationsPlugin(self.host))
        self.plugin = TasksPlugin(self.host)
        self.runtime.add(self.plugin)
        await self.runtime.start()
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "unknown")
        self.executor.reconcile.assert_not_awaited()
        self.assertEqual(self.executor.launch.await_count, 1)


class ProviderHandoffTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = provider_fixtures.JobLifecycleTest.asyncSetUp
    asyncTearDown = provider_fixtures.JobLifecycleTest.asyncTearDown
    rpc = provider_fixtures.JobLifecycleTest.rpc
    methods = provider_fixtures.JobLifecycleTest.methods

    async def test_preflight_checks_latest_completed_turn_and_no_live_commands(self):
        self.run["data"]["turn_id"] = "run"
        await self.runner.upgrade_ready(self.run)
        self.assertEqual(self.methods(), ["session.read", "session.runs", "operations.list"])
        self.terminals = [{"process_id": "live"}]
        with self.assertRaisesRegex(RuntimeError, "terminals"):
            await self.runner.upgrade_ready(self.run)
        self.assertNotIn("operations.stop", self.methods())
        self.terminals = []
        self.turns = [{"id": "different", "status": "completed"}]
        with self.assertRaisesRegex(RuntimeError, "checkpoint"):
            await self.runner.upgrade_ready(self.run)

    async def test_late_old_turn_completion_cannot_end_new_segment(self):
        self.runner.threads["background"] = "run"
        self.runner.handles["run"] = {"turn_id": "new-turn"}
        self.runner.notify = Mock()
        await self.runner.event(
            {
                "kind": "run.completed",
                "data": {"session_id": "background", "run": {"id": "old-turn", "status": "completed"}},
            }
        )
        self.runner.notify.assert_not_called()

    async def test_misconfigured_profile_cannot_downgrade_same_model(self):
        self.run["data"]["model_upgrades"] = [
            {
                "from": {"model": "same-model", "reasoning_effort": "high"},
                "to": {"model": "same-model", "reasoning_effort": "low"},
            }
        ]
        with self.assertRaisesRegex(ValueError, "would not increase"):
            await self.runner.upgrade_ready(self.run)
        self.assertEqual(self.methods(), [])

    async def test_catalog_refreshes_and_never_cumulates_old_entries(self):
        self.runner.catalog_at = 0
        self.client.request.side_effect = AsyncMock(
            return_value={
                "data": [
                    {
                        "id": "new",
                        "model": "new",
                        "supported_reasoning_efforts": [{"reasoning_effort": "low"}],
                    }
                ]
            }
        )
        await self.runner.models()
        self.assertEqual([m["model"] for m in self.runner.catalog], ["new"])


class PolicySettingsTest(unittest.TestCase):
    def test_empty_defaults_and_invalid_recommendations(self):
        self.assertEqual(ModelPolicy.parse({}).recommendations, {})
        for data in (
            {"max_upgrades": True},
            {"max_upgrades": 4},
            {"profiles": {}},
            {"fallbacks": {}},
            {"recommendations": {"invalid id": {}}},
            {"recommendations": {"valid": {"name": "missing model"}}},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ModelPolicy.parse(data)

    def test_duplicate_names_and_binding_efforts_are_validated(self):
        from assistant_runtime.core.tasks.settings import TaskSettings

        row = {
            "name": "诊断",
            "model": "one",
            "reason": "证据对比",
            "description": "根因未明时",
            "reasoning_effort": "high",
        }
        with self.assertRaisesRegex(ValueError, "unique"):
            ModelPolicy.parse({"recommendations": {"one": row, "two": row}})
        settings = TaskSettings.parse({"model_policy": {"recommendations": {"diagnosis": row}}})
        with self.assertRaisesRegex(ValueError, "已有的模型"):
            settings.validate_bindings({})
        with self.assertRaisesRegex(ValueError, "思考深度"):
            settings.validate_bindings({"one": {"reasoning_efforts": ["low"]}})
        settings.validate_bindings({"one": {"reasoning_efforts": ["low", "high"]}})
