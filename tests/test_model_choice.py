"""Active model correction: concrete candidates, user locks, bounded fallback."""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_tasks as fixtures
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.plugins.decisions.settings import DecisionSettings
from assistant_runtime.plugins.decisions.plugin import DecisionsPlugin


class ChoiceTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TasksTest.asyncSetUp
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    def enable(self):
        self.runtime.plugins["decisions"] = self.runtime.plugins["pi"]
        self.plugin.model_policy.review_mode = "active"
        self.host.config.plugin_settings.setdefault("decisions", {}).update(
            enabled=True,
            external_data_consent=True,
            mode="active",
            thresholds={"model_review": 0.2},
            active_timeout_seconds=3,
        )

    def recommendation(self, **changes):
        return {
            "recommendation": "brief",
            "reasoning_effort": "low",
            "reason": "Known workflow",
            "model_source": "assistant",
            "effort_source": "assistant",
            **changes,
        }

    def result(self, choice="investigate_medium", probability=0.24):
        return {
            "status": "ok",
            "choice": choice,
            "probabilities": {choice: probability},
            "receipt_id": "test",
            "probability_threshold": 0.2,
        }

    async def test_assistant_choice_can_change_and_receipt_is_not_a_second_review(self):
        self.enable()
        judge = AsyncMock(return_value=self.result())
        with patch("assistant_runtime.core.tasks.model_advice.decide", judge):
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
            return self.result("brief_medium", 0.28)

        with patch("assistant_runtime.core.tasks.model_advice.decide", side_effect=judge):
            d = await self.create(
                execution=self.recommendation(
                    model_source="user",
                    reasoning_effort="auto",
                    assessment={"user_preference": "User specified the model; effort is unspecified"},
                )
            )
        e = d["spec"]["execution"]
        self.assertEqual((e["model"], e["reasoning_effort"]), ("gpt-6-luna", "medium"))
        self.assertEqual(e["model_source"], "user")
        with patch("assistant_runtime.core.tasks.model_advice.decide", return_value=self.result()):
            d = await self.create(execution=self.recommendation(model_source="user"))
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
        self.assertFalse(d["spec"]["execution"]["selection"]["model_review"]["applied"])

    async def test_user_and_legacy_explicit_choices_are_never_overwritten(self):
        self.enable()
        judge = AsyncMock(return_value=self.result())
        with patch("assistant_runtime.core.tasks.model_advice.decide", judge):
            for execution in (
                self.recommendation(model_source="user", effort_source="user"),
                {"model": "gpt-6-luna", "reasoning_effort": "low", "reason": "Legacy explicit choice"},
            ):
                d = await self.create(execution=execution)
                self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
                self.assertEqual(d["spec"]["execution"]["reasoning_effort"], "low")
        judge.assert_not_awaited()

    async def test_missing_effort_and_unspecified_preference_do_not_block_creation_or_review(self):
        self.enable()
        requested = self.recommendation(
            assessment={"user_preference": "用户要求后台任务推荐，未指定模型或思考深度。"}
        )
        del requested["reasoning_effort"]
        original = copy.deepcopy(requested)

        async def judge(runtime, request):
            self.assertEqual(request.state["assessment"], requested["assessment"])
            self.assertGreater(len({o["reasoning_effort"] for o in request.state["options"].values()}), 1)
            return self.result()

        with patch("assistant_runtime.core.tasks.model_advice.decide", side_effect=judge) as decision:
            d = await self.create(execution=requested)
        e = d["spec"]["execution"]
        self.assertEqual((e["model"], e["reasoning_effort"]), ("gpt-6.1-sol", "medium"))
        self.assertEqual(e["selection"]["model_review"]["protected"], {})
        self.assertNotIn("effort_source", e)
        self.assertNotIn("effort_source", e["selection"]["agent_request"])
        self.assertEqual(requested, original)
        decision.assert_awaited_once()

    async def test_preference_prose_is_advisory_without_keyword_classification(self):
        self.enable()
        for preference in ("未指定", "希望快一些", "Use the model I specified", "No model preference"):
            with (
                self.subTest(preference=preference),
                patch(
                    "assistant_runtime.core.tasks.model_advice.decide", return_value=self.result()
                ) as judge,
            ):
                d = await self.create(
                    execution=self.recommendation(assessment={"user_preference": preference})
                )
                e = d["spec"]["execution"]
                self.assertEqual(e["model"], "gpt-6.1-sol")
                self.assertEqual(e["selection"]["model_review"]["protected"], {})
                self.assertEqual(judge.await_args.args[1].state["assessment"]["user_preference"], preference)
                judge.assert_awaited_once()

    async def test_empty_recommendation_sources_normalize_in_all_modes_and_fallback(self):
        self.enable()
        for mode in ("off", "shadow", "active"):
            self.plugin.model_policy.review_mode = mode
            for value in (None, "auto"):
                with (
                    self.subTest(mode=mode, value=value),
                    patch(
                        "assistant_runtime.core.tasks.model_advice.decide",
                        return_value={"status": "fallback", "choice": None, "reason": "transport_failed"},
                    ) as judge,
                ):
                    requested = {
                        "model": value,
                        "reasoning_effort": value,
                        "model_source": "assistant",
                        "effort_source": "assistant",
                    }
                    d = await self.create(execution=requested)
                    e = d["spec"]["execution"]
                    self.assertEqual((e["model"], e["reasoning_effort"]), ("gpt-6-luna", "low"))
                    self.assertNotIn("model_source", e)
                    self.assertNotIn("effort_source", e)
                    if mode == "active":
                        judge.assert_awaited_once()
                    else:
                        judge.assert_not_awaited()
        self.runtime.plugins.pop("decisions")
        with patch("assistant_runtime.core.tasks.model_advice.decide") as judge:
            d = await self.create(execution={"model_source": "assistant", "effort_source": "assistant"})
            self.assertNotIn("effort_source", d["spec"]["execution"])
            judge.assert_not_called()

    async def test_user_sources_need_actual_choices_and_invalid_sources_still_fail(self):
        self.enable()
        with patch("assistant_runtime.core.tasks.model_advice.decide") as judge:
            for source, field in (("model_source", "model"), ("effort_source", "reasoning_effort")):
                for missing in ({}, {field: None}, {field: "auto"}):
                    with (
                        self.subTest(source=source, missing=missing),
                        self.assertRaisesRegex(ValueError, source),
                    ):
                        await self.create(execution={**missing, source: "user"})
                with self.assertRaisesRegex(ValueError, source):
                    await self.create(execution={source: "inferred"})
            # Even an explicit profile only chooses a model tier, not an effort.
            with self.assertRaisesRegex(ValueError, "effort_source"):
                await self.create(
                    execution={
                        "recommendation": "brief",
                        "reason": "User preference",
                        "effort_source": "user",
                    }
                )
            judge.assert_not_called()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM job_definitions").fetchone()[0], 0)

    async def test_threshold_timeout_and_disabled_keep_original_without_second_judgment(self):
        self.enable()
        for reply in (
            self.result(probability=0.19),
            self.result("invented", 0.99),
            {"status": "fallback", "choice": None, "reason": "transport_failed"},
        ):
            with patch("assistant_runtime.core.tasks.model_advice.decide", return_value=reply) as judge:
                d = await self.create(execution=self.recommendation())
                self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")
                judge.assert_awaited_once()
        with patch("assistant_runtime.core.tasks.model_advice.decide", side_effect=TimeoutError):
            d = await self.create(execution=self.recommendation(reasoning_effort="auto"))
        self.assertEqual(d["spec"]["execution"]["selection"]["model_review"]["decision"]["reason"], "timeout")
        self.runtime.plugins.pop("decisions")
        with patch("assistant_runtime.core.tasks.model_advice.decide") as judge:
            d = await self.create(execution=self.recommendation())
            judge.assert_not_called()
        self.assertEqual(d["spec"]["execution"]["model"], "gpt-6-luna")

    async def test_unrelated_update_cannot_reselect_and_user_lock_survives_execution_update(self):
        self.enable()
        d = await self.create(
            auto_archive=False, execution=self.recommendation(model_source="user", effort_source="user")
        )
        with patch("assistant_runtime.core.tasks.model_advice.decide") as judge:
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
        with patch("assistant_runtime.core.tasks.model_advice.decide") as judge:
            updated = await self.plugin.request(
                TaskRequest(
                    "update",
                    {
                        "task_id": d["id"],
                        "revision": updated["revision"],
                        "changes": {
                            "execution": {
                                "recommendation": "investigate",
                                "model_source": "user",
                                "reason": "User selected a new recommendation",
                            }
                        },
                    },
                )
            )
            judge.assert_not_called()
        self.assertEqual(updated["spec"]["execution"]["model"], "gpt-6.1-sol")
        self.assertEqual(updated["spec"]["execution"]["model_source"], "user")

    def test_global_threshold_and_explicit_override_use_candidate_probability(self):
        settings = DecisionSettings.parse({"thresholds": {"model_review": 0.2}})
        self.assertEqual(settings.policy("voice_intent")["threshold"], 0.6)
        self.assertEqual(
            DecisionSettings.parse({"thresholds": {"voice_intent": 0.2}}).policy("voice_intent")["threshold"],
            0.2,
        )
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
                    "choice": "investigate_medium",
                    "model": "jev-1.13.0",
                    "probabilities": {"investigate_medium": 0.24},
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
                "supported_reasoning_efforts": [
                    {"reasoning_effort": e} for e in ("none", "low", "high", "max")
                ],
            }
        )

        async def judge(runtime, request):
            self.assertEqual(
                {o["reasoning_effort"] for o in request.state["options"].values()},
                {"none", "low", "high", "max"},
            )
            self.assertEqual({o["model"] for o in request.state["options"].values()}, {"deepseek"})
            return self.result("selected_high", 0.3)

        with patch("assistant_runtime.core.tasks.model_advice.decide", side_effect=judge):
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

        with patch("assistant_runtime.core.tasks.model_advice.decide", side_effect=judge):
            work = asyncio.create_task(self.create(execution=self.recommendation()))
            await entered.wait()
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM job_definitions").fetchone()[0], 0)
        self.executor.launch.assert_not_awaited()
