"""Ordered failure handling must not replay tool effects, canceled work or unknown calls."""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

import test_models
import test_tasks
import test_task_models
import test_job_lifecycle
from assistant_runtime.kernel.models import ModelCallError
from assistant_runtime.plugins.models.settings import ModelSettings
from assistant_runtime.plugins.codex.failures import model_failure
from assistant_runtime.plugins.tasks.selection import ModelPolicy
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin


class InferenceFallbackTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_models.ModelsTest.asyncSetUp
    asyncTearDown = test_models.ModelsTest.asyncTearDown
    request = test_models.ModelsTest.request

    def chain(self):
        self.plugin.settings.entries["second"] = {"provider": "test", "model": "two"}
        self.plugin.settings.entries["third"] = {"provider": "test", "model": "three"}
        self.plugin.settings.entries["test-model"]["fallbacks"] = ["second", "third"]

    async def test_order_and_receipt_prevent_duplicate_paid_calls(self):
        self.chain()
        self.client.complete.side_effect = [
            ModelCallError("model_http_402", fallback_allowed=True),
            ModelCallError("model_http_429", fallback_allowed=True),
            {"text": "done"},
        ]
        request = self.request()
        result = await self.runtime.invoke(request)
        self.assertEqual(
            [c.args[0]["model"] for c in self.client.complete.await_args_list],
            ["test-model", "second", "third"],
        )
        self.assertEqual(result["selected_model"], "third")
        self.assertEqual([a["state"] for a in result["attempts"]], ["failed", "failed", "completed"])
        self.assertEqual(await self.runtime.invoke(request), result)
        self.assertEqual(self.client.complete.await_count, 3)

    async def test_unknown_timeout_and_cancel_do_not_try_backup(self):
        self.chain()
        for error in (TimeoutError(), RuntimeError("stream disconnected"), asyncio.CancelledError()):
            self.client.complete.reset_mock()
            self.client.complete.side_effect = error
            request = self.request()
            with self.assertRaises(type(error)):
                await self.runtime.invoke(request)
            receipt = await self.runtime.invoke(request)
            self.assertEqual(receipt["state"], "unknown")
            self.assertEqual(receipt["attempts"][0]["state"], "unknown")
            self.client.complete.assert_awaited_once()

    async def test_exhaustion_is_failed_and_does_not_recurse_or_retry(self):
        self.chain()
        self.plugin.settings.entries["second"]["fallbacks"] = ["test-model"]
        self.client.complete.side_effect = ModelCallError("model_http_401", fallback_allowed=True)
        request = self.request()
        with self.assertRaises(ModelCallError):
            await self.runtime.invoke(request)
        result = await self.runtime.invoke(request)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(result["attempts"]), 3)
        self.assertEqual(self.client.complete.await_count, 3)

    async def test_restart_state_overrides_inflight_attempt_receipt(self):
        request = self.request()
        await self.runtime.invoke(request)
        self.store.db.execute(
            "UPDATE model_calls SET state='unknown',result=?",
            (json.dumps({"state": "submitted", "attempts": []}),),
        )
        self.assertEqual((await self.runtime.invoke(request))["state"], "unknown")
        self.client.complete.assert_awaited_once()

    async def test_incompatible_backup_cannot_drop_schema(self):
        self.chain()
        self.client.complete.side_effect = ModelCallError("model_http_402", fallback_allowed=True)
        with self.assertRaisesRegex(ValueError, "capability"):
            await self.runtime.invoke(self.request(schema={"type": "object"}))
        self.client.complete.assert_awaited_once()


class TaskFallbackTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_tasks.TasksTest.asyncSetUp
    asyncTearDown = test_tasks.TasksTest.asyncTearDown
    create = test_tasks.TasksTest.create
    make_running = test_task_models.SelectionTest.make_running

    async def ready(self):
        run = await self.make_running()
        self.executor.fallback_ready = AsyncMock()
        self.executor.detach = AsyncMock()
        self.executor.launch.reset_mock()
        self.plugin.model_policy.fallbacks = {
            "gpt-6-luna": [
                {"model": "api-two", "reasoning_effort": "none"},
                {"model": "api-three", "reasoning_effort": "medium"},
            ]
        }
        self.fail(run)
        return self.plugin.repo.run(run["id"])

    def fail(self, run):
        return self.plugin.provider_update(
            run["id"],
            "failed",
            turn_id=run["data"]["turn_id"],
            turn_active=False,
            model_failure={"code": "usageLimitExceeded", "fallback_allowed": True},
        )

    async def test_ordered_task_handoff_keeps_run_and_changes_thread(self):
        run = await self.ready()
        await self.plugin.fallbacks.apply(run["id"])
        first = self.executor.launch.call_args.args[0]
        self.assertEqual(first["id"], run["id"])
        self.assertIsNone(first["data"]["thread_id"])
        self.assertEqual(first["data"]["spec"]["execution"]["model"], "api-two")
        self.plugin.provider_update(
            run["id"], "running", thread_id="new-thread", turn_id="new-turn", turn_active=True
        )
        self.fail(self.plugin.repo.run(run["id"]))
        await self.plugin.fallbacks.apply(run["id"])
        self.assertEqual(
            self.executor.launch.call_args.args[0]["data"]["spec"]["execution"]["model"], "api-three"
        )
        self.plugin.provider_update(
            run["id"], "running", thread_id="last-thread", turn_id="last-turn", turn_active=True
        )
        self.fail(self.plugin.repo.run(run["id"]))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "failed")
        self.assertEqual(self.executor.launch.await_count, 2)

    async def test_duplicate_completion_does_not_duplicate_handoff(self):
        run = await self.ready()
        before = self.plugin.repo.run(run["id"])
        self.assertFalse(self.fail(run))
        self.assertEqual(self.plugin.repo.run(run["id"]), before)
        await self.plugin.fallbacks.apply(run["id"])
        await self.plugin.fallbacks.apply(run["id"])
        self.executor.launch.assert_awaited_once()

    async def test_tool_activity_blocks_fallback(self):
        run = await self.ready()
        self.executor.fallback_ready.side_effect = RuntimeError("tools already ran")
        await self.plugin.fallbacks.apply(run["id"])
        self.executor.launch.assert_not_awaited()
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_fallback"]["phase"], "blocked")

    async def test_pause_during_preflight_revokes_dispatch(self):
        run = await self.ready()

        async def pause(_):
            self.plugin.repo.update_run(run["id"], "paused")

        self.executor.fallback_ready.side_effect = pause
        await self.plugin.fallbacks.apply(run["id"])
        self.executor.launch.assert_not_awaited()
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "paused")

    async def test_dispatch_disconnect_is_unknown_and_never_replayed(self):
        run = await self.ready()
        self.executor.launch.side_effect = ConnectionError("lost after submission")
        await self.plugin.fallbacks.apply(run["id"])
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["state"], "unknown")
        self.assertEqual(current["data"]["model_fallback"]["phase"], "unknown")
        await self.plugin.fallbacks.apply(run["id"])
        self.executor.launch.assert_awaited_once()

    async def test_restart_during_handoff_preserves_unknown_without_dispatch(self):
        run = await self.ready()
        await self.runtime.close()
        self.executor.reconcile.return_value = ("completed", "old turn")
        self.runtime = Runtime(self.store)
        self.host.runtime = self.runtime
        self.runtime.add(test_tasks.Dummy("codex"))
        self.runtime.add(test_tasks.Dummy("conversation"))
        self.runtime.add(NotificationsPlugin(self.host))
        self.plugin = TasksPlugin(self.host)
        self.runtime.add(self.plugin)
        await self.runtime.start()
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "unknown")
        self.executor.reconcile.assert_not_awaited()
        self.executor.launch.assert_not_awaited()


class ProviderFallbackTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_job_lifecycle.JobLifecycleTest.asyncSetUp
    asyncTearDown = test_job_lifecycle.JobLifecycleTest.asyncTearDown
    rpc = test_job_lifecycle.JobLifecycleTest.rpc

    async def test_readback_must_prove_no_actions_and_detach_rejects_late_events(self):
        run = copy.deepcopy(self.run)
        run["data"].update(thread_id="background", turn_id="turn")
        self.runner.handles[run["id"]] = {
            "thread_id": "background",
            "fresh_execution": True,
            "activity": False,
        }
        self.runner.threads["background"] = run["id"]
        turn = {
            "id": "turn",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
            "items": [],
            "itemsView": "full",
        }
        self.runner.client.request = AsyncMock(
            side_effect=[{"thread": {"status": {"type": "systemError"}, "turns": [turn]}}, {"data": []}]
        )
        await self.runner.fallback_ready(run)
        for change in (
            {"items": [{"type": "commandExecution"}]},
            {"items": [{"type": "mcpToolCall"}]},
            {"status": "completed"},
            {"itemsView": "notLoaded"},
            {"error": {"codexErrorInfo": "other"}},
        ):
            self.runner.client.request = AsyncMock(
                return_value={"thread": {"status": {"type": "idle"}, "turns": [{**turn, **change}]}}
            )
            with self.assertRaises(RuntimeError):
                await self.runner.fallback_ready(run)
        await self.runner.detach(run)
        self.assertFalse(
            await self.runner.event(
                {"method": "turn/completed", "params": {"threadId": "background", "turn": turn}}
            )
        )

    def test_error_classifier_never_uses_prose_or_policy_denials(self):
        for error in (
            {"message": "unauthorized insufficient balance"},
            {"codexErrorInfo": "sandboxError"},
            {"codexErrorInfo": "cyberPolicy"},
            {"codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}}},
        ):
            self.assertFalse(model_failure(error)["fallback_allowed"])
        self.assertTrue(
            model_failure({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 402}}})[
                "fallback_allowed"
            ]
        )

    def test_chains_are_bounded_and_reject_unknown_or_duplicate_targets(self):
        for targets in (["missing"], ["test-model"], ["other"] * 4):
            config = test_models.settings()
            config["entries"]["test-model"]["fallbacks"] = targets
            with self.assertRaises(ValueError):
                ModelSettings.parse(config)
        for targets in (
            [],
            [{"model": "one", "reasoning_effort": "auto"}],
            [{"model": "one", "reasoning_effort": "low"}] * 2,
        ):
            with self.assertRaises(ValueError):
                ModelPolicy.parse({"fallbacks": {"one": targets}})
