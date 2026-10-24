"""Durable metering and denial before dispatch; isolated local resources only."""

from platform_support import private_mode

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from assistant_runtime.api.ai_usage import UsageScope, UsageError, UsageRequest
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.api.ai_usage import metered
from assistant_runtime.core.ai_usage.settings import UsageSettings


class UsageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.usage = UsageService(self.root)
        await self.usage.start()
        self.scope = UsageScope("work", "main")

    async def asyncTearDown(self):
        await self.usage.close()
        self.temp.cleanup()

    def policy(self, **changes):
        self.usage.settings = replace(self.usage.settings, **changes)
        self.usage.ledger.settings = self.usage.settings

    async def admit(self, scope=None, **kwargs):
        return await self.usage.admit(
            scope or self.scope, service="inference", provider="provider", model="model", **kwargs
        )

    async def done(self, ticket, **kwargs):
        return await self.usage.settle(ticket["id"], state="completed", **kwargs)

    async def test_unknown_compaction_retains_budget_without_poisoning_execution(self):
        ticket = await self.usage.admit(
            self.scope,
            service="execution",
            provider="p",
            model="m",
            compaction=True,
            bounds={"input_tokens": 100, "output_tokens": 50},
        )
        await self.usage.settle(ticket["id"], state="unknown")
        await self.usage.close()
        self.usage = UsageService(self.root)
        await self.usage.start()
        snapshot = await self.usage.snapshot("work")
        self.assertEqual(snapshot["requests"], 1)
        self.assertEqual(snapshot["unknown_requests"], 1)
        self.assertEqual(snapshot["unknown_execution_requests"], 0)
        ticket = await self.usage.admit(self.scope, service="execution", provider="p", model="m")
        await self.usage.settle(ticket["id"], state="unknown")
        self.assertEqual((await self.usage.snapshot("work"))["unknown_execution_requests"], 1)

    async def test_default_128_warning_256_allowed_257_denied(self):
        for n in range(256):
            ticket = await self.admit()
            await self.done(ticket, usage={"input_tokens": 1000000, "output_tokens": 1000000})
            notices = await self.usage.call("notices")
            self.assertEqual(any(v["kind"] == "warning" for v in notices), n >= 127)
        with self.assertRaisesRegex(UsageError, "max_requests"):
            await self.admit()
        snapshot = await self.usage.snapshot("work")
        self.assertEqual(snapshot["requests"], 256)
        self.assertEqual(snapshot["input_tokens"], 256000000)
        self.assertEqual(len(await self.usage.call("notices")), 2)

    async def test_parallel_children_share_one_atomic_allowance(self):
        self.policy(max_requests=10)
        children = [UsageScope("work", str(n), "plan") for n in range(40)]
        results = await asyncio.gather(*(self.admit(scope) for scope in children), return_exceptions=True)
        self.assertEqual(sum(isinstance(v, dict) for v in results), 10)
        self.assertEqual(sum(isinstance(v, UsageError) for v in results), 30)
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 10)

    async def test_binding_is_immutable_and_survives_reopen(self):
        bound = await self.usage.bind("run", UsageScope("work", "run", "task"))
        self.assertEqual(bound.work_id, "work")
        ticket = await self.admit(bound)
        await self.done(ticket)
        await self.usage.close()
        self.usage = UsageService(self.root, UsageSettings(max_requests=1))
        await self.usage.start()
        self.assertEqual((await self.usage.binding("run")).work_id, "work")
        with self.assertRaisesRegex(UsageError, "identity_conflict"):
            await self.usage.bind("run", UsageScope("other", "run"))
        with self.assertRaisesRegex(UsageError, "max_requests"):
            await self.admit(await self.usage.binding("run"))

    async def test_missing_usage_is_unknown_not_zero_and_can_continue(self):
        first = await self.admit()
        record = await self.done(first)
        self.assertIsNone(record["input_tokens"])
        self.assertEqual(record["usage_state"], "unknown")
        second = await self.admit()
        await self.done(second)
        self.assertEqual((await self.usage.snapshot("work"))["unknown_usage"], 2)

    async def test_reservations_bound_token_cost_without_oversubscription(self):
        self.policy(work_limits={"max_input_tokens": 100, "max_output_tokens": 20})
        ticket = await self.admit(bounds={"input_tokens": 80, "output_tokens": 10})
        with self.assertRaisesRegex(UsageError, "max_input_tokens"):
            await self.admit(bounds={"input_tokens": 30, "output_tokens": 5})
        await self.done(ticket, usage={"input_tokens": 50, "output_tokens": 5})
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 1)

    async def test_optional_unaccountable_budget_fails_closed(self):
        self.policy(work_limits={"max_cost_cny": 10})
        with self.assertRaisesRegex(UsageError, "unaccountable"):
            await self.admit()
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 0)

    async def test_price_receipt_is_estimate_and_retains_unknowns(self):
        self.policy(
            prices={
                "test": {
                    "provider": "provider",
                    "model": "model",
                    "input_cny_per_million": 2,
                    "output_cny_per_million": 4,
                }
            }
        )
        ticket = await self.admit()
        record = await self.done(ticket, usage={"input_tokens": 1000000, "output_tokens": 500000})
        self.assertEqual(record["cost_cny"], 4)
        self.assertEqual(record["cost_kind"], "estimated")
        self.assertIsNone((await self.done(await self.admit()))["cost_cny"])

    async def test_global_provider_rules_include_other_work(self):
        self.policy(
            rules=[
                {
                    "id": "provider-total",
                    "scope": "global",
                    "match": {"provider": "provider"},
                    "window_seconds": 60,
                    "limits": {"max_requests": 2},
                }
            ]
        )
        await self.done(await self.admit())
        await self.done(await self.admit(UsageScope("other", "other")))
        with self.assertRaisesRegex(UsageError, "provider-total"):
            await self.admit(UsageScope("third", "third"))

    async def test_concurrency_denial_is_transient_not_permanent_block(self):
        self.policy(work_limits={"concurrency": 1})
        first = await self.admit()
        with self.assertRaisesRegex(UsageError, "usage_busy"):
            await self.admit()
        self.assertIsNone((await self.usage.snapshot("work"))["blocked"])
        await self.done(first)
        await self.done(await self.admit())

    async def test_rate_limit_is_transient(self):
        self.policy(work_limits={"requests_per_second": 1})
        await self.done(await self.admit())
        with self.assertRaisesRegex(UsageError, "rate_limited"):
            await self.admit()
        self.assertIsNone((await self.usage.snapshot("work"))["blocked"])

    async def test_explicit_extension_is_audited_does_not_reset_counts(self):
        self.policy(max_requests=1)
        await self.done(await self.admit())
        with self.assertRaises(UsageError):
            await self.admit()
        result = await self.usage.call("extend", "work", 2, "Explicit operator approval")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["limit"], 3)
        await self.done(await self.admit())

    async def test_idempotent_settlement_and_not_sent_reservation_release(self):
        self.policy(max_requests=1)
        first = await self.admit()
        await self.usage.settle(first["id"], state="not_sent")
        await self.usage.settle(first["id"], state="completed", usage={"input_tokens": 999})
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 0)
        await self.done(await self.admit())

    async def test_unknown_after_restart_retains_reservation(self):
        await self.admit(bounds={"input_tokens": 80, "output_tokens": 10})
        await self.usage.close()
        self.usage = UsageService(self.root)
        await self.usage.start()
        result = await self.usage.snapshot("work")
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["unknown_requests"], 1)
        self.assertEqual(result["inflight"], 0)

    async def test_storage_error_blocks_without_sending(self):
        sent = []

        async def invoke():
            sent.append(True)
            return {}

        with patch.object(self.usage.ledger, "admit", side_effect=OSError("secret details")):
            with self.assertRaisesRegex(UsageError, "storage_unavailable"):
                await metered(self.usage, self.scope, "inference", "provider", "model", invoke)
        self.assertFalse(sent)
        self.assertFalse(self.usage.ready)

    async def test_admission_queue_bound_and_event_loop_remains_responsive(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        original = self.usage.ledger.snapshot

        def slow(work):
            entered.set()
            release.wait(2)
            return original(work)

        self.policy(queue_size=1)
        with patch.object(self.usage.ledger, "snapshot", side_effect=slow):
            task = asyncio.create_task(self.usage.snapshot("work"))
            while not entered.is_set():
                await asyncio.sleep(0)
            try:
                with self.assertRaisesRegex(UsageError, "usage_busy"):
                    await self.admit()
                await asyncio.wait_for(asyncio.sleep(0), 0.1)
            finally:
                release.set()
                await task

    async def test_bodies_disabled_then_opt_in_redacted_and_query_metadata(self):
        body = {"authorization": "PRIVATE", "text": "Bearer PRIVATE", "image": "PRIVATE"}
        record = await self.admit(body=body)
        self.assertNotIn("request_body", record)
        await self.done(record)
        self.policy(body_enabled=True)
        record = await self.admit(body=body)
        self.assertNotIn("PRIVATE", record["request_body"])
        await self.done(record)
        self.assertEqual(private_mode(self.usage.ledger.path), 0o600)

    async def test_retention_prunes_details_once_preserves_counts_and_active_unknown(self):
        old = time.time() - 100 * 86400
        with patch("assistant_runtime.core.ai_usage.ledger.time.time", return_value=old):
            await self.done(await self.admit(), usage={"input_tokens": 7, "output_tokens": 3})
        await self.usage.call("prune")
        await self.usage.call("prune")
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 1)
        self.assertEqual((await self.usage.call("query", {}))["items"], [])
        self.assertEqual((await self.usage.snapshot("work"))["input_tokens"], 7)
        self.policy(max_requests=1)
        with self.assertRaises(UsageError):
            await self.admit()

    async def test_cancelled_admission_is_released_before_any_dispatch(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        original = self.usage.ledger.admit

        def slow(*args):
            entered.set()
            release.wait(2)
            return original(*args)

        with patch.object(self.usage.ledger, "admit", side_effect=slow):
            task = asyncio.create_task(self.admit())
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((await self.usage.snapshot("work"))["requests"], 0)
        self.assertEqual((await self.usage.call("query", {}))["items"][0]["state"], "not_sent")

    async def test_detail_expiry_keeps_exact_dimension_statistics(self):
        old = time.time() - 100 * 86400
        with patch("assistant_runtime.core.ai_usage.ledger.time.time", return_value=old):
            await self.done(await self.admit(), usage={"input_tokens": 7, "output_tokens": 3})
        await self.done(await self.admit(), usage={"input_tokens": 11, "output_tokens": 5})
        await self.usage.call("prune")
        stats = (await self.usage.call("query", {"group_by": "provider"}))["items"][0]
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["input_tokens"], 18)
        daily = (await self.usage.call("query", {"group_by": "day"}))["items"]
        self.assertEqual(sum(r["requests"] for r in daily), 2)

    async def test_cached_and_reasoning_usage_are_preserved_without_double_counting(self):
        record = await self.done(
            await self.admit(),
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 80},
                "output_tokens_details": {"reasoning_tokens": 15},
            },
        )
        self.assertEqual(record["total_tokens"], 120)
        self.assertEqual(record["cached_input_tokens"], 80)
        self.assertEqual(record["reasoning_tokens"], 15)
        self.assertEqual((await self.usage.snapshot("work"))["input_tokens"], 100)

    async def test_second_owner_cannot_recover_or_reset_an_active_ledger(self):
        ticket = await self.admit()
        other = UsageService(self.root)
        try:
            await other.start()
            self.assertFalse(other.ready)
            with self.assertRaises(UsageError):
                await other.admit(self.scope, service="inference", provider="provider")
            records = await self.usage.call("query", {})
            self.assertEqual(records["items"][0]["id"], ticket["id"])
            self.assertEqual(records["items"][0]["state"], "submitted")
        finally:
            await other.close()

    async def test_config_checks_limits_rules_and_legacy_ownership(self):
        for config in (
            {"max_requests": True},
            {"work_limits": {"nope": 1}},
            {"rules": [{"id": "x", "scope": "global", "limits": {"max_requests": 2}}]},
            {"body_enabled": 1},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                UsageSettings.parse(config)
        self.assertEqual(UsageSettings.parse({}).max_requests, 256)


class BoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_is_read_only_and_task_identity_is_bound(self):
        from fastmcp import Client
        from assistant_runtime.core.ai_usage.tools import create_server

        class Calls:
            def __init__(self):
                self.requests = []

            async def invoke(self, request):
                self.requests.append(request)
                return {}

        calls = Calls()
        async with Client(create_server(calls, "task-run")) as client:
            tools = await client.list_tools()
            self.assertEqual(
                {t.name for t in tools}, {"ai_usage_status", "ai_usage_summary", "ai_usage_calls"}
            )
            await client.call_tool("ai_usage_summary", {"work_id": "other-work"})
        self.assertIsInstance(calls.requests[0], UsageRequest)
        self.assertEqual(calls.requests[0].caller_run, "task-run")

    async def test_shadow_decisions_preserve_explicit_host_scope(self):
        from test_decisions import ShadowTest, sample

        case = ShadowTest()
        await case.asyncSetUp()
        self.addAsyncCleanup(case.asyncTearDown)
        scope = UsageScope("event-batch", "event-check", "plan")
        case.plugin.enqueue(sample(), "assistant", scope)
        await case.settle()
        rows = await case.runtime.plugins["ai_usage"].service.call("query", {"work_id": "event-batch"})
        self.assertEqual(len(rows["items"]), 1)
        self.assertEqual(rows["items"][0]["task_id"], "plan")


class TaskBudgetTest(unittest.IsolatedAsyncioTestCase):
    from test_tasks import TasksTest as Fixtures

    asyncSetUp = Fixtures.asyncSetUp
    create = Fixtures.create

    async def asyncTearDown(self):
        # unittest async cleanups run after tearDown; release the ledger before
        # the shared fixture removes its instance directory (required on Windows).
        await self.host.ai_usage.service.close()
        await self.Fixtures.asyncTearDown(self)

    async def test_plan_pause_is_durable_before_next_scheduler_occurrence(self):
        from assistant_runtime.core.ai_usage.plugin import AIUsagePlugin
        from assistant_runtime.api.contracts import TaskRequest

        self.host.config.ai_usage = {"max_requests": 1}
        self.host.ai_usage = AIUsagePlugin(self.host)
        await self.host.ai_usage.service.start()
        self.addAsyncCleanup(self.host.ai_usage.service.close)
        task = await self.create(trigger={"kind": "interval", "seconds": 600})
        run = self.plugin.repo.enqueue(task, "isolated", "running")
        await self.host.ai_usage.bind_task(run)
        scope = await self.host.ai_usage.service.binding(run["id"])
        ticket = await self.host.ai_usage.service.admit(scope, service="execution", provider="provider")
        await self.host.ai_usage.service.settle(ticket["id"], state="completed")
        with self.assertRaises(UsageError):
            await self.host.ai_usage.service.admit(scope, service="execution", provider="provider")
        self.plugin.repo.update_definition(task["id"], next_at=time.time() - 1)
        before = len(self.plugin.repo.runs(task["id"]))
        self.plugin.due(task["id"])
        self.assertEqual(self.plugin.repo.definition(task["id"])["state"], "paused")
        self.assertEqual(len(self.plugin.repo.runs(task["id"])), before)
        await self.plugin.request(TaskRequest("resume", {"task_id": task["id"]}))
        self.assertIsNone(await self.host.ai_usage.service.call("plan_blocked", task["id"]))
        # Resuming the schedule does not replenish the old run allowance.
        with self.assertRaises(UsageError):
            await self.host.ai_usage.service.admit(scope, service="execution", provider="provider")

    async def test_preflight_and_parallel_review_share_the_task_work(self):
        from assistant_runtime.api.ai_usage import current_usage
        from assistant_runtime.core.ai_usage.plugin import AIUsagePlugin
        from unittest.mock import AsyncMock, Mock

        self.plugin.scheduler.pause()
        self.plugin.model_review.enqueue = Mock()
        self.host.ai_usage = AIUsagePlugin(self.host)
        await self.host.ai_usage.service.start()
        self.addAsyncCleanup(self.host.ai_usage.service.close)
        task = await self.create()
        run = self.plugin.repo.enqueue(task, "preflight", "queued")
        seen = []

        async def decision(*_):
            scope = current_usage.get()
            self.assertIsNotNone(scope)
            self.assertEqual(scope.task_id, task["id"])
            seen.append(scope.work_id)
            ticket = await self.host.ai_usage.service.admit(scope, service="decision", provider="typesafe")
            await self.host.ai_usage.service.settle(ticket["id"], state="completed")
            return True

        self.plugin.event_inbox.prepare = AsyncMock(side_effect=decision)
        await self.plugin.launch(run)
        self.plugin.model_review._evaluate = AsyncMock(side_effect=decision)
        await self.plugin.model_review.evaluate(run["id"])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual((await self.host.ai_usage.service.snapshot(seen[0]))["requests"], 2)

    async def test_task_bound_query_cannot_escape_or_increase_budget(self):
        from assistant_runtime.core.ai_usage.plugin import AIUsagePlugin

        self.host.ai_usage = AIUsagePlugin(self.host)
        await self.host.ai_usage.service.start()
        self.addAsyncCleanup(self.host.ai_usage.service.close)
        task = await self.create()
        run = self.plugin.repo.enqueue(task, "isolated", "running")
        await self.host.ai_usage.bind_task(run)
        scope = await self.host.ai_usage.service.binding(run["id"])
        ticket = await self.host.ai_usage.service.admit(scope, service="inference", provider="provider")
        await self.host.ai_usage.service.settle(ticket["id"], state="completed")
        other = await self.host.ai_usage.service.admit(
            UsageScope("other", "other", "other"), service="inference", provider="provider"
        )
        await self.host.ai_usage.service.settle(other["id"], state="completed")
        result = await self.host.ai_usage.request(UsageRequest("calls", {"work_id": "other"}, run["id"]))
        self.assertEqual([r["id"] for r in result["items"]], [ticket["id"]])
        with self.assertRaises(ValueError):
            await self.host.ai_usage.request(
                UsageRequest(
                    "extend",
                    {"work_id": scope.work_id, "requests": 100, "reason": "model request"},
                    run["id"],
                )
            )
