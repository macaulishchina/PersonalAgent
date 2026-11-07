"""Optional owner evidence, task handoff, correction and headless composition."""

from dataclasses import dataclass
import math
from types import SimpleNamespace
import unittest
import uuid

import test_tasks as fixtures
from assistant_runtime.api.contracts import NotificationRequest, TaskRequest
from assistant_runtime.api.operations import summarize, validate_observations


@dataclass(frozen=True)
class OwnerRequest:
    action: str
    arguments: dict


def observation(state, revision=1, **fields):
    return {
        "id": "operation",
        "revision": revision,
        "state": state,
        "responsibility": "user"
        if state == "waiting_user"
        else "system"
        if state in {"pending", "unknown"}
        else "none",
        **fields,
    }


class ObservationContractTest(unittest.TestCase):
    def test_aggregation_and_strict_bounded_metadata(self):
        self.assertEqual(summarize([observation("completed"), observation("failed")])["state"], "partial")
        for bad in (
            observation("unknown", math.nan),
            observation("unknown", math.inf),
            observation("success"),
            {**observation("failed"), "responsibility": "guess"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_observations({"operations": [bad]})
        with self.assertRaises(ValueError):
            validate_observations({"operations": [observation("completed"), observation("completed")]})


class BusinessResultTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TasksTest.asyncSetUp
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    async def test_capability_readiness_respects_owner_lifecycle(self):
        self.assertTrue(self.runtime.available(NotificationRequest))
        context = self.runtime.contexts["notifications"]
        original = context.state
        try:
            context.state = "failed"
            self.assertFalse(self.runtime.available(NotificationRequest))
            self.assertFalse(self.runtime.available(OwnerRequest))
        finally:
            context.state = original

    async def prepared(self, **spec):
        definition = await self.create(trigger={"kind": "interval", "seconds": 3600}, **spec)
        run = self.plugin.repo.enqueue(definition, "synthetic", "completed")
        return self.plugin.repo.update_run(run["id"], progress="已全部删除成功")

    def source(self, items):
        self.items = items
        self.host.operation_sources = {"example": OwnerRequest}

        async def observe(request):
            self.assertEqual(request.action, "operation_outcomes")
            if isinstance(self.items, Exception):
                raise self.items
            return {"operations": self.items}

        self.runtime.register("tasks", OwnerRequest, observe)

    async def test_unknown_receipt_overrides_success_prose_without_claiming_user_confirmation(self):
        self.source([observation("unknown", summary="提交回执需要核对")])
        run = await self.prepared()
        await self.plugin.deliver_result(run)
        current = self.plugin.repo.run(run["id"])
        payload = self.plugin.result_payload(current)
        self.assertNotIn("已全部删除成功", payload["body"])
        self.assertIn("不需要你逐项确认", payload["body"])
        self.assertEqual(payload["kind"], "result")
        self.assertTrue(self.plugin.result_protected(run["id"]))
        self.executor.launch.assert_not_awaited()

    async def test_dependent_task_waits_for_business_outcome_not_agent_completion(self):
        self.source([observation("unknown")])
        run = await self.prepared()
        self.assertIsNone(self.plugin.business.event_state(run))
        run = await self.plugin.business.refresh(run)
        self.assertIsNone(self.plugin.business.event_state(run))
        self.items = [observation("failed", 2)]
        run = await self.plugin.business.refresh(run)
        self.assertEqual(self.plugin.business.event_state(run), "failed")
        self.items = [observation("completed", 3)]
        run = await self.plugin.business.refresh(run)
        self.assertEqual(self.plugin.business.event_state(run), "completed")

    async def test_late_observation_cannot_overwrite_a_new_execution_round(self):
        self.host.operation_sources = {"example": OwnerRequest}
        run = await self.prepared()

        async def observe(request):
            self.plugin.repo.update_run(run["id"], "queued", completion_round=1, progress="新的执行轮次")
            return {"operations": [observation("unknown")]}

        self.runtime.register("tasks", OwnerRequest, observe)
        result = await self.plugin.business.refresh(run)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["data"]["progress"], "新的执行轮次")
        self.assertNotIn("business_result", result["data"])

    async def test_pending_business_ends_agent_but_waits_for_owner_and_delivers_once(self):
        self.source([observation("pending")])
        run = await self.prepared()
        await self.plugin.deliver_result(run)
        self.assertFalse(self.plugin.repo.run(run["id"])["data"].get("completion_delivered", False))
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "completed")
        self.items = [observation("completed", 2)]
        await self.plugin.deliver_result(self.plugin.repo.run(run["id"]))
        await self.plugin.deliver_result(self.plugin.repo.run(run["id"]))
        count = self.store.db.execute(
            "SELECT count(*) FROM notifications WHERE event_key LIKE 'task-result:%'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.executor.launch.assert_not_awaited()

    async def test_reconciliation_corrects_same_notice_and_releases_hold_without_new_execution(self):
        self.source([observation("unknown", summary="结果未知")])
        run = await self.prepared()
        await self.plugin.deliver_result(run)
        old = self.plugin.feedback.describe(self.plugin.repo.run(run["id"]))["receipt"]
        self.items = [observation("completed", 2)]
        await self.plugin.refresh_business(self.plugin.repo.run(run["id"]))
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["data"]["business_result"]["state"], "completed")
        self.assertFalse(self.plugin.result_protected(run["id"]))
        notice = await self.runtime.invoke(NotificationRequest("get", {"id": old["notification_id"]}))
        self.assertNotIn("结果待核验", notice["payload"]["title"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM notifications WHERE event_key LIKE 'task-result:%'"
            ).fetchone()[0],
            1,
        )
        self.executor.launch.assert_not_awaited()

    async def test_known_provider_failure_retains_evidence_unrelated_failure_does_not_block(self):
        self.source([observation("unknown")])
        first = await self.prepared()
        first = await self.plugin.business.refresh(first)
        self.items = RuntimeError("unavailable")
        result = await self.plugin.business.refresh(first)
        self.assertEqual(result["data"]["business_result"]["state"], "unknown")
        self.assertEqual(result["data"]["business_result"]["sources"]["example"][0]["id"], "operation")
        second = await self.prepared()
        result = await self.plugin.business.refresh(second)
        self.assertNotIn("business_result", result["data"])

    async def test_disappearing_or_stale_evidence_does_not_turn_unknown_into_success(self):
        self.source([observation("unknown", 3)])
        run = await self.plugin.business.refresh(await self.prepared())
        for items in (
            [observation("completed", 2)],
            [],
            [{**observation("completed", 4), "id": "different"}],
        ):
            self.items = items
            result = await self.plugin.business.refresh(run)
            self.assertEqual(result["data"]["business_result"]["state"], "unknown")

    async def test_media_correction_replaces_the_prior_attention_notice_idempotently(self):
        from assistant_runtime.plugins.media.repository import MediaRepository
        from assistant_runtime.plugins.media.settings import MediaSettings
        from assistant_runtime.plugins.media.notifications import publish
        import test_media as media_fixtures

        repo = MediaRepository(self.store.db, MediaSettings())
        item = repo.create(str(uuid.uuid4()), media_fixtures.intent(), "assistant")
        adapter = SimpleNamespace(repo=repo, api=SimpleNamespace(invoke=self.runtime.invoke))
        warning = {
            "event_key": "media-attention:" + item["id"],
            "title": "结果待核验",
            "body": "暂不能确认",
            "kind": "attention",
        }
        first = await publish(adapter, warning)
        complete = {
            "event_key": "media-complete:" + item["id"],
            "title": "已核验",
            "body": "目录已移入回收站",
            "kind": "result",
        }
        updated = await publish(adapter, complete)
        again = await publish(adapter, complete)
        self.assertEqual(first["id"], updated["id"])
        self.assertEqual(again["revision"], updated["revision"])
        self.assertEqual(updated["payload"]["kind"], "result")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)

    async def test_waiting_user_requires_actionable_message_not_generic_check_result(self):
        self.source([observation("waiting_user", summary="账号授权失效", next_action="更新网盘凭据")])
        result = await self.plugin.business.refresh(await self.prepared())
        payload = self.plugin.result_payload(result)
        self.assertEqual(payload["kind"], "attention")
        self.assertIn("更新网盘凭据", payload["body"])

    async def test_no_notifications_or_operation_plugin_preserves_query_only_completion(self):
        self.host.operation_sources = {}
        self.runtime.handlers.pop(NotificationRequest)
        run = await self.prepared()
        await self.plugin.deliver_result(run)
        result = self.plugin.repo.run(run["id"])
        self.assertTrue(result["data"]["completion_delivered"])
        self.assertEqual(result["data"]["completion_feedback"]["route"], "query")
        self.assertEqual(result["data"]["progress"], "已全部删除成功")

    async def test_business_results_refresh_on_task_read_and_block_unsafe_continuation(self):
        self.source([observation("unknown")])
        run = await self.prepared()
        self.plugin.repo.update_run(run["id"], "failed")
        await self.plugin.business.refresh(self.plugin.repo.run(run["id"]))
        with self.assertRaisesRegex(ValueError, "关联业务操作"):
            await self.plugin.request(TaskRequest("continue", {"run_id": run["id"], "text": "retry"}))
        self.items = [observation("completed", 2)]
        result = await self.plugin.request(TaskRequest("get", {"task_id": run["task_id"]}))
        observed = next(item for item in result["runs"] if item["id"] == run["id"])
        self.assertEqual(observed["data"]["business_result"]["state"], "completed")
