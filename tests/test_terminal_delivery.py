"""Scoped terminal notifications and native-only entrance forwarding."""

import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from assistant_runtime.api.terminal import TerminalNotifications
from assistant_runtime.core.notifications.repository import Notifications
from assistant_runtime.core.notifications.settings import NotificationSettings
from assistant_runtime.core.notifications.terminals import TerminalDelivery
from assistant_runtime.core.configuration.entrances import EntranceGateway
from assistant_runtime.plugins.ai_passport.connection import Connection


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.repo = Notifications(self.db, NotificationSettings.parse({}))
        self.delivery = TerminalDelivery(
            SimpleNamespace(
                repo=self.repo, host=SimpleNamespace(config=SimpleNamespace(quiet_hours={}), ai_usage=None)
            )
        )

    async def asyncTearDown(self):
        self.db.close()

    def notice(self, key, session="pp", source="task:task1"):
        return self.repo.publish(
            source,
            key,
            {
                "title": "完成",
                "body": "结果内容",
                "kind": "result",
                "origin": {"conversation_id": session, "task_id": "task1", "run_id": "run1"},
            },
            [],
        )

    async def call(self, action, **args):
        return await self.delivery.request(TerminalNotifications(action, "pp", "device", args))

    async def test_only_own_session_and_real_task_source_and_stable_paging(self):
        one = self.notice("1")
        two = self.notice("2")
        self.notice("3", "web")
        self.notice("4", source="untrusted")
        self.db.execute("UPDATE notifications SET updated=100")
        self.db.commit()
        page = await self.call("list", limit=1)
        next_page = await self.call("list", limit=1, before=page["before"])
        self.assertEqual({page["items"][0]["id"], next_page["items"][0]["id"]}, {one["id"], two["id"]})
        final = await self.call("list", limit=1, before=next_page["before"])
        self.assertEqual(final["items"], [])
        with self.assertRaises(ValueError):
            await self.call("get", id=self.notice("outside", "web")["id"], revision=1)

    async def test_excerpt_uses_owned_full_result_reference(self):
        item = self.notice("long")
        payload = item["payload"]
        payload.update(result_ref={"id": "owned", "version": "v1"}, presentation={"body": "excerpt"})
        self.db.execute("UPDATE notifications SET payload=? WHERE id=?", (json.dumps(payload), item["id"]))
        self.db.commit()
        invoke = AsyncMock(return_value={"data": "完整分页", "eof": False, "next_offset": 104})
        self.delivery.plugin.host.runtime = SimpleNamespace(invoke=invoke)
        page = await self.call("get", id=item["id"], revision=1, offset=100, limit=4)
        self.assertEqual(page["text"], "完整分页")
        self.assertEqual(page["next_offset"], 104)
        self.assertEqual(invoke.call_args.args[0].arguments["id"], "owned")

    async def test_received_played_read_are_independent_and_version_bound(self):
        item = self.notice("item")
        args = {"id": item["id"], "revision": 1}
        await self.call("received", **args)
        page = await self.call("list")
        self.assertTrue(page["items"][0]["unread"])
        self.assertEqual(page["items"][0]["terminal_receipt"], {"received": True, "played": False})
        await self.call("played", **args)
        self.assertTrue(self.repo.get(item["id"])["unread"])
        await self.call("read", **args)
        self.assertFalse(self.repo.get(item["id"])["unread"])
        with self.assertRaises(ValueError):
            await self.call("played", id=item["id"], revision=2)
        data = await self.call("get", **args, offset=2, limit=2)
        self.assertEqual(data["text"], "内容")

    async def test_passport_page_preserves_notification_identity_through_real_delivery(self):
        item = self.notice("passport-page")
        text = "这是分页的后台任务报告。" * 20
        payload = item["payload"] | {"body": text}
        self.db.execute("UPDATE notifications SET payload=? WHERE id=?", (json.dumps(payload), item["id"]))
        self.db.commit()
        ws = SimpleNamespace(send_json=AsyncMock())
        owner = SimpleNamespace(context=SimpleNamespace(invoke=self.delivery.request))
        connection = Connection(owner, "device", "pp", ws)
        pages, offset = [], 0
        while True:
            await connection.command(
                {"type": "notification.page", "id": item["id"], "revision": 1, "offset": offset}
            )
            page = ws.send_json.call_args.args[0]
            self.assertEqual(
                (page["type"], page["id"], page["revision"], page["offset"]),
                ("notification.page", item["id"], 1, offset),
            )
            self.assertNotIn("notification_id", page)
            self.assertNotIn("work_id", page)
            self.assertNotIn("result_ref", page)
            pages.append(page["text"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(pages), text)
        self.assertTrue(self.repo.get(item["id"])["unread"])
        with self.assertRaises(ValueError):
            await connection.command(
                {"type": "notification.page", "id": item["id"], "revision": 2, "offset": 0}
            )
        with self.assertRaises(ValueError):
            await connection.command(
                {
                    "type": "notification.page",
                    "id": self.notice("other", "web")["id"],
                    "revision": 1,
                    "offset": 0,
                }
            )


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_browser_credentials_forwarded_and_stream_cleanup(self):
        seen = []

        async def pair(request):
            seen.append(dict(request.headers))
            return web.json_response({"paired": True})

        async def stream(request):
            seen.append(dict(request.headers))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for m in ws:
                if m.type.value == 1:
                    await ws.send_str(m.data)
            return ws

        backend = web.Application()
        backend.router.add_post("/pair", pair)
        backend.router.add_get("/stream", stream)
        server = TestServer(backend)
        await server.start_server()
        gateway = EntranceGateway(
            SimpleNamespace(
                request=AsyncMock(
                    return_value={"host": "127.0.0.1", "port": server.port, "key": "private-forwarding-key"}
                )
            )
        )
        app = web.Application()
        app.router.add_route("*", "/{owner}/{route}", gateway.forward)
        front = TestServer(app)
        await front.start_server()
        try:
            async with ClientSession() as client:
                async with client.post(
                    front.make_url("/ai_passport/pair"),
                    json={},
                    headers={"Origin": "https://attacker.invalid"},
                ) as response:
                    self.assertEqual(response.status, 403)
                self.assertFalse(seen)
                async with client.post(
                    front.make_url("/ai_passport/pair"),
                    json={},
                    headers={"Authorization": "Bearer device", "X-PA-Frontend-Key": "forged"},
                ) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(seen[0]["X-PA-Frontend-Key"], "private-forwarding-key")
                async with client.ws_connect(
                    front.make_url("/ai_passport/stream"), headers={"Authorization": "Bearer device"}
                ) as ws:
                    await ws.send_json({"ping": 1})
                    self.assertEqual(json.loads((await ws.receive()).data), {"ping": 1})
        finally:
            await front.close()
            await gateway.close()
            await server.close()
        self.assertFalse(gateway.sockets)


class TaskTerminalOwnershipTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_task_completion_routes_only_to_its_terminal_session(self):
        from test_task_feedback import TaskFeedbackTest
        from assistant_runtime.api.conversation import current_conversation

        fixture = TaskFeedbackTest("test_default_delegation_and_query_leave_background_responsible")
        await fixture.asyncSetUp()
        try:
            delivery = TerminalDelivery(fixture.host.notifications)
            token = current_conversation.set("paired-pp-session")
            try:
                run = await fixture.new_run()
            finally:
                current_conversation.reset(token)
            # Recreate the feedback coordinator, as after a restart: origin must stay in the task record.
            from assistant_runtime.core.tasks.feedback import TaskFeedback

            fixture.plugin.feedback = TaskFeedback(fixture.plugin)
            fixture.complete(run)
            await fixture.settled(run)
            own = await delivery.request(TerminalNotifications("list", "paired-pp-session", "pp", {}))
            other = await delivery.request(TerminalNotifications("list", "another-pp", "other", {}))
            self.assertEqual(len(own["items"]), 1)
            self.assertEqual(other["items"], [])
            item = own["items"][0]
            self.assertEqual(item["payload"]["origin"]["run_id"], run["id"])
            content = await delivery.request(
                TerminalNotifications(
                    "get", "paired-pp-session", "pp", {"id": item["id"], "revision": item["revision"]}
                )
            )
            self.assertIn("Verified task result", content["text"])
            # Repeat completion processing: still one canonical notice, not a second delivery record.
            await fixture.settled(run)
            again = await delivery.request(TerminalNotifications("list", "paired-pp-session", "pp", {}))
            self.assertEqual([x["id"] for x in again["items"]], [item["id"]])
        finally:
            await fixture.asyncTearDown()
