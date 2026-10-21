"""Real shadow worker/storage with isolated HTTP, lifecycle and ingress tests."""

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from fastmcp import Client

from assistant_runtime.assistant.dialogue import Dialogue
from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.config import Config
from assistant_runtime.kernel.contracts import (
    Control,
    DecisionRequest,
    DecisionSample,
    DecisionQuery,
    DecisionOutcome,
    TaskRequest,
    EventRequest,
)
from assistant_runtime.kernel.decisions import decide, fallback
from assistant_runtime.mcp.decisions import create_server
from assistant_runtime.mcp.catalog import DECISION_TOOLS
from assistant_runtime.plugins.codex.mcp import server_config
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.decisions.jev import DecisionError, JevProvider, answer, credential
from assistant_runtime.plugins.decisions.plugin import DecisionsPlugin
from assistant_runtime.plugins.decisions.questions import VERSIONS, prepare, question
from assistant_runtime.plugins.decisions.settings import DecisionSettings
from assistant_runtime.store import Store


def sample(reference="one", **changes):
    return DecisionSample(
        "voice_intent", reference, {"utterance": "退下吧", "execution_active": False}, **changes
    )


def model_state(objective="科研文献检索与归纳"):
    return {
        "objective": objective,
        "options": {
            f"{profile}_{effort}": {"profile": profile, "model": model, "reasoning_effort": effort}
            for profile, model, effort in (
                ("fast", "gpt-6-luna", "low"),
                ("balanced", "gpt-6.1-sol", "low"),
                ("deep", "gpt-6.1-sol", "medium"),
                ("ultra", "gpt-6-astra", "high"),
            )
        },
    }


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.failure = None
        self.choice = None
        self.probability = 1.0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def start(self):
        pass

    def reload(self):
        pass

    async def close(self):
        self.closed = True

    async def warmup(self):
        pass

    def diagnostics(self):
        return {}

    async def evaluate(self, job):
        self.calls.append(copy.deepcopy(job))
        self.entered.set()
        await self.release.wait()
        if self.failure:
            raise self.failure
        choice = self.choice or ("sleep" if job["kind"] == "voice_intent" else "uncertain")
        result = {
            "model": "jev-1.13.0",
            "choice": choice,
            "confidence": 0.9,
            "probabilities": {
                k: self.probability
                if k == choice
                else (1 - self.probability) / (len(job["question"]["criteria"]) - 1)
                for k in job["question"]["criteria"]
            },
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        if job["kind"] == "model_profile":
            result["fields"] = {}
            for field in ("profile", "reasoning_effort"):
                probabilities = {"uncertain": result["probabilities"]["uncertain"]}
                for ident, option in job["input"]["options"].items():
                    value = option[field]
                    probabilities[value] = probabilities.get(value, 0) + result["probabilities"][ident]
                winner = max(probabilities, key=probabilities.get)
                result["fields"][field] = {"choice": winner, "probabilities": probabilities}
        return result


class ShadowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "host.sqlite3")
        self.config = Config(root=self.root, decisions={"mode": "shadow", "external_data_consent": True})
        self.runtime = Runtime(self.store)
        self.provider = FakeProvider()
        self.plugin = DecisionsPlugin(SimpleNamespace(config=self.config), self.provider)
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    async def settle(self):
        await asyncio.wait_for(self.plugin.events.join(), 2)
        await asyncio.wait_for(self.plugin.queue.join(), 2)

    async def test_passive_bounded_observer_deduplicates_and_never_dispatches(self):
        self.provider.release.clear()
        self.runtime.publish(
            SimpleNamespace(
                kind="decision.observed", source="assistant", generation=1, data={"sample": sample()}
            )
        )
        await self.provider.entered.wait()
        controlled = []
        self.runtime.register(
            "decisions", Control, lambda command: asyncio.sleep(0, result=controlled.append(command.kind))
        )
        await asyncio.wait_for(self.runtime.invoke(Control("stop")), 0.2)
        self.assertEqual(controlled, ["stop"])
        self.assertEqual(
            set(self.runtime.handlers), {DecisionRequest, DecisionQuery, DecisionOutcome, Control}
        )
        self.assertFalse(self.plugin.diagnostics()["execution_enabled"])
        self.provider.release.set()
        await self.settle()
        self.plugin.enqueue(sample(), "assistant")
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        receipts = await self.plugin.request(DecisionRequest("list"))
        self.assertEqual(len(receipts["items"]), 1)
        self.assertEqual(receipts["items"][0]["state"], "completed")
        raw = (self.root / "state/decisions/shadow.sqlite3").read_bytes()
        self.assertNotIn("退下吧".encode(), raw)

    async def test_queue_overload_expiry_and_oversize_are_explicit(self):
        self.provider.release.clear()
        self.plugin.enqueue(sample("running"), "assistant")
        await self.provider.entered.wait()
        for index in range(self.plugin.settings.queue_size - 1):
            self.assertTrue(self.plugin.enqueue(sample(str(index)), "assistant")["accepted"])
        self.assertEqual(self.plugin.enqueue(sample("overflow"), "assistant")["reason"], "queue_full")
        self.assertEqual(self.plugin.counts["queue_dropped"], 1)
        self.provider.release.set()
        await self.settle()
        self.plugin.enqueue(sample("expired", observed_at=0), "assistant")
        await self.settle()
        rows = (await self.plugin.request(DecisionRequest("list")))["items"]
        self.assertEqual(rows[0]["state"], "expired")
        oversized = DecisionSample(
            "voice_intent", "big", {"utterance": "中" * 6000, "execution_active": False}
        )
        self.assertFalse(self.plugin.enqueue(oversized, "assistant")["accepted"])

    async def test_usage_survives_restart_and_uncertain_attempt_is_not_retried(self):
        self.provider.release.clear()
        self.plugin.enqueue(sample(), "assistant")
        await self.provider.entered.wait()
        await self.runtime.close()
        self.assertTrue(self.provider.closed)
        provider = FakeProvider()
        plugin = DecisionsPlugin(SimpleNamespace(config=self.config), provider)
        self.runtime = Runtime(self.store)
        self.runtime.add(plugin)
        await self.runtime.start()
        plugin.enqueue(sample(), "assistant")
        plugin.enqueue(sample("two"), "assistant")
        await asyncio.wait_for(plugin.queue.join(), 2)
        rows = await plugin.request(DecisionRequest("list"))
        self.assertEqual([r["state"] for r in rows["items"]], ["completed", "unknown"])
        self.assertEqual([call["reference"] for call in provider.calls], ["two"])
        self.assertEqual((await plugin.request(DecisionRequest("status")))["attempts_today"], 2)

    async def test_authentication_failure_suspends_until_explicit_reload_without_replay(self):
        self.provider.failure = DecisionError("authentication_failed")
        self.plugin.enqueue(sample(), "assistant")
        await self.settle()
        self.plugin.enqueue(sample("two"), "assistant")
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(self.plugin.auth_blocked)
        self.provider.failure = None
        await self.plugin.request(DecisionRequest("reload"))
        self.plugin.enqueue(sample("three"), "assistant")
        await self.settle()
        self.assertEqual(len(self.provider.calls), 2)
        self.assertFalse(self.plugin.auth_blocked)

    async def test_failure_backoff_and_errors_do_not_leak_provider_exceptions(self):
        self.provider.failure = DecisionError("timeout")
        self.plugin.enqueue(sample(), "assistant")
        await self.settle()
        self.plugin.enqueue(sample("two"), "assistant")
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        rows = (await self.plugin.request(DecisionRequest("list")))["items"]
        self.assertEqual(rows[0]["state"], "backoff_skipped")
        self.plugin.retry_at = 0
        self.provider.failure = RuntimeError("private-token-never-log")
        self.plugin.enqueue(sample("three"), "assistant")
        await self.settle()
        self.assertNotIn("private-token", json.dumps(await self.plugin.request(DecisionRequest("status"))))
        self.assertEqual(self.runtime.contexts["decisions"].state, "ready")

    async def test_labels_pagination_and_bounded_receipts_are_evaluation_only(self):
        self.plugin.settings.retention = 3
        for number in range(5):
            self.plugin.enqueue(sample(str(number), baseline="stop"), "assistant")
        await self.settle()
        page = await self.plugin.request(DecisionRequest("list", {"limit": 2}))
        self.assertEqual(len(page["items"]), 2)
        ident = page["items"][0]["id"]
        await self.plugin.request(DecisionRequest("label", {"id": ident, "expected": "sleep"}))
        status = await self.plugin.request(DecisionRequest("status"))
        self.assertEqual(status["records"], 3)
        group = status["groups"]["assistant:voice_intent"]
        self.assertEqual(group["accuracy"], 1)
        self.assertEqual(group["baseline_agreed"], 0)
        self.assertEqual(group["baseline_compared"], 3)
        second = await self.plugin.request(DecisionRequest("list", {"before": page["before"]}))
        self.assertEqual(len(second["items"]), 1)
        with self.assertRaises(ValueError):
            await self.plugin.request(DecisionRequest("label", {"id": ident, "expected": "execute"}))

    async def test_unknown_sources_and_unregistered_event_kinds_do_not_invoke_provider(self):
        for source, kind in (("other", "decision.observed"), ("assistant", "events.committed")):
            self.runtime.publish(
                SimpleNamespace(kind=kind, source=source, generation=1, data={"sample": sample()})
            )
        await self.settle()
        self.assertEqual(self.provider.calls, [])

    async def test_manual_fixture_labels_do_not_become_live_accuracy(self):
        manual = self.plugin.enqueue(sample(), "manual")
        self.plugin.enqueue(sample(), "assistant")
        await self.settle()
        await self.plugin.request(DecisionRequest("label", {"id": manual["id"], "expected": "sleep"}))
        groups = (await self.plugin.request(DecisionRequest("status")))["groups"]
        self.assertEqual(groups["manual:voice_intent"]["accuracy"], 1)
        self.assertIsNone(groups["assistant:voice_intent"]["accuracy"])

    async def test_restart_after_committed_intent_never_replays_unconfirmed_call(self):
        job = prepare(sample(), "assistant", self.plugin.settings.model)
        self.assertTrue(await self.plugin.receipts.call("claim", job))
        await self.runtime.close()  # Simulate a process lost after intent, before a response receipt.
        provider = FakeProvider()
        plugin = DecisionsPlugin(SimpleNamespace(config=self.config), provider)
        self.runtime = Runtime(self.store)
        self.runtime.add(plugin)
        await self.runtime.start()
        plugin.enqueue(sample(), "assistant")
        await asyncio.wait_for(plugin.queue.join(), 2)
        row = await plugin.request(DecisionRequest("get", {"id": job["id"]}))
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(provider.calls, [])

    async def test_slow_receipt_query_does_not_hold_the_main_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        original = self.plugin.receipts.operation

        def slow(action, *args):
            if action == "status":
                entered.set()
                release.wait(2)
            return original(action, *args)

        with patch.object(self.plugin.receipts, "operation", side_effect=slow):
            query = asyncio.create_task(self.plugin.request(DecisionRequest("status")))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
                self.assertFalse(query.done())
            finally:
                release.set()
                await query

    async def test_storage_backlog_is_bounded_under_slow_queries(self):
        entered, release = threading.Event(), threading.Event()
        original = self.plugin.receipts.operation

        def slow(action, *args):
            entered.set()
            release.wait(2)
            return original(action, *args)

        with patch.object(self.plugin.receipts, "operation", side_effect=slow):
            queries = [asyncio.create_task(self.plugin.request(DecisionRequest("status"))) for _ in range(16)]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertEqual(self.plugin.receipts.pending, 16)
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    await self.plugin.request(DecisionRequest("status"))
            finally:
                release.set()
                await asyncio.gather(*queries)


class ActiveTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = ShadowTest.asyncSetUp
    asyncTearDown = ShadowTest.asyncTearDown
    settle = ShadowTest.settle

    async def query(self, reference="active", **changes):
        self.plugin.settings.mode = "active"
        self.plugin.settings.external_data_consent = True
        request = DecisionQuery("voice_intent", reference, sample().state, **changes)
        return await self.runtime.invoke(request)

    async def test_usable_result_is_cached_and_active_bus_does_not_duplicate_http(self):
        result = await self.query()
        self.assertEqual(result["choice"], "sleep")
        self.assertEqual(result["probability_threshold"], 0.6)
        cached = await self.query()
        self.assertEqual(result["receipt_id"], cached["receipt_id"])
        self.assertNotEqual(result["decision_id"], cached["decision_id"])
        self.runtime.contexts["decisions"].publish("decision.observed", sample=sample("ignored"))
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(self.plugin.diagnostics()["active_enabled"])

    async def test_low_probability_uncertain_and_errors_fall_back(self):
        self.provider.probability = 0.59
        self.assertEqual((await self.query("low"))["reason"], "low_confidence")
        self.provider.probability = 1
        self.provider.choice = "uncertain"
        self.assertEqual((await self.query("unknown"))["reason"], "low_confidence")
        self.provider.failure = DecisionError("authentication_failed")
        self.assertEqual((await self.query("auth"))["reason"], "authentication_failed")
        self.assertEqual((await self.query("blocked"))["reason"], "credential_unavailable")
        self.provider.failure = None
        await self.plugin.reload()
        self.assertEqual((await self.query("auth"))["reason"], "authentication_failed")
        self.assertEqual(len(self.provider.calls), 3)  # Reload never replays a failed identity.

    async def test_ultra_advice_uses_normal_confidence_gate(self):
        self.plugin.settings.mode = "active"
        self.provider.choice = "ultra_high"
        for reference, probability, expected in (("low", 0.59, "fallback"), ("high", 0.99, "ok")):
            self.provider.probability = probability
            result = await self.runtime.invoke(
                DecisionQuery(
                    "model_profile", reference, model_state("极复杂的原创理论证明，需要验证交叉约束")
                )
            )
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["choice"], "ultra_high" if expected == "ok" else None)

    async def test_usage_count_is_informational_and_does_not_limit_calls(self):
        for index in range(121):
            result = await self.query(f"usage:{index}")
            self.assertEqual(result["status"], "ok")
        status = await self.plugin.request(DecisionRequest("status"))
        self.assertEqual(len(self.provider.calls), 121)
        self.assertEqual(status["attempts_today"], 121)
        self.assertNotIn("daily_limit", status)

    async def test_disabled_no_consent_and_missing_plugin_preserve_original_route(self):
        self.plugin.settings.mode = "active"
        self.plugin.settings.external_data_consent = False
        request = DecisionQuery("voice_intent", "no-consent", sample().state)
        self.assertEqual((await self.runtime.invoke(request))["reason"], "external_data_not_authorized")
        self.assertFalse(self.plugin.enqueue(sample(), "manual")["accepted"])
        self.plugin.settings.enabled = False
        self.assertEqual((await self.runtime.invoke(request))["reason"], "disabled")
        self.assertEqual(self.provider.calls, [])
        absent = Runtime(self.store)
        self.assertEqual((await decide(absent, request))["fallback"], "use_original_logic")
        for response in (None, {"status": "ok"}, {"status": "ok", "choice": None}):
            broken = SimpleNamespace(invoke=AsyncMock(return_value=response))
            self.assertEqual((await decide(broken, request))["reason"], "invalid_decision_result")

    async def test_disabled_start_does_not_construct_optional_http_provider(self):
        await self.runtime.close()
        self.config.decisions = {"enabled": False}
        plugin = DecisionsPlugin(SimpleNamespace(config=self.config))
        self.runtime = Runtime(self.store)
        self.runtime.add(plugin)
        with patch("assistant_runtime.plugins.decisions.jev.JevProvider", side_effect=AssertionError):
            await self.runtime.start()
        self.assertIsNone(plugin.provider)
        self.assertEqual((await plugin.judge(DecisionQuery("selection", "x", {})))["reason"], "disabled")

    async def test_coalescing_busy_and_timeout_have_no_automatic_retries(self):
        self.plugin.settings.active_timeout_seconds = 0.1
        self.provider.release.clear()
        first = asyncio.create_task(self.query("slow"))
        await self.provider.entered.wait()
        same = asyncio.create_task(self.query("slow"))
        self.assertEqual((await self.query("another"))["reason"], "timeout")
        results = await asyncio.wait_for(asyncio.gather(first, same), 0.5)
        self.assertTrue(all(r["status"] == "fallback" for r in results))
        self.assertTrue(all(r["reason"] == "timeout" for r in results))
        await self.settle()
        self.plugin.retry_at = 0
        self.provider.release.set()
        self.assertEqual((await self.query("slow"))["reason"], "timeout")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertFalse(self.plugin.waiters)

    async def test_caller_cancellation_propagates_and_result_does_not_start_fallback_work(self):
        self.provider.release.clear()
        caller = asyncio.create_task(self.query("canceled"))
        await self.provider.entered.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.provider.release.set()
        await self.settle()
        self.assertEqual((await self.query("canceled"))["choice"], "sleep")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_slow_receipt_io_does_not_extend_caller_deadline(self):
        entered, release = threading.Event(), threading.Event()
        original = self.plugin.receipts.operation
        self.plugin.settings.active_timeout_seconds = 0.1

        def slow(action, *args):
            if action == "find":
                entered.set()
                release.wait(2)
            return original(action, *args)

        with patch.object(self.plugin.receipts, "operation", side_effect=slow):
            try:
                started = time.monotonic()
                result = await asyncio.wait_for(self.query("slow-storage"), 0.4)
                self.assertTrue(entered.is_set())
                self.assertEqual(result["reason"], "timeout")
                self.assertLess(time.monotonic() - started, 0.4)
                self.assertLessEqual(self.plugin.receipts.pending, 2)
                self.assertEqual(self.provider.calls, [])
            finally:
                release.set()
                await self.plugin.receipts.call("status")

    async def test_background_context_is_checked_and_obvious_private_fields_never_leave(self):
        handler = AsyncMock(return_value={"run": {"state": "completed"}})
        self.runtime.register("decisions", TaskRequest, handler)
        self.assertEqual((await self.query("background", caller_run="run"))["reason"], "caller_not_active")
        handler.return_value = {"run": {"state": "running"}}
        self.assertEqual((await self.query("background", caller_run="run"))["choice"], "sleep")
        private = DecisionQuery(
            "selection",
            "private",
            {
                "question": "choose",
                "context": {"nested": {"Authorization": "private"}},
                "options": {"yes": "Yes"},
            },
        )
        self.assertEqual((await self.runtime.invoke(private))["status"], "fallback")
        self.assertEqual(len(self.provider.calls), 1)


class McpDecisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_main_background_schema_context_and_failure_fallback(self):
        values = {
            "request_id": "1b1100e5-614b-445f-a9f6-105d80de06f1",
            "question": "Which flow?",
            "context": {"evidence": "The interface changed"},
            "options": {"inspect": "Read current interface", "continue": "Use known flow"},
        }
        for caller in (None, "background-run"):
            handler = SimpleNamespace(
                invoke=AsyncMock(
                    side_effect=[
                        {"enabled": True, "external_data_consent": True},
                        fallback("timeout"),
                    ]
                )
            )
            async with Client(create_server(handler, caller)) as client:
                tools = {t.name: t for t in await client.list_tools()}
                self.assertEqual(set(tools), set(DECISION_TOOLS))
                self.assertTrue(tools["judge_options"].annotations.read_only_hint)
                result = (await client.call_tool("judge_options", values)).structured_content
                self.assertEqual(result["fallback"], "use_original_logic")
                request = handler.invoke.await_args.args[0]
                self.assertEqual(request.caller_run, caller)
                self.assertEqual(request.state["options"], values["options"])
                handler.invoke.side_effect = RuntimeError("private transport detail")
                result = (await client.call_tool("judge_options", values)).structured_content
                self.assertEqual(result["status"], "fallback")
                self.assertNotIn("private", json.dumps(result))
                handler.invoke.side_effect = None
                handler.invoke.return_value = {"enabled": True, "external_data_consent": False}
                handler.invoke.reset_mock()
                result = (await client.call_tool("judge_options", values)).structured_content
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(handler.invoke.await_count, 1)
        cfg = Config(root=Path("/tmp"))
        self.assertIn("decisions", server_config(cfg.socket, decisions=True))
        self.assertFalse(server_config(cfg.socket, enabled=False, decisions=True)["decisions"]["enabled"])


class HttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".env").write_text('TYPESAFE_API_KEY="test-local-key"\nOTHER_SECRET=do-not-export\n')
        self.seen = []
        self.connections = []
        self.warmup_entered = asyncio.Event()
        self.warmup_release = asyncio.Event()
        self.warmup_release.set()
        self.status = 200
        self.mode = "normal"

        async def handle(request):
            self.connections.append((request.method, id(request.transport)))
            self.seen.append(
                (
                    request.headers.get("Authorization"),
                    await request.json() if request.can_read_body else None,
                )
            )
            if request.method == "GET":
                self.warmup_entered.set()
                await self.warmup_release.wait()
            if self.mode == "timeout":
                await asyncio.sleep(0.3)
            if self.mode == "redirect":
                return web.Response(status=302, headers={"Location": "/other"})
            if self.mode == "large":
                return web.Response(body=b"x" * 70000)
            body = json.dumps(
                {
                    "model": "jev-1.13.0",
                    "answers": {
                        "decision": {
                            "type": "choice",
                            "choice": "sleep",
                            "confidence": 0.8,
                            "probabilities": {
                                k: float(k == "sleep") for k in question("voice_intent")["criteria"]
                            },
                        }
                    },
                    "usage": {"input_tokens": 123, "output_tokens": 45},
                }
            ).encode()
            payload = self.seen[-1][1]
            if payload and set(payload["questions"]) == {"profile", "effort"}:
                data = json.loads(body)
                data["answers"] = {
                    key: {
                        "type": "choice",
                        "choice": value,
                        "confidence": 0.99,
                        "probabilities": {
                            name: float(name == value) for name in payload["questions"][key]["criteria"]
                        },
                    }
                    for key, value in (("profile", "ultra"), ("effort", "high"))
                }
                if self.mode == "missing_effort":
                    data["answers"].pop("effort")
                body = json.dumps(data).encode()
            if self.mode == "chunked":
                response = web.StreamResponse()
                await response.prepare(request)
                await response.write(body[:20])
                await asyncio.sleep(0.01)
                await response.write(body[20:])
                await response.write_eof()
                return response
            return web.Response(body=body, status=self.status, content_type="application/json")

        app = web.Application()
        app.router.add_post("/", handle)
        app.router.add_get("/models", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        address = self.site._server.sockets[0].getsockname()
        self.provider = JevProvider(
            self.root, DecisionSettings(timeout_seconds=0.1, external_data_consent=True)
        )
        self.provider.endpoint = f"http://127.0.0.1:{address[1]}/"
        self.provider.models_endpoint = self.provider.endpoint + "models"
        with patch.dict(os.environ, {}, clear=True):
            self.provider.reload()
        await self.provider.start()

    async def asyncTearDown(self):
        self.warmup_release.set()
        await self.provider.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_real_http_framing_and_credentials_are_private(self):
        self.mode = "chunked"
        result = await self.provider.evaluate(prepare(sample(), "test", "jev-1.13.0"))
        self.assertEqual(result["choice"], "sleep")
        self.assertEqual(self.seen[0][0], "Bearer test-local-key")
        payload = self.seen[0][1]
        self.assertNotIn("reference", payload["state"])
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)
        self.assertNotIn("OTHER_SECRET", os.environ)
        self.assertEqual(payload["model"], "jev-1.13.0")

    async def test_one_http_request_carries_two_small_questions_and_validates_both_answers(self):
        job = prepare(DecisionSample("model_profile", "batch", model_state()), "tasks", "jev-1.13.0")
        result = await self.provider.evaluate(job)
        self.assertEqual(len(self.seen), 1)
        questions = self.seen[0][1]["questions"]
        self.assertEqual(set(questions), {"profile", "effort"})
        self.assertEqual(result["choice"], "ultra_high")
        self.assertEqual(result["fields"]["profile"]["choice"], "ultra")
        self.assertEqual(result["fields"]["reasoning_effort"]["choice"], "high")
        self.assertEqual(result["usage"], {"input_tokens": 123, "output_tokens": 45})
        self.assertNotIn("usage", result["fields"]["profile"])
        self.mode = "missing_effort"
        with self.assertRaisesRegex(DecisionError, "invalid_response"):
            await self.provider.evaluate(job)
        self.assertEqual(len(self.seen), 2)

    async def test_auth_rate_limit_timeout_redirect_and_oversized_response_are_bounded(self):
        job = prepare(sample(), "test", "jev-1.13.0")
        for mode, status, code in (
            ("normal", 401, "authentication_failed"),
            ("normal", 429, "rate_limited"),
            ("redirect", 200, "upstream_unavailable"),
            ("large", 200, "response_too_large"),
            ("timeout", 200, "timeout"),
        ):
            with self.subTest(mode=mode, status=status):
                self.mode, self.status = mode, status
                before = len(self.seen)
                with self.assertRaisesRegex(DecisionError, code):
                    await self.provider.evaluate(job)
                self.assertEqual(len(self.seen), before + 1)  # No implicit retries.


