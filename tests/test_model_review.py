"""Shadow review preserves execution, provenance and bounded lifecycle."""

import asyncio
import copy
import unittest
from unittest.mock import patch

import test_tasks as fixtures
from assistant_runtime.kernel.contracts import DecisionSample
from assistant_runtime.plugins.decisions.questions import prepare
from assistant_runtime.plugins.tasks.model_review import assessment, evidence
from assistant_runtime.plugins.tasks.selection import ModelPolicy


class ReviewTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TasksTest.asyncSetUp
    asyncTearDown = fixtures.TasksTest.asyncTearDown
    create = fixtures.TasksTest.create

    async def staged(self, **kwargs):
        d = await self.create(**kwargs)
        self.plugin.due(d["id"])
        run = self.plugin.repo.runs(d["id"])[0]
        return self.plugin.repo.update_run(run["id"], "dispatching")

    async def test_facts_are_validated_and_prior_evidence_is_read_from_task_owner(self):
        prior = await self.staged()
        self.plugin.repo.update_run(prior["id"], "completed", progress="Reported done, not independent proof")
        d = await self.create(
            execution={
                "assessment": {
                    "acceptance": "Read back destination files",
                    "procedure": "Known transfer API",
                    "uncertainties": ["Candidate identity conflicts"],
                    "prior_run_id": prior["id"],
                    "user_preference": "User said use Sol",
                }
            }
        )
        e = d["spec"]["execution"]
        self.assertEqual(
            e["selection"]["prior_execution"]["reported_summary"], "Reported done, not independent proof"
        )
        self.assertEqual(e["assessment"]["user_preference"], "User said use Sol")
        with self.assertRaises(ValueError):
            await self.create(execution={"assessment": {"source": "user_verified"}})
        with self.assertRaises(ValueError):
            assessment({"uncertainties": ["x"] * 5})
        other = await self.staged()
        with self.assertRaisesRegex(ValueError, "parent task scope"):
            evidence(self.plugin, {"prior_run_id": prior["id"]}, other["id"], str(self.root))
        with self.assertRaisesRegex(ValueError, "different workspace"):
            evidence(self.plugin, {"prior_run_id": prior["id"]}, None, "/elsewhere")

    async def test_blind_full_pair_review_disagrees_without_changing_execution(self):
        run = await self.staged(
            execution={
                "profile": "fast",
                "reason": "agent believes easy",
                "assessment": {
                    "acceptance": "Verify identity and destination",
                    "uncertainties": ["Conflicting versions"],
                },
            }
        )
        original = copy.deepcopy(run["data"]["spec"]["execution"])
        self.host.config.decisions.update(enabled=True, external_data_consent=True, mode="active")
        review = self.plugin.model_review
        review.enqueue(run)
        self.assertEqual(review.queue.get_nowait(), run["id"])

        async def judgment(runtime, request):
            self.assertEqual(request.kind, "model_review")
            self.assertNotIn("agent believes easy", str(request.state))
            self.assertIn("deep_medium", request.state["options"])
            prepared = prepare(
                DecisionSample(request.kind, request.reference, request.state), "tasks", "jev-1.13.0"
            )
            self.assertIn("deep_medium", prepared["choices"])
            return {"status": "ok", "choice": "deep_medium", "receipt_id": "receipt"}

        with patch("assistant_runtime.plugins.tasks.model_review.decide", side_effect=judgment):
            await review.evaluate(run["id"])
        updated = self.plugin.repo.run(run["id"])
        self.assertEqual(updated["data"]["spec"]["execution"], original)
        self.assertEqual(updated["state"], "dispatching")
        self.assertTrue(updated["data"]["model_review"]["disagrees"])
        self.assertFalse(updated["data"]["model_review"]["applied"])
        self.assertFalse(updated["data"]["model_review"]["user_preference_verified"])
        trace = self.plugin.trace.read({"run_id": run["id"]})
        self.assertEqual(trace["model_review"]["recommended"]["reasoning_effort"], "medium")
        self.executor.launch.assert_not_awaited()

    async def test_native_efforts_and_fixed_constraints_filter_review_candidates(self):
        self.plugin.model_policy.profiles["balanced"] = {"model": "deepseek", "reasoning_effort": "high"}
        self.executor.models.return_value.append(
            {
                "model": "deepseek",
                "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("none", "low", "high", "max")],
            }
        )
        run = await self.staged()
        self.host.config.decisions.update(enabled=True, external_data_consent=True)
        self.plugin.settings.execution = {"model": "deepseek", "reasoning_effort": "auto"}
        review = self.plugin.model_review
        review.enqueue(run)
        review.queue.get_nowait()

        async def judgment(runtime, request):
            self.assertEqual({o["model"] for o in request.state["options"].values()}, {"deepseek"})
            self.assertNotIn("medium", {o["reasoning_effort"] for o in request.state["options"].values()})
            return {"status": "fallback", "choice": None, "reason": "low_confidence"}

        with patch("assistant_runtime.plugins.tasks.model_review.decide", side_effect=judgment):
            await review.evaluate(run["id"])
        result = self.plugin.repo.run(run["id"])["data"]["model_review"]
        self.assertIsNone(result["recommended"])
        self.assertIsNone(result["disagrees"])

    async def test_disabled_saturation_and_restart_never_replay(self):
        run = await self.staged()
        review = self.plugin.model_review
        review.enqueue(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_review"]["state"], "skipped")
        self.assertTrue(review.queue.empty())
        self.host.config.decisions.update(enabled=True, external_data_consent=True)
        run = await self.staged()
        for i in range(16):
            review.queue.put_nowait(str(i))
        review.enqueue(run)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_review"]["reason"], "queue_full")
        while not review.queue.empty():
            review.queue.get_nowait()
        run = await self.staged()
        review.enqueue(run)
        review.recover()
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_review"]["state"], "interrupted")
        # Recovery records unfinished observations, never calls a provider.
        self.executor.launch.assert_not_awaited()
        with self.assertRaises(ValueError):
            ModelPolicy.parse({"review_mode": "active"})

    async def test_slow_review_does_not_block_launch_and_failure_is_isolated(self):
        self.host.config.decisions.update(enabled=True, external_data_consent=True)
        gate = asyncio.Event()

        async def blocked(*args):
            await gate.wait()
            raise RuntimeError("synthetic failure")

        with patch.object(self.plugin.model_review, "evaluate", side_effect=blocked):
            run = await self.staged()
            self.plugin.repo.update_run(run["id"], "queued")
            await asyncio.wait_for(self.plugin.launch(self.plugin.repo.run(run["id"])), 1)
            self.executor.launch.assert_awaited_once()
            gate.set()
            await asyncio.wait_for(self.plugin.model_review.queue.join(), 1)
        self.assertEqual(self.plugin.repo.run(run["id"])["data"]["model_review"]["state"], "fallback")

    async def test_identical_facts_reuse_reference_and_invalid_pair_cannot_be_applied(self):
        first = await self.staged()
        second = await self.staged()
        self.host.config.decisions.update(enabled=True, external_data_consent=True)
        review = self.plugin.model_review
        references = []

        async def judgment(runtime, request):
            references.append(request.reference)
            return {"status": "ok", "choice": "invented_medium"}

        with patch("assistant_runtime.plugins.tasks.model_review.decide", side_effect=judgment):
            for run in (first, second):
                review.enqueue(run)
                review.queue.get_nowait()
                await review.evaluate(run["id"])
                result = self.plugin.repo.run(run["id"])["data"]["model_review"]
                self.assertEqual(result["decision"]["reason"], "invalid_review_candidate")
                self.assertFalse(result["applied"])
        self.assertEqual(references[0], references[1])
