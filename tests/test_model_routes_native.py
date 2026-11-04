"""Route and effort facts using real pinned Pi processes, synthetic local providers."""

import asyncio
import unittest

from assistant_runtime.api import CapabilityRequestError
import test_pi_skeleton as pi_fixtures


class RouteNativeTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pi_fixtures.PiSkeletonTest.asyncSetUp
    asyncTearDown = pi_fixtures.PiSkeletonTest.asyncTearDown
    session = pi_fixtures.PiSkeletonTest.session
    completed = pi_fixtures.PiSkeletonTest.completed

    async def test_two_models_execute_in_parallel_with_frozen_route_and_effort_receipts(self):
        a, b = await asyncio.gather(self.session(), self.session(model="model-b"))
        routes = [self.settings.route(model) for model in ("model-a", "model-b")]
        results = await asyncio.gather(
            *[
                self.client.request(
                    "run.start",
                    {
                        "session_id": session,
                        "model": route["model"],
                        "effort": effort,
                        "expected_model": {k: route[k] for k in ("model", "revision")},
                        "input": [{"type": "text", "text": "synthetic route check"}],
                    },
                )
                for session, route, effort in zip((a, b), routes, ("auto", "high"))
            ]
        )
        finished = [await self.completed(), await self.completed()]
        self.assertEqual({r["route"]["model"] for r in finished}, {"model-a", "model-b"})
        self.assertEqual({r["reasoning_effort"] for r in finished}, {"auto", "high"})
        self.assertEqual({r["route"]["revision"] for r in results}, {r["revision"] for r in routes})
        self.assertEqual(len(self.gateway.requests), 2)
        # Historical route facts survive model reconfiguration and a worker replacement.
        await self.client.request("session.close", {"session_id": a})
        self.settings.entries["model-a"]["model"] = "changed-upstream"
        saved = await self.client.request("session.read", {"session_id": a, "include_runs": True})
        self.assertEqual(saved["session"]["runs"][-1]["route"], routes[0])

    async def test_changed_or_disabled_route_never_reaches_synthetic_supplier(self):
        session = await self.session()
        route = self.settings.route("model-a")
        condition = {k: route[k] for k in ("model", "revision")}
        self.settings.entries["model-a"]["model"] = "changed-upstream"
        with self.assertRaises(CapabilityRequestError):
            await self.client.request(
                "run.start",
                {
                    "session_id": session,
                    "model": "model-a",
                    "effort": "low",
                    "expected_model": condition,
                    "input": [{"type": "text", "text": "must not dispatch"}],
                },
            )
        self.assertEqual(len(self.gateway.requests), 0)
        self.assertFalse(self.client.workers[session].meta.get("active_run"))