class ContractTest(unittest.TestCase):
    def test_verified_api_none_effort_is_preserved_in_joint_questions(self):
        state = {
            "objective": "确定性的 API 核验",
            "options": {"fast_none": {"profile": "fast", "model": "api-model", "reasoning_effort": "none"}},
        }
        job = prepare(DecisionSample("model_profile", "api-none", state), "tasks", "jev-1.13.0")
        self.assertIn("fast_none", job["choices"])
        from assistant_runtime.plugins.decisions.questions import execution_questions

        self.assertEqual(set(execution_questions(job)["effort"]["criteria"]), {"none", "uncertain"})

    def test_execution_options_are_closed_bounded_and_not_arbitrary_model_output(self):
        for mutation in (
            lambda state: state.pop("options"),
            lambda state: state.update(options={}),
            lambda state: state["options"]["fast_low"].update(reasoning_effort="invented"),
            lambda state: state["options"]["fast_low"].update(profile="ultra"),
            lambda state: state["options"]["fast_low"].update(extra="unsupported"),
        ):
            state = model_state()
            mutation(state)
            with self.assertRaises(ValueError):
                prepare(DecisionSample("model_profile", "invalid", state), "tasks", "jev-1.13.0")

    def test_new_model_profile_question_invalidates_only_its_old_cache(self):
        model_sample = DecisionSample("model_profile", "same", model_state())
        current = prepare(model_sample, "tasks", "jev-1.13.0")
        voice = prepare(sample(), "assistant", "jev-1.13.0")
        with patch.dict(VERSIONS, {"model_profile": 3}):
            old = prepare(model_sample, "tasks", "jev-1.13.0")
            self.assertEqual(prepare(sample(), "assistant", "jev-1.13.0")["id"], voice["id"])
        self.assertNotEqual(current["id"], old["id"])
        self.assertEqual(current["question_version"], 6)
        self.assertEqual(
            set(current["choices"]), {"fast_low", "balanced_low", "deep_medium", "ultra_high", "uncertain"}
        )

    def test_configuration_and_question_contract_are_strict(self):
        for values in (
            {"mode": "invalid"},
            {"daily_limit": 100},
            {"timeout_seconds": float("nan")},
            {"api_key": "never-in-config"},
            {"endpoint": "http://elsewhere"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                DecisionSettings.parse(values)
        for kind in ("voice_intent", "event_relevance"):
            self.assertIn("uncertain", question(kind)["criteria"])
        with self.assertRaises(ValueError):
            prepare(DecisionSample("execute", "one", {}), "test", "jev-1.13.0")

    def test_invalid_provider_decisions_and_numbers_are_rejected(self):
        valid = {
            "model": "jev-1.13.0",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "sleep",
                    "confidence": 0.8,
                    "probabilities": {"sleep": 1.0, "uncertain": 0.0},
                }
            },
        }
        for field, value in (
            ("choice", "shell"),
            ("confidence", float("nan")),
            ("probabilities", {"sleep": True, "uncertain": 0}),
            ("probabilities", {"sleep": 0.1, "uncertain": 0.9}),
        ):
            invalid = copy.deepcopy(valid)
            invalid["answers"]["decision"][field] = value
            with self.subTest(field=field), self.assertRaises(DecisionError):
                answer(invalid, {"sleep": "end", "uncertain": "unknown"}, "jev-1.13.0")

    def test_dotenv_does_not_expand_or_export_other_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".env").write_text('TYPESAFE_API_KEY="literal-${OTHER}"\nOTHER=private\n')
            self.assertEqual(credential(root), "literal-${OTHER}")
            self.assertNotIn("OTHER", os.environ)
            with patch.dict(os.environ, TYPESAFE_API_KEY="environment-precedence"):
                self.assertEqual(credential(root), "environment-precedence")


