"""Per-round tokens cannot join a later turn while waiting for transport slots."""

import asyncio
from types import SimpleNamespace
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
