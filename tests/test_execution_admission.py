"""Shared foreground/background admission, cancellation and terminal ownership."""

import asyncio
import base64
from unittest.mock import AsyncMock

import unittest
import test_execution_service as fixtures
from assistant_runtime.api import FileRequest


class SharedAdmissionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ExecutionServiceTest.asyncSetUp
    asyncTearDown = fixtures.ExecutionServiceTest.asyncTearDown

    async def test_all_entrances_share_slots_and_queued_cancel_never_dispatches(self):
        admission = self.service.admission
        calls = []

        async def dispatch(session):
            calls.append(session)
            await self.service.event(
                {"kind": "run.started", "data": {"session_id": session, "run": {"id": session + "-turn"}}}
            )
            return {"run": {"id": session + "-turn"}}

        first = await admission.start(
            {
                "session_id": "chat",
                "model": "test",
                "effort": "low",
                "execution_context": {"source": "conversation", "reference": "message"},
            },
            lambda: dispatch("chat"),
        )
        waiting = asyncio.create_task(admission.start({"session_id": "task"}, lambda: dispatch("task")))
        await asyncio.sleep(0)
        await admission.cancel(
            {"session_id": "task"}, AsyncMock(side_effect=AssertionError("must not dispatch"))
        )
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            await waiting
        self.assertEqual(calls, ["chat"])
        self.assertEqual(self.service.records.get(self.service.sessions["task"])["state"], "cancelled")
        await self.service.event(
            {
                "kind": "run.completed",
                "data": {
                    "session_id": "chat",
                    "run": {"id": "chat-turn", "status": "completed", "items": []},
                },
            }
        )
        await admission.start({"session_id": "task-next"}, lambda: dispatch("task-next"))
        self.assertEqual(calls, ["chat", "task-next"])
        self.assertEqual(self.service.records.get(first["execution_id"])["state"], "completed")
        self.assertEqual(len(admission.leases), 1)
        # A late old completion is consumed without changing the task/dialogue projections.
        self.assertTrue(
            await self.service.event(
                {
                    "kind": "run.completed",
                    "data": {
                        "session_id": "chat",
                        "run": {"id": "chat-turn", "status": "failed", "items": []},
                    },
                }
            )
        )
        self.assertEqual(self.service.records.get(first["execution_id"])["state"], "completed")

    async def test_terminal_event_before_start_ack_cannot_open_a_second_attempt(self):
        admission = self.service.admission
        completed, acknowledge = asyncio.Event(), asyncio.Event()

        async def dispatch():
            await self.service.event(
                {
                    "kind": "run.completed",
                    "data": {
                        "session_id": "same",
                        "run": {"id": "first", "status": "completed", "items": []},
                    },
                }
            )
            completed.set()
            await acknowledge.wait()
            return {"run": {"id": "first"}}

        first = asyncio.create_task(admission.start({"session_id": "same"}, dispatch))
        try:
            await completed.wait()
            with self.assertRaisesRegex(RuntimeError, "active attempt"):
                await admission.start(
                    {"session_id": "same"}, AsyncMock(side_effect=AssertionError("must not dispatch"))
                )
        finally:
            acknowledge.set()
            await first
        self.assertFalse(admission.dispatching)

    async def test_uncertain_dispatch_is_not_replayed_or_overwritten(self):
        callback = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await self.service.admission.start({"session_id": "lost"}, callback)
        ident = self.service.sessions["lost"]
        self.assertEqual(self.service.records.get(ident)["state"], "unknown")
        self.assertFalse(self.service.admission.leases)
        self.assertEqual(callback.await_count, 1)
        await self.service.event(
            {
                "kind": "run.completed",
                "data": {"session_id": "lost", "run": {"id": "late", "status": "completed", "items": []}},
            }
        )
        self.assertEqual(self.service.records.get(ident)["state"], "unknown")

    async def test_registered_media_uses_the_same_input_contract_without_copying_registry(self):
        import io
        from PIL import Image
        from assistant_runtime.api.plugins import CallIdentity, current_call

        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
        media = await self.files.media.add_image(buffer.getvalue(), "capture.png")
        result = await self.files.inputs.request(FileRequest("get", {"id": media["id"]}))
        self.assertEqual(result["storage"], "media")
        self.files.inputs.grant([media], "task-work")
        token = current_call.set(CallIdentity("request", "task-work"))
        try:
            captured = self.files.inputs.capture([media["id"]])
        finally:
            current_call.reset(token)
        images, paths = self.files.inputs.materialize(captured, self.files.area("executions/media"))
        self.assertEqual(images[0]["mimeType"], "image/jpeg")
        self.assertTrue(base64.b64decode(images[0]["data"]))
        self.assertEqual(len(paths), 1)

    async def test_unconfirmed_cancellation_quarantines_capacity_until_session_closed(self):
        admission = self.service.admission
        self.client.request = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            await admission.start({"session_id": "uncertain"}, AsyncMock(side_effect=TimeoutError()))
        self.assertEqual(len(admission.leases), 1)
        invoked = AsyncMock(return_value={"run": {"id": "new"}})
        waiting = asyncio.create_task(admission.start({"session_id": "next"}, invoked))
        await asyncio.sleep(0.01)
        invoked.assert_not_awaited()
        admission.closed("uncertain")
        await asyncio.wait_for(waiting, 1)
        invoked.assert_awaited_once()
        self.assertEqual(len(admission.leases), 1)