class VoiceIngressTest(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_control_ignores_late_epoch_input_focus_and_fallback(self):
        service = AssistantService.__new__(AssistantService)
        service.voice = SimpleNamespace(epoch=7)
        service.active = True
        service.quiet_active = lambda: False
        service.input_count = 3
        service.conversation = SimpleNamespace(voice_revision=12)
        service.backend = SimpleNamespace(session_id="main", active_turn="turn")
        service.control = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(request):
            entered.set()
            await release.wait()
            return {"status": "ok", "choice": "sleep"}

        service.runtime = SimpleNamespace(invoke=delayed)
        for attr, value in (("epoch", 8), ("input_count", 4), ("voice_revision", 13)):
            task = asyncio.create_task(service._voice_decision("请结束一下", 7, 3, "turn", 12))
            await entered.wait()
            owner = (
                service.voice
                if attr == "epoch"
                else service.conversation
                if attr == "voice_revision"
                else service
            )
            old = getattr(owner, attr)
            setattr(owner, attr, value)
            release.set()
            await task
            service.control.assert_not_awaited()
            setattr(owner, attr, old)
            entered.clear()
            release.clear()
        service.runtime = SimpleNamespace(invoke=AsyncMock(return_value=fallback("timeout")))
        await service._voice_decision("请结束一下", 7, 3, "turn", 12)
        service.control.assert_not_awaited()
        service.runtime.invoke.return_value = {"status": "ok", "choice": "cancel"}
        service.backend.active_turn = "new-turn"
        await service._voice_decision("取消刚才的任务", 7, 3, "turn", 12)
        service.control.assert_not_awaited()
        service.backend.active_turn = "turn"
        await service._voice_decision("取消刚才的任务", 7, 3, "turn", 12)
        service.control.assert_not_awaited()  # No scoped coordinator: never pretend takeover succeeded.

    async def test_cancel_control_passes_captured_turn_after_playback_wait(self):
        service = AssistantService.__new__(AssistantService)
        service.player = SimpleNamespace(stop=AsyncMock())
        service.backend = SimpleNamespace(cancel=AsyncMock(return_value=False))
        service.speak = AsyncMock()
        await service.control(Control("cancel", "captured-turn"))
        service.backend.cancel.assert_awaited_once_with(expected_turn="captured-turn")

    async def test_real_voice_owner_publishes_sample_and_exits_without_waiting_for_judge(self):
        service = AssistantService.__new__(AssistantService)
        service.mode = "listening"
        service.events = asyncio.Queue()
        service.voice = SimpleNamespace(epoch=7)
        service.active = True
        service.quiet_active = lambda: False
        service.dialogue = Dialogue()
        service.config = SimpleNamespace(wake_words=["嘟嘟"])
        service.store = SimpleNamespace(transcript=Mock())
        service.backend = SimpleNamespace(session_id="main", busy=True)
        service.input_count = 0
        service.context = SimpleNamespace(publish=Mock())
        service.control = AsyncMock()
        worker = asyncio.create_task(service._events())
        try:
            await service.events.put(
                {
                    "epoch": 7,
                    "kind": "transcript",
                    "role": "user",
                    "text": "退下吧",
                    "message_id": "utterance",
                }
            )
            await asyncio.wait_for(service.events.join(), 0.2)
            service.control.assert_awaited_once_with(Control("sleep"))
            observed = service.context.publish.call_args.kwargs["sample"]
            self.assertEqual(observed.baseline, "sleep")
            self.assertEqual(observed.state["utterance"], "退下吧")
            self.assertIn("utterance", observed.reference)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


class EventIngressTest(unittest.IsolatedAsyncioTestCase):
    import test_task_events as fixtures

    asyncSetUp = fixtures.TaskEventsTest.asyncSetUp
    asyncTearDown = fixtures.TaskEventsTest.asyncTearDown
    open_runtime = fixtures.TaskEventsTest.open_runtime
    create = fixtures.TaskEventsTest.create
    emit = fixtures.TaskEventsTest.emit

    async def test_relevance_skip_retains_events_and_fallback_keeps_original_launch(self):
        definition = await self.create()
        event = await self.emit("semantic")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(definition["id"])[0]
        self.config.decisions = {"external_data_consent": True}
        judge = AsyncMock(return_value={"status": "ok", "choice": "irrelevant", "receipt_id": "receipt"})
        self.runtime.register("tasks", DecisionQuery, judge)
        self.assertFalse(await self.inbox.prepare(run))
        final = self.tasks.repo.run(run["id"])
        self.assertEqual(final["data"]["skip_reason"], "semantic_irrelevant")
        self.assertTrue((await self.events.request(EventRequest("get", {"id": event["id"]})))["record"])
        judge.return_value = fallback("timeout")
        await self.emit("next")
        await self.inbox.cycle()
        next_run = self.tasks.repo.runs(definition["id"])[0]
        self.assertTrue(await self.inbox.prepare(next_run))
        self.assertEqual(self.tasks.repo.run(next_run["id"])["data"]["decision_support"]["reason"], "timeout")

    async def test_adoption_logging_cannot_overwrite_cancellation(self):
        definition = await self.create()
        await self.emit("cancel-during-adoption")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(definition["id"])[0]
        self.config.decisions = {"external_data_consent": True}
        self.runtime.register(
            "tasks",
            DecisionQuery,
            AsyncMock(
                return_value={
                    "status": "ok",
                    "choice": "irrelevant",
                    "decision_id": "a" * 32,
                }
            ),
        )
        recorded = asyncio.Event()

        async def outcome(request):
            self.tasks.repo.update_run(run["id"], "canceled")
            recorded.set()
            return {}

        self.runtime.register("tasks", DecisionOutcome, outcome)
        self.assertFalse(await self.inbox.prepare(run))
        await asyncio.wait_for(recorded.wait(), 1)
        self.assertEqual(self.tasks.repo.run(run["id"])["state"], "canceled")
        self.executor.launch.assert_not_awaited()

    async def test_event_deletion_and_run_cancellation_during_judgment_block_dispatch(self):
        definition = await self.create()
        event = await self.emit("deleted-while-awaiting")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(definition["id"])[0]
        self.config.decisions = {"external_data_consent": True}

        async def deleting(request):
            await self.events.request(EventRequest("delete", {"id": event["id"], "revision": 1}))
            return fallback("decision_service_unavailable")

        judge = AsyncMock(side_effect=deleting)
        self.runtime.register("tasks", DecisionQuery, judge)
        self.assertFalse(await self.inbox.prepare(run))
        self.assertEqual(self.tasks.repo.run(run["id"])["data"]["skip_reason"], "events_no_longer_valid")
        await self.emit("canceled-while-awaiting")
        await self.inbox.cycle()
        run = self.tasks.repo.runs(definition["id"])[0]

        async def canceling(request):
            self.tasks.repo.update_run(run["id"], "canceled")
            return {"status": "ok", "choice": "relevant", "receipt_id": "receipt"}

        judge.side_effect = canceling
        self.assertFalse(await self.inbox.prepare(run))
        self.assertEqual(self.tasks.repo.run(run["id"])["state"], "canceled")
        self.executor.launch.assert_not_awaited()

    async def test_only_committed_inbox_admission_is_observed_without_changing_dispatch(self):
        queue = self.runtime.subscribe("shadow-fixture")
        definition = await self.create()
        event = await self.emit("observed")

        def fail(*args, **kwargs):
            raise RuntimeError("transaction rolled back")

        with patch.object(self.tasks.repo, "enqueue", side_effect=fail):
            await self.inbox.cycle()
        while not queue.empty():
            self.assertNotEqual(queue.get_nowait().kind, "decision.observed")
        with self.store.db:
            self.store.db.execute("UPDATE job_subscriptions SET retry_at=0")
        await self.inbox.cycle()
        observations = []
        while not queue.empty():
            item = queue.get_nowait()
            if item.kind == "decision.observed":
                observations.append(item.data["sample"])
        self.assertEqual(len(observations), 1)
        self.assertIn(event["id"], observations[0].reference)
        self.assertEqual(observations[0].kind, "event_relevance")
        self.assertEqual(len(self.tasks.repo.runs(definition["id"])), 1)
        self.executor.launch.assert_not_awaited()


class ModelIngressTest(unittest.IsolatedAsyncioTestCase):
    import test_tasks as fixtures

    asyncSetUp = fixtures.TasksTest.asyncSetUp
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    def register_advice(self, choice):
        self.config.decisions = {"external_data_consent": True}
        judge = AsyncMock(return_value={"status": "ok", "choice": choice, "receipt_id": "joint"})
        self.runtime.register("tasks", DecisionQuery, judge)
        return judge

    async def test_partial_confidence_adopts_only_the_trusted_unresolved_field(self):
        judge = self.register_advice("ultra_high")
        judge.return_value["field_choices"] = {"profile": "ultra", "reasoning_effort": None}
        selected = (await self.create())["spec"]["execution"]
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("ultra", "high"))
        self.assertEqual(
            selected["selection"]["sources"], {"model": "jev", "reasoning_effort": "profile_default"}
        )
        self.assertIsNone(selected["selection"]["decision_support"]["recommended"]["reasoning_effort"])
        judge.return_value["field_choices"] = {"profile": None, "reasoning_effort": "high"}
        selected = (await self.create())["spec"]["execution"]
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("fast", "high"))
        self.assertEqual(selected["selection"]["sources"]["model"], "profile_default")

    async def test_unresolved_field_without_confidence_or_incompatible_pair_falls_back(self):
        judge = self.register_advice("deep_high")
        judge.return_value["field_choices"] = {"profile": "deep", "reasoning_effort": None}
        selected = (await self.create(execution={"profile": "deep", "reason": "模型已明确"}))["spec"][
            "execution"
        ]
        self.assertEqual(selected["reasoning_effort"], "medium")
        self.assertEqual(selected["selection"]["decision_support"]["reason"], "low_confidence")
        judge.return_value["field_choices"] = {"profile": "fast", "reasoning_effort": "ultra"}
        selected = (await self.create())["spec"]["execution"]
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("fast", "low"))

    async def test_joint_advice_can_vary_effort_without_losing_profile(self):
        judge = self.register_advice("deep_high")
        definition = await self.create()
        execution = definition["spec"]["execution"]
        self.assertEqual((execution["profile"], execution["reasoning_effort"]), ("deep", "high"))
        self.assertEqual(execution["selection"]["sources"], {"model": "jev", "reasoning_effort": "jev"})
        self.assertEqual(judge.await_count, 1)
        options = judge.await_args.args[0].state["options"]
        self.assertNotIn("balanced_high", options)  # Same concrete execution as deep_high.
        self.assertIn("ultra_high", options)
        self.assertEqual(options["ultra_high"]["model"], "gpt-6-astra")

    async def test_explicit_profile_only_allows_effort_advice(self):
        judge = self.register_advice("deep_high")
        definition = await self.create(execution={"profile": "deep", "reason": "困难推理但不需要 Astra"})
        selected = definition["spec"]["execution"]
        self.assertEqual(selected["profile"], "deep")
        self.assertEqual(selected["reasoning_effort"], "high")
        self.assertEqual(
            selected["selection"]["sources"], {"model": "profile_default", "reasoning_effort": "jev"}
        )
        self.assertTrue(
            all(o["profile"] == "deep" for o in judge.await_args.args[0].state["options"].values())
        )

    async def test_explicit_effort_is_preserved_while_profile_is_suggested(self):
        judge = self.register_advice("deep_high")
        definition = await self.create(execution={"reasoning_effort": "high", "reason": "需要困难推理"})
        selected = definition["spec"]["execution"]
        self.assertEqual(selected["selection"]["sources"]["reasoning_effort"], "agent")
        self.assertEqual(selected["reasoning_effort"], "high")
        self.assertTrue(
            all(o["reasoning_effort"] == "high" for o in judge.await_args.args[0].state["options"].values())
        )

    async def test_fixed_model_and_custom_agent_model_only_receive_supported_effort_advice(self):
        judge = self.register_advice("ultra_medium")
        self.plugin.settings.execution = {"model": "gpt-6-astra", "reasoning_effort": "auto"}
        definition = await self.create()
        selected = definition["spec"]["execution"]
        self.assertEqual((selected["model"], selected["reasoning_effort"]), ("gpt-6-astra", "medium"))
        self.assertEqual(selected["profile"], "ultra")
        self.assertEqual(selected["selection"]["sources"]["model"], "instance_fixed")
        self.assertTrue(
            all(o["model"] == "gpt-6-astra" for o in judge.await_args.args[0].state["options"].values())
        )
        self.plugin.settings.execution["model"] = "auto"
        self.executor.models.return_value.append(
            {
                "model": "custom",
                "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "high")],
            }
        )
        judge.return_value["choice"] = "custom_high"
        definition = await self.create(execution={"model": "custom", "reason": "用户指定独立研究模型"})
        selected = definition["spec"]["execution"]
        self.assertEqual(
            (selected["model"], selected["profile"], selected["reasoning_effort"]),
            ("custom", "custom", "high"),
        )

    async def test_fixed_effort_is_preserved_while_profile_is_suggested(self):
        judge = self.register_advice("deep_high")
        self.plugin.settings.execution = {"model": "auto", "reasoning_effort": "high"}
        definition = await self.create()
        selected = definition["spec"]["execution"]
        self.assertEqual(selected["selection"]["sources"]["reasoning_effort"], "instance_fixed")
        self.assertEqual(selected["reasoning_effort"], "high")
        self.assertTrue(
            all(o["reasoning_effort"] == "high" for o in judge.await_args.args[0].state["options"].values())
        )

    async def test_disabled_shadow_catalog_failure_and_invalid_choice_keep_original_fields(self):
        judge = self.register_advice("ultra_high")
        explicit = {"profile": "deep", "reason": "只使用当前指定模型"}
        for config in ({"enabled": False}, {"mode": "shadow"}, {"external_data_consent": False}):
            self.config.decisions = {"external_data_consent": True, **config}
            selected = (await self.create(execution=explicit))["spec"]["execution"]
            self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("deep", "medium"))
        judge.assert_not_awaited()
        self.config.decisions = {"external_data_consent": True}
        self.executor.models.side_effect = TimeoutError()
        selected = (await self.create(execution=explicit))["spec"]["execution"]
        self.assertEqual(selected["selection"]["decision_support"]["reason"], "model_catalog_unavailable")
        self.executor.models.side_effect = None
        selected = (await self.create(execution=explicit))["spec"]["execution"]
        self.assertEqual(selected["selection"]["decision_support"]["reason"], "invalid_execution_option")
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("deep", "medium"))

    async def test_single_supported_choice_is_local_and_invalid_explicit_effort_is_not_hidden(self):
        judge = self.register_advice("deep_high")
        self.executor.models.return_value = [
            {"model": "gpt-6.1-sol", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}
        ]
        selected = (await self.create(execution={"profile": "deep", "reason": "已确定模型"}))["spec"][
            "execution"
        ]
        self.assertEqual(selected["reasoning_effort"], "medium")
        self.assertEqual(selected["selection"]["sources"]["reasoning_effort"], "catalog_only_option")
        judge.assert_not_awaited()
        self.executor.select.side_effect = ValueError("Unsupported reasoning effort")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            await self.create(
                execution={"profile": "deep", "reasoning_effort": "invalid", "reason": "explicit"}
            )

    async def test_auto_selection_records_advice_but_explicit_and_fixed_choices_bypass(self):
        self.config.decisions = {"external_data_consent": True}
        judge = AsyncMock(return_value={"status": "ok", "choice": "balanced_low", "receipt_id": "receipt"})
        self.runtime.register("tasks", DecisionQuery, judge)
        definition = await self.create()
        execution = definition["spec"]["execution"]
        self.assertEqual(execution["profile"], "balanced")
        self.assertEqual(execution["selection"]["decision_support"]["receipt_id"], "receipt")
        await self.create(
            execution={"profile": "deep", "reasoning_effort": "medium", "reason": "需要诊断冲突证据"}
        )
        self.plugin.settings.execution = {"model": "fixed", "reasoning_effort": "low"}
        definition = await self.create()
        self.assertEqual(definition["spec"]["execution"]["model"], "fixed")
        self.assertEqual(judge.await_count, 1)

    async def test_ultra_advice_reaches_task_policy_and_explicit_ultra_bypasses_judgment(self):
        self.config.decisions = {"external_data_consent": True}
        judge = AsyncMock(return_value={"status": "ok", "choice": "ultra_high", "receipt_id": "research"})
        self.runtime.register("tasks", DecisionQuery, judge)
        definition = await self.create()
        selected = definition["spec"]["execution"]
        self.assertEqual((selected["profile"], selected["reasoning_effort"]), ("ultra", "high"))
        self.assertEqual(selected["selection"]["decision_support"]["receipt_id"], "research")
        await self.create(
            execution={
                "profile": "ultra",
                "reasoning_effort": "high",
                "reason": "极复杂的跨学科原创证明，需要高强度推理",
            }
        )
        self.assertEqual(judge.await_count, 1)

        self.executor.select.side_effect = ValueError("Research effort unavailable")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.create(execution={"profile": "ultra", "reason": "明确指定科研推理档位"})

    async def test_timeout_and_unavailable_suggestion_use_original_policy(self):
        self.config.decisions = {"external_data_consent": True}
        judge = AsyncMock(return_value=fallback("timeout"))
        self.runtime.register("tasks", DecisionQuery, judge)
        definition = await self.create()
        self.assertEqual(definition["spec"]["execution"]["profile"], "fast")
        self.assertEqual(
            definition["spec"]["execution"]["selection"]["decision_support"]["reason"], "timeout"
        )
        judge.return_value = {"status": "ok", "choice": "balanced_low", "receipt_id": "receipt"}

        async def select(model, effort):
            if model == "gpt-6.1-sol":
                raise ValueError("Model unavailable")
            return {"model": model, "reasoning_effort": effort}

        self.executor.select.side_effect = select
        definition = await self.create()
        execution = definition["spec"]["execution"]
        self.assertEqual(execution["profile"], "fast")
        self.assertEqual(
            execution["selection"]["decision_support"]["reason"], "suggested_execution_unavailable"
        )
