"""Final route rejections after queue admission release capacity without unknown dispatch."""

import asyncio
import unittest

from assistant_runtime.api import CapabilityRequestError
import test_execution_service as fixtures


class RouteAdmissionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ExecutionServiceTest.asyncSetUp
    asyncTearDown = fixtures.ExecutionServiceTest.asyncTearDown

    async def test_changed_route_after_capacity_wait_is_not_dispatched_and_slot_is_reusable(self):
        admission = self.service.admission
        first = await admission.start(
            {"session_id": "busy"}, lambda: asyncio.sleep(0, result={"run": {"id": "busy-turn"}})
        )
        invoked = []

        async def validate_at_dispatch():
            invoked.append("validation")
            raise CapabilityRequestError("model_binding_changed", "The route changed while queued")

        pending = asyncio.create_task(admission.start({"session_id": "queued"}, validate_at_dispatch))
        async with asyncio.timeout(2):
            while "queued" not in self.service.sessions:
                await asyncio.sleep(0)
        self.assertEqual(invoked, [])
        admission.release(first["execution_id"])
        with self.assertRaises(CapabilityRequestError):
            await pending
        record = self.service.records.get(self.service.sessions["queued"])
        self.assertEqual(record["state"], "interrupted")
        self.assertEqual(record["data"]["error"]["code"], "model_binding_changed")
        self.assertEqual(admission.slots.active, 0)
        self.assertEqual(self.client.calls, [])
        await admission.start(
            {"session_id": "next"}, lambda: asyncio.sleep(0, result={"run": {"id": "next-turn"}})
        )
        self.assertEqual(admission.slots.active, 1)
