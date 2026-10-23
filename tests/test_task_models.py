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

    def test_api_no_reasoning_model_remains_an_automatic_selection_candidate(self):
        policy = ModelPolicy.parse({"profiles": {"fast": {"model": "api-model", "reasoning_effort": "none"}}})
        candidates = policy.options(
            [{"model": "api-model", "supported_reasoning_efforts": [{"reasoning_effort": "none"}]}],
            {},
            {"model": "auto", "reasoning_effort": "auto"},
        )
        self.assertEqual(
            candidates, {"fast_none": {"profile": "fast", "model": "api-model", "reasoning_effort": "none"}}
        )

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
            "profile": "balanced",
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

    async def test_auto_and_children_do_not_inherit_high_configuration(self):
        self.host.backend.resolved_reasoning_effort = "high"
        d = await self.create()
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "low")
        parent = await self.make_running(
            execution={"profile": "deep", "reason": "复杂诊断，需要验证冲突证据"}
        )
        child = await self.plugin.request(
            TaskRequest(
                "create",
                {"request_id": str(uuid.uuid4()), "spec": {"title": "child", "prompt": "authorized child"}},
                parent["id"],
            )
        )
        self.assertEqual(child["spec"]["execution"]["profile"], "fast")
        self.assertEqual(child["spec"]["depth"], 1)

    async def test_agent_reason_and_unrelated_update_preserve_real_provenance(self):
        with self.assertRaisesRegex(ValueError, "execution.reason"):
            await self.create(execution={"profile": "balanced"})
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
        self.assertEqual(models["model_policy"]["default_profile"], "fast")
        self.plugin.settings.execution = {"model": "fixed", "reasoning_effort": "high"}
        run = await self.make_running()
        receipt = run["data"]["model_selections"][0]
        self.assertEqual(receipt["model"], "fixed")
        self.assertEqual(receipt["selection"]["sources"]["model"], "instance_fixed")
        trace = await self.plugin.request(TaskRequest("trace", {"run_id": run["id"]}))
        self.assertEqual(trace["model_selections"], run["data"]["model_selections"])

    async def test_ultra_requires_reason_and_uses_instance_mapping_without_becoming_default(self):
        with self.assertRaisesRegex(ValueError, "execution.reason"):
            await self.create(execution={"profile": "ultra"})
        self.plugin.model_policy = ModelPolicy.parse(
            {"profiles": {"ultra": {"model": "research-model", "reasoning_effort": "xhigh"}}}
        )
        definition = await self.create(
            execution={
                "profile": "ultra",
                "reason": "原创跨领域证明，需同时验证多层相互依赖的假设，deep 不足",
            }
        )
        execution = definition["spec"]["execution"]
        self.assertEqual((execution["model"], execution["reasoning_effort"]), ("research-model", "xhigh"))
        self.assertEqual(execution["selection"]["profile"], "ultra")
        self.assertEqual((await self.create())["spec"]["execution"]["profile"], "fast")
        self.assertEqual(self.plugin.model_policy.max_upgrades, 1)

        self.executor.select.side_effect = ValueError("Configured research model unavailable")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.create(execution={"profile": "ultra", "reason": "已有具体研究推理需求"})

    async def test_deep_to_ultra_handoff_preserves_definition_and_cannot_downgrade(self):
        run = await self.make_running(execution={"profile": "deep", "reason": "需要诊断相互冲突的证据"})
        await self.upgrade(
            run,
            profile="ultra",
            cause="reasoning_limit",
            reason="已验证该问题需要原创多层证明，deep 的方法不足",
            evidence="现有方法不能同时满足两个已验证约束，需要重新构造并验证理论模型",
        )
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        await self.plugin.upgrades.apply(run["id"])
        dispatched = self.executor.launch.await_args.args[0]
        selected = dispatched["data"]["spec"]["execution"]
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("ultra", "high"))
        self.assertEqual(dispatched["id"], run["id"])
        self.assertEqual(dispatched["data"]["execution_session_id"], "provider-thread")
        definition = self.plugin.repo.definition(run["task_id"])
        self.assertEqual(definition["spec"]["execution"]["profile"], "deep")
        highest = await self.make_running(execution={"profile": "ultra", "reason": "复杂原创科研证明"})
        with self.assertRaisesRegex(ValueError, "higher"):
            await self.upgrade(highest, profile="deep")

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
        self.assertEqual(dispatched["data"]["spec"]["execution"]["profile"], "balanced")
        self.assertEqual(dispatched["task_id"], run["task_id"])
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_selections"][-1]["segment"], 2)
        self.plugin.provider_update(run["id"], "running", turn_id="new-turn", turn_active=True)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_upgrades"][-1]["phase"], "applied")
        with self.assertRaisesRegex(ValueError, "limit"):
            await self.upgrade(self.plugin.repo.run(run["id"]), profile="deep")

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
        with self.assertRaisesRegex(ValueError, "higher"):
            await self.upgrade(run, profile="fast")
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
    def test_profile_settings_are_strict_and_instance_overrides_are_partial(self):
        p = ModelPolicy.parse({"profiles": {"fast": {"model": "custom-fast"}}})
        self.assertEqual(p.profiles["fast"], {"model": "custom-fast", "reasoning_effort": "low"})
        self.assertEqual(p.profiles["ultra"], {"model": "go-gpt", "reasoning_effort": "max"})
        configured = ModelPolicy.parse({"profiles": {"ultra": {"reasoning_effort": "xhigh"}}})
        self.assertEqual(configured.profiles["ultra"]["reasoning_effort"], "xhigh")
        self.assertEqual(configured.profiles["deep"]["reasoning_effort"], "high")
        for data in (
            {"max_upgrades": True},
            {"max_upgrades": 4},
            {"profiles": {"unexpected": {}}},
            {"profiles": {"fast": {"model": "auto"}}},
        ):
            with self.assertRaises(ValueError):
                ModelPolicy.parse(data)
