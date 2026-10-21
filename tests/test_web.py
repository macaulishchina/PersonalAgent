"""Real local HTTP access, image validation and resumable browser events."""

import asyncio
from dataclasses import replace
import io
import hashlib
from pathlib import Path
import socket
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar, FormData
from PIL import Image
from assistant_runtime.plugins.web.plugin import WebPlugin
from assistant_runtime.plugins.web.auth import Access
from assistant_runtime.plugins.web.settings import WebSettings
from test_conversation import fixture, Owned


class WebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = fixture(Path(self.temp.name))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.host.config = replace(self.host.config, web={"enable": True, "host": "127.0.0.1", "port": port})
        self.context = Owned()
        await self.host.conversation.start(self.context)
        self.plugin = WebPlugin(self.host)
        await self.plugin.start(self.context)
        self.base = f"http://127.0.0.1:{port}"
        self.client = ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": self.base})
        self.token = (self.host.config.state_dir / "web-access-token").read_text().strip()

    async def asyncTearDown(self):
        await self.client.close()
        await self.context.close()
        self.host.store.db.close()
        self.temp.cleanup()

    async def login(self):
        r = await self.client.post(self.base + "/api/login", json={"token": self.token})
        self.assertEqual(r.status, 200)

    async def test_auth_origin_and_restart_cookie(self):
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 401)
        r = await self.client.post(
            self.base + "/api/login", json={"token": self.token}, headers={"Origin": "http://evil.test"}
        )
        self.assertEqual(r.status, 403)
        await self.login()
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 200)
        self.plugin.auth = Access(self.host.config.state_dir / "web-access-token", self.host.store.db)
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 200)
        r = await self.client.get(self.base + "/api/messages", headers={"Host": "evil.test"})
        self.assertEqual(r.status, 400)
        await self.client.post(self.base + "/api/logout", json={})
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 401)

    async def test_task_pages_use_owner_filter_cursor_and_details(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from assistant_runtime.plugins.tasks.repository import TaskRepository
        from assistant_runtime.plugins.tasks.plugin import TasksPlugin
        import json
        import uuid

        repo = TaskRepository(self.host.store.db)
        for number in range(45):
            ident = f"{number:032x}"
            spec = {"title": f"任务 {number}", "prompt": "分页", "trigger": {"kind": "immediate"}}
            self.host.store.db.execute(
                "INSERT INTO job_definitions(id,request_id,fingerprint,state,spec,created,updated) VALUES(?,?,?,'archived',?,?,?)",
                (ident, str(uuid.uuid4()), "test", json.dumps(spec), 100, 100),
            )
        for number in range(45):
            repo.enqueue(repo.definition(f"{44:032x}"), str(number), "missed")
        self.host.store.db.commit()
        owner = object.__new__(TasksPlugin)
        owner.repo = repo
        from assistant_runtime.plugins.tasks.feedback import TaskFeedback

        owner.feedback = TaskFeedback(owner)
        self.host.runtime.plugins = {"tasks": SimpleNamespace()}
        self.host.runtime.invoke = AsyncMock(side_effect=owner.request)
        response = await self.client.get(
            self.base + "/api/tasks?view=temporary&include_archived=true&limit=20"
        )
        self.assertEqual(response.status, 401)
        await self.login()
        response = await self.client.get(
            self.base + "/api/tasks?view=temporary&include_archived=true&limit=20"
        )
        first = await response.json()
        self.assertEqual(len(first["tasks"]), 20)
        response = await self.client.get(self.base + "/api/tasks", params={"view": "temporary"})
        self.assertEqual((await response.json())["tasks"], [])
        response = await self.client.get(self.base + "/api/tasks", params={"view": "planned"})
        self.assertEqual((await response.json())["tasks"], [])
        response = await self.client.get(
            self.base + "/api/tasks", params={"view": "planned", "active_only": "true"}
        )
        self.assertEqual((await response.json())["tasks"], [])
        self.assertIs(self.host.runtime.invoke.await_args.args[0].arguments["active_only"], True)
        response = await self.client.get(
            self.base + "/api/tasks",
            params={"view": "temporary", "include_archived": "true", "before": first["before"], "limit": 20},
        )
        second = await response.json()
        self.assertEqual(len(second["tasks"]), 20)
        self.assertFalse({d["id"] for d in first["tasks"]} & {d["id"] for d in second["tasks"]})
        response = await self.client.get(
            self.base + "/api/tasks",
            params={"view": "temporary", "include_archived": "true", "query": "任务 0"},
        )
        self.assertEqual([d["id"] for d in (await response.json())["tasks"]], [f"{0:032x}"])
        response = await self.client.get(
            self.base + "/api/tasks", params={"task_id": f"{44:032x}", "limit": 20}
        )
        detail = await response.json()
        self.assertEqual(len(detail["runs"]), 20)
        self.assertIsNotNone(detail["before"])
        for params in (
            {"limit": "500"},
            {"limit": "invalid"},
            {"before": "invalid"},
            {"known": "not json"},
            {"view": "planned", "active_only": "yes"},
            {"view": "temporary", "include_archived": "1"},
            {"view": "temporary", "before": first["before"]},
        ):
            response = await self.client.get(self.base + "/api/tasks", params=params)
            self.assertEqual(response.status, 400)

    async def test_notification_group_routes_preserve_snapshot_and_reject_internal_links(self):
        from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
        from assistant_runtime.kernel.contracts import NotificationRequest
        from unittest.mock import AsyncMock

        notifications = NotificationsPlugin(self.host)
        self.host.runtime.invoke = AsyncMock(side_effect=notifications.request)
        for i in range(25):
            await notifications.request(
                NotificationRequest(
                    "publish",
                    {
                        "event_key": str(i),
                        "title": "事项进展",
                        "body": "证据",
                        "group_key": "operation:one",
                    },
                )
            )
        await self.login()
        response = await self.client.get(self.base + "/api/notifications?grouped=true&unread=true")
        self.assertEqual(response.status, 200)
        listing = await response.json()
        self.assertEqual(len(listing["items"]), 1)
        group = listing["items"][0]
        self.assertEqual(group["total"], 25)
        response = await self.client.post(
            self.base + "/api/notifications",
            json={
                "action": "group",
                "arguments": {"group_id": group["id"], "snapshot": group["read_before"]},
            },
        )
        members = await response.json()
        self.assertEqual(len(members["items"]), 20)
        self.assertTrue(members["before"])
        response = await self.client.post(
            self.base + "/api/notifications", json={"action": "link", "arguments": {}}
        )
        self.assertEqual(response.status, 400)
        await notifications.request(
            NotificationRequest(
                "publish",
                {"event_key": "late", "title": "新进展", "body": "新证据", "group_key": "operation:one"},
            )
        )
        response = await self.client.post(
            self.base + "/api/notifications",
            json={
                "action": "read_group",
                "arguments": {"group_id": group["id"], "before": group["read_before"]},
            },
        )
        self.assertEqual((await response.json())["marked_read"], 25)
        self.assertEqual(notifications.repo.unread_count(), 1)

    async def test_notification_inbox_auth_read_and_follow_up_context(self):
        from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
        from assistant_runtime.kernel.contracts import NotificationRequest
        from unittest.mock import AsyncMock
        import uuid

        notifications = NotificationsPlugin(self.host)
        self.host.runtime.invoke = AsyncMock(side_effect=notifications.request)
        response = await self.client.get(self.base + "/api/notifications")
        self.assertEqual(response.status, 401)
        await self.login()
        notice = await notifications.request(
            NotificationRequest(
                "publish",
                {
                    "event_key": "announcement",
                    "title": "新预告",
                    "body": "确认窗口为明天。",
                },
            )
        )
        response = await self.client.get(self.base + "/api/notifications")
        listing = await response.json()
        self.assertEqual(listing["unread"], 1)
        self.assertNotIn("archived", listing["items"][0])
        response = await self.client.post(
            self.base + "/api/notifications",
            json={"action": "archive", "arguments": {"id": notice["id"], "revision": 1}},
        )
        self.assertEqual(response.status, 400)
        self.assertTrue(notifications.repo.get(notice["id"])["unread"])
        response = await self.client.post(
            self.base + "/api/notifications",
            json={"action": "read", "arguments": {"id": notice["id"], "revision": 1}},
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            self.base + "/api/notifications",
            json={"action": "read", "arguments": {"id": notice["id"], "revision": 1}},
        )
        self.assertEqual(response.status, 200)
        response = await self.client.post(
            self.base + "/api/notifications",
            json={"action": "read_all", "arguments": {"before": listing["read_before"]}},
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual(response.status, 403)
        response = await self.client.post(
            self.base + "/api/notifications",
            json={"action": "read_all", "arguments": {"before": listing["read_before"]}},
        )
        self.assertEqual(response.status, 200)
        response = await self.client.post(
            self.base + "/api/messages",
            json={
                "request_id": str(uuid.uuid4()),
                "text": "帮我解释一下",
                "attachments": [],
                "notification_id": notice["id"],
            },
        )
        self.assertEqual(response.status, 202)
        for _ in range(20):
            if self.host.backend.submit.await_count:
                break
            await asyncio.sleep(0.02)
        text = self.host.backend.submit.await_args.args[0]
        self.assertIn("确认窗口为明天", text)
        self.assertIn("帮我解释一下", text)
        self.assertEqual(self.host.conversation.session, "session")

    async def test_invalid_json_shapes_are_client_errors(self):
        r = await self.client.post(self.base + "/api/login", json=[])
        self.assertEqual(r.status, 400)
        await self.login()
        for route in ("messages", "control"):
            r = await self.client.post(self.base + "/api/" + route, json=[])
            self.assertEqual(r.status, 400)

    async def test_explicitly_approved_existing_password_survives_reload_without_general_bypass(self):
        path = self.host.config.state_dir / "web-access-token"
        self.token = "fixture8"
        path.write_text(self.token + "\n")
        approval = hashlib.sha256(self.token.encode()).hexdigest()
        await self.plugin.close()
        self.host.config = replace(
            self.host.config, web={**self.host.config.web, "approved_short_password_sha256": approval}
        )
        self.plugin = WebPlugin(self.host)
        await self.plugin.start(self.context)
        r = await self.client.post(self.base + "/api/login", json={"token": "wrong123"})
        self.assertEqual(r.status, 401)
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 401)
        await self.login()
        self.plugin.auth = Access(path, self.host.store.db, approved_short_password_sha256=approval)
        r = await self.client.get(self.base + "/api/messages")
        self.assertEqual(r.status, 200)
        self.assertEqual(path.read_text(), self.token + "\n")
        with self.assertRaisesRegex(ValueError, "at least 24"):
            Access(path, self.host.store.db)
        path.write_text("other123\n")
        with self.assertRaisesRegex(ValueError, "at least 24"):
            Access(path, self.host.store.db, approved_short_password_sha256=approval)

    async def test_wrong_password_attempts_remain_rate_limited(self):
        for _ in range(10):
            r = await self.client.post(self.base + "/api/login", json={"token": "wrong123"})
            self.assertEqual(r.status, 401)
        r = await self.client.post(self.base + "/api/login", json={"token": self.token})
        self.assertEqual(r.status, 400)
        self.assertIn("尝试过于频繁", (await r.json())["error"])

    async def test_upload_is_validated_and_private(self):
        await self.login()
        form = FormData()
        form.add_field("image", b'<svg onload="alert(1)"/>', filename="bad.png", content_type="image/png")
        r = await self.client.post(self.base + "/api/uploads", data=form)
        self.assertEqual(r.status, 400)
        data = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(data, format="PNG")
        form = FormData()
        form.add_field("image", data.getvalue(), filename="blue.png", content_type="image/png")
        r = await self.client.post(self.base + "/api/uploads", data=form)
        self.assertEqual(r.status, 201)
        attachment = await r.json()
        r = await self.client.get(self.base + attachment["url"])
        self.assertEqual(r.status, 200)
        self.assertEqual(r.content_type, "image/jpeg")
        async with ClientSession() as other:
            r = await other.get(self.base + attachment["url"])
            self.assertEqual(r.status, 401)

    async def test_report_group_projection_matches_history_and_sse(self):
        import json
        from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
        from assistant_runtime.kernel.contracts import NotificationRequest

        notifications = NotificationsPlugin(self.host)
        notice = await notifications.request(
            NotificationRequest(
                "publish",
                {
                    "event_key": "report",
                    "title": "完成",
                    "body": "原始详情",
                    "group_key": "batch:one",
                    "priority": "important",
                },
            )
        )
        cursor = self.host.conversation.store.cursor()
        await notifications.channels["chat"].deliver(notice)
        await self.login()
        response = await self.client.get(self.base + "/api/messages")
        messages = (await response.json())["messages"]
        message = next(m for m in messages if m.get("turn_id") == "notification:" + notice["id"])
        self.assertEqual(message["report_group"]["id"], notice["group_id"])
        self.assertTrue(message["report_group"]["important"])
        response = await self.client.get(self.base + "/api/events?after=" + str(cursor))
        async with asyncio.timeout(3):
            data = await response.content.readuntil(b"\n\n")
        response.close()
        event = json.loads(next(line[6:] for line in data.decode().splitlines() if line.startswith("data: ")))
        self.assertEqual(event["data"]["report_group"], message["report_group"])
        self.assertEqual(event["data"]["text"], message["text"])
        self.assertTrue(notifications.repo.get(notice["id"])["unread"])

    async def test_events_replay_latest_message_and_reset_future_cursor(self):
        await self.login()
        c = self.host.conversation
        s = c.store
        cursor = s.cursor()
        s.put("a", c.session, text="第一版")
        s.put("a", c.session, text="已修正", status="complete")
        r = await self.client.get(self.base + "/api/events?after=" + str(cursor))
        async with asyncio.timeout(3):
            data = await r.content.readuntil(b"\n\n")
        self.assertIn("已修正".encode(), data)
        r.close()
        r = await self.client.get(self.base + "/api/events?after=999999")
        self.assertIn(b"event: reset", await r.content.read())
        r.close()


class WebSettingsTest(unittest.TestCase):
    def test_invalid_listener_and_port_rejected(self):
        for value in (
            {"port": 0},
            {"port": True},
            {"host": "external.example"},
            {"enable": "false"},
            {"extra": 1},
            {"approved_short_password_sha256": "invalid"},
            {"approved_short_password_sha256": True},
        ):
            with self.assertRaises(ValueError):
                WebSettings.parse(value)
