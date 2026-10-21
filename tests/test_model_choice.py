"""Active model correction: concrete candidates, user locks, bounded fallback."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_tasks as fixtures
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.plugins.decisions.settings import DecisionSettings
from assistant_runtime.plugins.decisions.plugin import DecisionsPlugin


class ChoiceTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TasksTest.asyncSetUp
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    def enable(self):
        self.plugin.model_policy.review_mode = "active"
        self.host.config.decisions.update(
            enabled=True,
            external_data_consent=True,
            mode="active",
            thresholds={"model_review": 0.2},
            active_timeout_seconds=3,
        )

    def recommendation(self, **changes):
        return {
            "profile": "fast",
            "reasoning_effort": "low",
            "reason": "Known workflow",
            "model_source": "assistant",
            "effort_source": "assistant",
            **changes,
        }

    def result(self, choice="deep_medium", probability=0.24):
        return {
            "status": "ok",
            "choice": choice,
            "probabilities": {choice: probability},
            "receipt_id": "test",
        }

    async def test_assistant_choice_can_change_and_receipt_is_not_a_second_review(self):
        self.enable()
        judge = AsyncMock(return_value=self.result())
        with patch("assistant_runtime.plugins.tasks.model_advice.decide", judge):
            d = await self.create(execution=self.recommendation())
        e = d["spec"]["execution"]
        self.assertEqual((e["model"], e["reasoning_effort"]), ("gpt-6.1-sol", "medium"))
        r = e["selection"]["model_review"]
        self.assertTrue(r["applied"])
        self.assertTrue(r["changed"])
        self.assertEqual(r["original"]["model"], "gpt-6-luna")
        self.assertEqual(e["selection"]["sources"]["model"], "jev")
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        self.plugin.model_review.enqueue(run)
        self.assertTrue(self.plugin.model_review.queue.empty())
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_review"]["mode"], "active")
        judge.assert_awaited_once()

    async def test_user_model_stays_fixed_but_unlocked_effort_can_change(self):
        self.enable()

        async def judge(runtime, request):
            self.assertEqual({o["model"] for o in request.state["options"].values()}, {"gpt-6-luna"})
            return self.result("fast_medium", 0.28)

        with patch("assistant_runtime.plugins.tasks.model_advice.decide", side_effect=judge):
            d = await self.create(execution=self.recommendation(model_source="user"))
        e = d["spec"]["execution"]
        self.assertEqual((e["model"], e["reasoning_effort"]), ("gpt-6-luna", "medium"))
        self.assertEqual(e["model_source"], "user")
        with patch("assistant_runtime.plugins.tasks.model_advice.decide", return_value=self.result()):
            d = await self.create(execution=self.recommendation(model_source="user"))
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertFalse(d["spec"]["execution"]["selection"]["model_review"]["applied"])

    async def test_user_and_legacy_explicit_choices_are_never_overwritten(self):
        self.enable()
        judge = AsyncMock(return_value=self.result())
        with patch("assistant_runtime.plugins.tasks.model_advice.decide", judge):
            for execution in (
                self.recommendation(model_source="user", effort_source="user"),
                {"model": "gpt-6-luna", "reasoning_effort": "low", "reason": "Legacy explicit choice"},
                self.recommendation(assessment={"user_preference": "Use the model I specified"}),
            ):
                d = await self.create(execution=execution)
                self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
                self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "low")
        judge.assert_not_awaited()

    async def test_threshold_timeout_and_disabled_keep_original_without_second_judgment(self):
        self.enable()
        for reply in (
            self.result(probability=0.19),
            self.result("invented", 0.99),
            {"status": "fallback", "choice": None, "reason": "transport_failed"},
        ):
            with patch("assistant_runtime.plugins.tasks.model_advice.decide", return_value=reply) as judge:
                d = await self.create(execution=self.recommendation())
                self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
                judge.assert_awaited_once()
        self.host.config.decisions["active_timeout_seconds"] = 0.01

        async def slow(*args):
            await asyncio.sleep(1)
            return self.result()

        with patch("assistant_runtime.plugins.tasks.model_advice.decide", side_effect=slow):
            d = await self.create(execution=self.recommendation())
        self.assertEqual(d["spec"]["execution"]["selection"]["model_review"]["decision"]["reason"], "timeout")
        self.host.config.decisions["enabled"] = False
        with patch("assistant_runtime.plugins.tasks.model_advice.decide") as judge:
            d = await self.create(execution=self.recommendation())
            judge.assert_not_called()
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")

    async def test_unrelated_update_cannot_reselect_and_user_lock_survives_execution_update(self):
        self.enable()
        d = await self.create(
            auto_archive=False, execution=self.recommendation(model_source="user", effort_source="user")
        )
        with patch("assistant_runtime.plugins.tasks.model_advice.decide") as judge:
            updated = await self.plugin.request(
                TaskRequest(
                    "update",
                    {"task_id": d["id"], "revision": d["revision"], "changes": {"title": "New title"}},
                )
            )
            judge.assert_not_called()
        self.assertEqual(updated["spec"]["execution"], d["spec"]["execution"])
        with self.assertRaisesRegex(ValueError, "user choice"):
            await self.plugin.request(
                TaskRequest(
                    "update",
                    {
                        "task_id": d["id"],
                        "revision": updated["revision"],
                        "changes": {
                            "execution": {
                                "model": "gpt-6.1-sol",
                                "model_source": "assistant",
                                "reason": "Agent preference",
                            }
                        },
                    },
                )
            )
        with patch("assistant_runtime.plugins.tasks.model_advice.decide") as judge:
            updated = await self.plugin.request(
                TaskRequest(
                    "update",
                    {
                        "task_id": d["id"],
                        "revision": updated["revision"],
                        "changes": {"execution": {"profile": "deep", "reason": "New recommendation"}},
                    },
                )
            )
            judge.assert_not_called()
        self.assertEqual(updated["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertEqual(updated["spec"]["execution"]["model_source"], "user")

    def test_only_model_review_accepts_lower_threshold_and_uses_candidate_probability(self):
        settings = DecisionSettings.parse({"thresholds": {"model_review": 0.2}})
        self.assertEqual(settings.thresholds["voice_intent"], 0.98)
        with self.assertRaises(ValueError):
            DecisionSettings.parse({"thresholds": {"voice_intent": 0.2}})
        for invalid in (-0.1, 1.1, float("nan"), True):
            with self.assertRaises(ValueError):
                DecisionSettings.parse({"thresholds": {"model_review": invalid}})
        plugin = object.__new__(DecisionsPlugin)
        plugin.settings = settings
        result = plugin.result(
            {
                "state": "completed",
                "kind": "model_review",
                "id": "receipt",
                "answer": {
                    "choice": "deep_medium",
                    "model": "jev-1.13.0",
                    "probabilities": {"deep_medium": 0.24},
                    "confidence": 0.19,
                },
            }
        )
        self.assertEqual(result["status"], "ok")

    async def test_native_deepseek_efforts_are_not_invented(self):
        self.enable()
        self.executor.models.return_value.append(
            {
                "model": "deepseek",
                "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("none", "low", "high", "max")],
            }
        )

        async def judge(runtime, request):
            self.assertEqual(
                {o["reasoning_effort"] for o in request.state["options"].values()},
                {"none", "low", "high", "max"},
            )
            self.assertEqual({o["model"] for o in request.state["options"].values()}, {"deepseek"})
            return self.result("custom_high", 0.3)

        with patch("assistant_runtime.plugins.tasks.model_advice.decide", side_effect=judge):
            d = await self.create(
                execution={
                    "model": "deepseek",
                    "model_source": "user",
                    "reasoning_effort": "low",
                    "effort_source": "assistant",
                    "reason": "User requested DeepSeek",
                }
            )
        self.assertEqual(d["spec"]["execution"]["model"], "deepseek")
        self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "high")

    async def test_cancellation_does_not_create_task_or_start_fallback(self):
        self.enable()
        entered = asyncio.Event()

        async def judge(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch("assistant_runtime.plugins.tasks.model_advice.decide", side_effect=judge):
            work = asyncio.create_task(self.create(execution=self.recommendation()))
            await entered.wait()
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM job_definitions").fetchone()[0], 0)
        self.executor.launch.assert_not_awaited()
