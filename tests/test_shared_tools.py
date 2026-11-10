"""Shared internal MCP transport, checked independently of the Pi client guard."""

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import ClientSession

from assistant_runtime.core.pi.mcp import normalize_services, server_config
from assistant_runtime.core.pi.shared_tools import LoopbackServer, SharedTools
from assistant_runtime.platforms.ipc import serve


class Worker:
    def __init__(self, ident, path, allowed):
        self.execution_session_id = ident
        self.active_turn = "turn-" + ident
        self.turn_aborted = False
        servers = server_config(path, task_run=ident, plugins=("tasks",))
        servers["tasks"]["enabled_tools"] = allowed
        self.meta = {"mcp_servers": servers}


class SharedToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.entered = {name: asyncio.Event() for name in ("a", "b")}
        self.disconnected = {name: asyncio.Event() for name in ("a", "b")}
        self.release = asyncio.Event()
        self.observed = []

        async def control(reader, writer):
            try:
                data = json.loads(await reader.readline())
                ident = data["execution_session"]
                self.observed.append(data)
                self.entered[ident].set()
                read = asyncio.create_task(reader.read(1))
                finish = asyncio.create_task(self.release.wait())
                try:
                    done, _ = await asyncio.wait((read, finish), return_when=asyncio.FIRST_COMPLETED)
                    if read in done:
                        self.disconnected[ident].set()
                    else:
                        writer.write(
                            json.dumps({"ok": True, "result": {"caller": data["caller_run"]}}).encode()
                            + b"\n"
                        )
                        await writer.drain()
                finally:
                    for task in (read, finish):
                        task.cancel()
                    await asyncio.gather(read, finish, return_exceptions=True)
            finally:
                writer.close()
                await writer.wait_closed()

        path = Path(root) / "mcp.sock"
        ipc = await serve(path, control)

        async def close_ipc():
            self.release.set()
            ipc.close()
            await ipc.wait_closed()

        self.addAsyncCleanup(close_ipc)
        self.a = Worker("a", path, ["list_tasks"])
        self.b = Worker("b", path, ["list_tasks", "get_task"])
        self.tools = SharedTools(SimpleNamespace(workers={"a": self.a, "b": self.b}))
        self.addAsyncCleanup(self.tools.close)
        await asyncio.gather(self.tools.prepare(["tasks"]), self.tools.prepare(["tasks"]))
        self.keys = {worker: self.tools.issue(worker) for worker in (self.a, self.b)}
        self.http = await self.enterAsyncContext(ClientSession())

    async def request(self, worker, method, params=None, *, key=None, path="tasks", **headers):
        async with self.http.post(
            self.tools.url + "/" + path,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + (self.keys[worker] if key is None else key),
                **headers,
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})},
        ) as response:
            return response.status, await response.json() if response.status == 200 else None

    async def call(self, worker, name="list_tasks", **kwargs):
        return await self.request(worker, "tools/call", {"name": name, "arguments": {}}, **kwargs)

    async def test_scope_is_enforced_at_discovery_and_call(self):
        self.assertEqual(set(self.tools.apps), {"tasks"})
        self.assertEqual(len(self.tools.lifespans), 1)
        self.assertNotEqual(self.keys[self.a], self.keys[self.b])
        status, body = await self.request(self.a, "tools/list")
        self.assertEqual(status, 200)
        self.assertEqual([tool["name"] for tool in body["result"]["tools"]], ["list_tasks"])
        _, body = await self.request(self.b, "tools/list")
        self.assertEqual({tool["name"] for tool in body["result"]["tools"]}, {"list_tasks", "get_task"})
        _, denied = await self.call(self.a, "get_task")
        self.assertTrue(denied["result"]["isError"], denied)
        outcome = denied["result"]["structuredContent"]["tool_outcome"]
        self.assertEqual((outcome["code"], outcome["dispatch"]), ("tool_permission_denied", "not_dispatched"))
        self.assertEqual((await self.call(self.a, key="forged"))[0], 401)
        self.assertEqual((await self.request(self.a, "tools/list", path="notifications"))[0], 404)
        self.assertFalse(self.observed)

    async def test_cancel_one_call_without_rebinding_or_interrupting_other_worker(self):
        a = asyncio.create_task(self.call(self.a, **{"x-assistant-session": "b"}))
        b = asyncio.create_task(self.call(self.b, **{"x-assistant-session": "a"}))
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*(event.wait() for event in self.entered.values()))
                self.a.turn_aborted = True
                self.tools.cancel(self.a)
                self.assertEqual((await a)[0], 503)
                await self.disconnected["a"].wait()
                self.assertFalse(b.done())
                self.release.set()
                _, result = await b
                self.assertEqual(result["result"]["structuredContent"], {"caller": "b"})
        finally:
            self.release.set()
            await asyncio.gather(a, b, return_exceptions=True)
        self.assertEqual(
            {(x["execution_session"], x["caller_run"], x["execution_turn"]) for x in self.observed},
            {("a", "a", "turn-a"), ("b", "b", "turn-b")},
        )
        self.tools.revoke(self.a)
        self.assertEqual((await self.call(self.a))[0], 401)
        self.assertEqual((await self.request(self.b, "tools/list"))[0], 200)

    async def test_http_disconnect_cancels_the_bound_request_and_does_not_replay(self):
        task = asyncio.create_task(self.call(self.a))
        try:
            async with asyncio.timeout(5):
                await self.entered["a"].wait()
                task.cancel()
                await self.disconnected["a"].wait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.observed), 1)
        self.a.active_turn = None
        _, result = await self.call(self.a)
        self.assertTrue(result["result"]["isError"])
        self.assertEqual(len(self.observed), 1)

    async def test_one_worker_cannot_consume_another_workers_request_allowance(self):
        self.tools.MAX_REQUESTS_PER_WORKER = 2
        calls = [asyncio.create_task(self.call(self.a)) for _ in range(2)]
        try:
            async with asyncio.timeout(5):
                while len(self.observed) < 2:
                    await asyncio.sleep(0)
                self.assertEqual((await self.call(self.a))[0], 503)
                self.assertEqual((await self.request(self.b, "tools/list"))[0], 200)
                self.release.set()
                self.assertEqual([status for status, _ in await asyncio.gather(*calls)], [200, 200])
        finally:
            self.release.set()
            await asyncio.gather(*calls, return_exceptions=True)
        self.assertEqual((await self.call(self.a))[0], 200)
        self.assertFalse(self.tools.pending_by_worker)

    async def test_restart_revokes_old_credentials_and_startup_failure_cleans_up(self):
        previous = self.keys[self.a]
        await self.tools.close()
        self.assertIsNone(self.tools.url)
        self.assertFalse(self.tools.apps)
        with patch.object(LoopbackServer, "serve", side_effect=OSError("synthetic bind failure")):
            with self.assertRaises(OSError):
                await self.tools.prepare(["tasks"])
        self.assertIsNone(self.tools.listener)
        await self.tools.prepare(["tasks"])
        self.keys[self.a] = self.tools.issue(self.a)
        self.assertEqual((await self.request(self.a, "tools/list", key=previous))[0], 401)
        self.assertEqual((await self.request(self.a, "tools/list"))[0], 200)


class SavedToolsTest(unittest.TestCase):
    def test_old_generated_config_keeps_the_scope_without_a_stdio_command(self):
        old = {
            "tasks": {
                "command": "python",
                "args": [
                    "-m",
                    "assistant_runtime.mcp",
                    "tasks",
                    "--socket",
                    "/example",
                    "--task-run",
                    "one",
                    "--execution-session",
                    "session",
                ],
                "enabled_tools": ["list_tasks"],
            }
        }
        updated = normalize_services(old)
        self.assertEqual(
            updated,
            {
                "tasks": {
                    "local_service": "tasks",
                    "socket": "/example",
                    "task_run": "one",
                    "enabled_tools": ["list_tasks"],
                }
            },
        )
        self.assertEqual(normalize_services(updated), updated)
