"""Per-round tokens cannot join a later turn while waiting for transport slots."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from aiohttp import web
from assistant_runtime.core.pi.transport import ExecutionTransport


class ControlledTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_stale_waiter_is_rejected_after_round_changes(self):
        worker = SimpleNamespace(
            meta={
                "active_run": "a",
                "budget_id": "a",
                "controlled_context": "ctx",
                "control_token": "round-a",
            },
            compacting=False,
        )
        client = SimpleNamespace(
            environment={"PA_EXECUTION_TOKEN": "private"},
            workers={"session": worker},
            settings=SimpleNamespace(concurrency=1, queue_size=2),
        )
        transport = ExecutionTransport(client)
        request = SimpleNamespace(
            headers={
                "Authorization": "Bearer private",
                "x-assistant-session": "session",
                "x-assistant-control": "round-a",
            }
        )
        self.assertIs(transport.authenticate(request), worker)
        await transport.slots.acquire()
        pending = asyncio.create_task(transport.forward(request))
        await asyncio.sleep(0)
        worker.meta.update(active_run="b", budget_id="b", control_token="round-b")
        transport.slots.release()
        with self.assertRaises(web.HTTPForbidden):
            await pending
        self.assertEqual(transport.pending, 0)
        request.headers["x-assistant-control"] = "round-b"
        self.assertIs(transport.authenticate(request), worker)


class ToolMediaTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_identity_and_controlled_storage_do_not_bypass_admission(self):
        service = SimpleNamespace(
            register_tool_result=AsyncMock(return_value={"state": "ready", "attachments": [], "issues": []})
        )
        worker = SimpleNamespace(
            meta={"active_run": "run", "budget_id": "run", "pending_operations": {"call": {}}},
            compacting=False,
            active_turn="turn",
            execution_session_id="session",
        )
        client = SimpleNamespace(
            environment={"PA_EXECUTION_TOKEN": "private"},
            workers={"session": worker},
            settings=SimpleNamespace(concurrency=1, queue_size=2),
            admission=SimpleNamespace(service=service),
        )
        transport = ExecutionTransport(client)
        request = SimpleNamespace(
            headers={"Authorization": "Bearer private", "x-assistant-session": "session"},
            json=AsyncMock(return_value={"call_id": "forged", "images": [], "payload": {}}),
        )
        with self.assertRaises(web.HTTPConflict):
            await transport.tool_result(request)
        service.register_tool_result.assert_not_awaited()
        request.json.return_value = {"call_id": "call", "images": [], "owner": "forged"}
        with self.assertRaises(web.HTTPBadRequest):
            await transport.tool_result(request)
        service.register_tool_result.assert_not_awaited()
        request.json.return_value = {"call_id": "call", "images": [], "payload": {}}
        await transport.tool_result(request)
        service.register_tool_result.assert_awaited_once_with("session", "turn", "call", {}, [])
        worker.meta.update(controlled_context="ctx", control_token="bound")
        request.headers["x-assistant-control"] = "bound"
        with self.assertRaises(web.HTTPForbidden):
            await transport.tool_result(request)
        self.assertEqual(service.register_tool_result.await_count, 1)
