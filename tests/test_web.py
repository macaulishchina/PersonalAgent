"""Real local HTTP access, image validation and resumable browser events."""

import asyncio
from dataclasses import replace
import io
from pathlib import Path
import socket
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar, FormData, web
from PIL import Image
from assistant_runtime.app.bundled import context_for
from assistant_runtime.plugins.web.plugin import WebPlugin
from assistant_runtime.plugins.web.auth import Access
from assistant_runtime.plugins.web.settings import WebSettings
from test_conversation import fixture, Owned


def with_timeout(handler):
    async def invoke(request, **kwargs):
        return await handler(request)

    return invoke


class WebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = fixture(Path(self.temp.name))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.host.config = replace(
            self.host.config,
            plugins=["tasks", "notifications"],
            web={"enable": True, "host": "127.0.0.1", "port": port},
        )
        self.context = Owned()
        await self.host.conversation.start(self.context)
        self.plugin = WebPlugin(context_for(self.host, "web"))
        await self.plugin.start(self.context)
        from types import SimpleNamespace
        from assistant_runtime.core.configuration.chat_gateway import ChatGateway

        async def endpoint(payload):
            return self.host.http_frontends["web"].endpoint

        self.gateway = ChatGateway(
            SimpleNamespace(process=SimpleNamespace(returncode=None), request=endpoint)
        )

        @web.middleware
        async def routing(request, handler):
            request["gateway_scheme"] = "https" if getattr(self, "https_gateway", False) else "http"
            if getattr(self, "https_gateway", False):
                request["gateway_origin"] = "https://public.example"
                request["gateway_trusted_origin"] = True
            return await handler(request)

        app = web.Application(middlewares=[routing])
        app.router.add_route("*", "/chat/{path:.*}", self.gateway.forward)
        self.gateway_runner = web.AppRunner(app, shutdown_timeout=1)
        await self.gateway_runner.setup()
        site = web.TCPSite(self.gateway_runner, "127.0.0.1", 0)
        await site.start()
        self.origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.base = self.origin + "/chat"
        self.client = ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": self.origin})
        self.token = (self.host.config.state_dir / "web-access-token").read_text().strip()

    async def asyncTearDown(self):
        await self.client.close()
        await self.gateway.close()
        await self.gateway_runner.cleanup()
        await self.context.close()
        self.host.store.db.close()
        self.temp.cleanup()

    async def login(self):
        r = await self.client.post(self.base + "/api/login", json={"token": self.token})
        self.assertEqual(r.status, 200)

    async def test_result_reader_rejects_invalid_or_unbounded_ranges(self):
        await self.login()
        for query in ("offset=bad", "offset=-1", "length=0", "length=16001", "length=bad"):
            response = await self.client.get(self.base + "/api/results/example?" + query)
            self.assertEqual(response.status, 400, query)

    async def test_applied_model_presentation_and_avatars_use_chat_auth(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from assistant_runtime.core.models.plugin import ModelsPlugin
        from assistant_runtime.core.models.settings import ModelSettings
        from assistant_runtime.core.models import presentation
        from test_models import settings

        owner = object.__new__(ModelsPlugin)
        owner.host = SimpleNamespace(config=replace(self.host.config, main_model="test-model"))
        raw = settings()
        raw["providers"]["test"]["presentation"] = {
            "display_name": "Public Team",
            "avatar": "builtin:deepseek",
        }
        raw["entries"]["test-model"]["presentation"] = {
            "display_name": "Public Model",
            "description": "Helpful model",
        }
        owner.settings = ModelSettings.parse(raw)

        async def invoke(request, **kwargs):
            return await owner.handle(request)

        self.host.runtime.invoke = AsyncMock(side_effect=invoke)
        for path in ("models", "model-avatar/builtin/deepseek"):
            response = await self.client.get(self.base + "/api/" + path)
            self.assertEqual(response.status, 401)
        self.host.runtime.invoke.assert_not_awaited()
        await self.login()
        response = await self.client.get(self.base + "/api/models")
        self.assertEqual(response.status, 200)
        catalog = await response.json()
        self.assertEqual(catalog["default"]["route"]["model"], "test-model")
        row = catalog["models"][0]
        self.assertEqual(row["display_name"], "Public Model")
        self.assertEqual(row["displayName"], "Public Model")
        self.assertEqual(row["provider_display_name"], "Public Team")
        self.assertEqual(row["avatar"], {"kind": "builtin", "name": "deepseek"})
        self.assertNotIn("key_env", str(catalog))
        response = await self.client.get(self.base + "/api/model-avatar/builtin/deepseek")
        self.assertEqual(response.status, 200)
        self.assertIn("image/svg+xml", response.headers["Content-Type"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        cached = await self.client.get(
            self.base + "/api/model-avatar/builtin/deepseek",
            headers={"If-None-Match": response.headers["ETag"]},
        )
        self.assertEqual(cached.status, 304)

        stream = io.BytesIO()
        Image.new("RGB", (10, 10), "green").save(stream, format="PNG")
        image = presentation.normalize(stream.getvalue())
        ident = presentation.digest(image)
        folder = self.host.config.root / "config/model-avatars"
        folder.mkdir(parents=True)
        (folder / (ident + ".png")).write_bytes(image)
        raw["entries"]["test-model"]["presentation"].update(
            display_name="Updated Model", avatar="asset:" + ident
        )
        owner.settings = ModelSettings.parse(raw)
        reloaded = await (await self.client.get(self.base + "/api/models")).json()
        self.assertEqual(reloaded["models"][0]["display_name"], "Updated Model")
        response = await self.client.get(self.base + f"/api/model-avatar/{ident}?version={ident}")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), image)
        response = await self.client.get(self.base + f"/api/model-avatar/{ident}?version=stale")
        self.assertEqual(response.status, 400)
        del raw["entries"]["test-model"]["presentation"]["avatar"]
        owner.settings = ModelSettings.parse(raw)
        response = await self.client.get(self.base + f"/api/model-avatar/{ident}?version={ident}")
        self.assertEqual(response.status, 400)

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

    async def test_machine_hostname_keeps_origin_and_auth_guards(self):
        hostname = socket.gethostname().lower()
        port = self.origin.rsplit(":", 1)[1]
        for name in (hostname, hostname.upper()):
            authority = f"{name}:{port}"
            headers = {"Host": authority, "Origin": f"http://{authority}"}
            response = await self.client.get(self.base + "/", headers=headers)
            self.assertEqual(response.status, 200)
            response = await self.client.get(self.base + "/api/messages", headers=headers)
            self.assertEqual(response.status, 401)
            response = await self.client.post(
                self.base + "/api/login",
                json={"token": self.token},
                headers={**headers, "Origin": "http://evil.test"},
            )
            self.assertEqual(response.status, 403)
        response = await self.client.get(
            self.base + "/",
            headers={"Host": hostname + ".evil.test"},
        )
        self.assertEqual(response.status, 400)

    async def test_explicit_input_is_bound_to_web_and_cannot_switch_session(self):
        from unittest.mock import AsyncMock

        await self.login()
        self.host.runtime.invoke = AsyncMock(return_value={"state": "answered"})
        response = await self.client.post(
            self.base + "/api/conversation",
            json={
                "action": "answer",
                "request_id": "request",
                "question_id": "question",
                "text": "answer",
            },
        )
        self.assertEqual(response.status, 200)
        request = self.host.runtime.invoke.await_args.args[0]
        self.assertEqual(request.arguments["session_id"], self.host.conversation.session)
        self.assertEqual(request.arguments["question_id"], "question")
        response = await self.client.post(
            self.base + "/api/conversation",
            json={
                "action": "cancel",
                "session_id": "another",
                "request_id": "request",
            },
        )
        self.assertEqual(response.status, 400)

    async def test_queue_controls_are_authenticated_and_bound_to_the_web_session(self):
        from unittest.mock import AsyncMock

        calls = AsyncMock(return_value={"requests": []})
        self.host.runtime.invoke = calls
        operations = [
            ("edit_queued", {"request_id": "queued", "revision": "seen", "text": "edited"}),
            ("remove_queued", {"request_id": "queued", "revision": "seen"}),
            ("reorder_queue", {"request_ids": ["b", "a"], "expected_order": ["a", "b"]}),
        ]
        response = await self.client.post(
            self.base + "/api/conversation", json={"action": "remove_queued", **operations[1][1]}
        )
        self.assertEqual(response.status, 401)
        calls.assert_not_awaited()
        await self.login()
        for action, arguments in operations:
            with self.subTest(action=action):
                response = await self.client.post(
                    self.base + "/api/conversation", json={"action": action, **arguments}
                )
                self.assertEqual(response.status, 200)
                received = calls.await_args.args[0]
                self.assertEqual(received.action, action)
                self.assertEqual(
                    received.arguments, {**arguments, "session_id": self.host.conversation.session}
                )
                count = calls.await_count
                response = await self.client.post(
                    self.base + "/api/conversation",
                    json={"action": action, **arguments, "session_id": "another-conversation"},
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(calls.await_count, count)

    async def test_task_pages_use_owner_filter_cursor_and_details(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from assistant_runtime.core.tasks.repository import TaskRepository
        from assistant_runtime.core.tasks.plugin import TasksPlugin
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
        owner.host = self.host
        owner.repo = repo
        from assistant_runtime.core.tasks.business import BusinessResults

        owner.business = BusinessResults(owner)
        from assistant_runtime.core.tasks.feedback import TaskFeedback

        owner.feedback = TaskFeedback(owner)
        self.host.runtime.plugins = {"tasks": SimpleNamespace()}
        self.host.runtime.invoke = AsyncMock(side_effect=with_timeout(owner.request))
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
        from assistant_runtime.core.notifications.plugin import NotificationsPlugin
        from assistant_runtime.api.contracts import NotificationRequest
        from unittest.mock import AsyncMock

        notifications = NotificationsPlugin(self.host)
        self.host.runtime.invoke = AsyncMock(side_effect=with_timeout(notifications.request))
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
        from assistant_runtime.core.notifications.plugin import NotificationsPlugin
        from assistant_runtime.api.contracts import NotificationRequest
        from unittest.mock import AsyncMock
        import uuid

        notifications = NotificationsPlugin(self.host)
        self.host.runtime.invoke = AsyncMock(side_effect=with_timeout(notifications.request))
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

    async def test_six_character_password_survives_reload_and_rotation(self):
        path = self.host.config.state_dir / "web-access-token"
        self.token = "123456"
        path.write_text(self.token + "\n")
        await self.plugin.close()
        self.plugin = WebPlugin(context_for(self.host, "web"))
        await self.plugin.start(self.context)
        await self.login()
        old_session = self.plugin.auth.login(self.token, "test")
        path.write_text("abcdef\n")
        self.plugin.auth = Access(path, self.host.store.db)
        self.assertFalse(self.plugin.auth.valid(old_session))
        with self.assertRaises(PermissionError):
            self.plugin.auth.login(self.token, "old")
        self.assertTrue(self.plugin.auth.valid(self.plugin.auth.login("abcdef", "new")))
        for token in ("12345", "五个中文字"):
            path.write_text(token + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                Access(path, self.host.store.db)

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
        r = await self.client.get(self.origin + attachment["url"])
        self.assertEqual(r.status, 200)
        self.assertEqual(r.content_type, "image/jpeg")
        async with ClientSession() as other:
            r = await other.get(self.origin + attachment["url"])
            self.assertEqual(r.status, 401)

    async def test_large_upload_streams_past_the_management_json_body_limit(self):
        import random

        await self.login()
        data = io.BytesIO()
        Image.frombytes("RGB", (1024, 1024), random.Random(0).randbytes(1024 * 1024 * 3)).save(
            data, format="PNG"
        )
        self.assertGreater(len(data.getvalue()), 2 * 1024 * 1024)
        form = FormData()
        form.add_field("image", data.getvalue(), filename="large.png", content_type="image/png")
        response = await self.client.post(self.base + "/api/uploads", data=form)
        self.assertEqual(response.status, 201, await response.text())

    async def test_trusted_https_gateway_preserves_secure_chat_cookie(self):
        self.https_gateway = True
        response = await self.client.post(
            self.base + "/api/login", json={"token": self.token}, headers={"Origin": "https://public.example"}
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies["personal_agent_session"]["secure"])
        self.assertEqual(response.cookies["personal_agent_session"]["path"], "/chat/")

    async def test_report_group_projection_matches_history_and_sse(self):
        import json
        from assistant_runtime.core.notifications.plugin import NotificationsPlugin
        from assistant_runtime.api.contracts import NotificationRequest

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

    async def test_task_trace_is_authenticated_bounded_read_only_projection(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from assistant_runtime.core.tasks.repository import TaskRepository
        from assistant_runtime.core.tasks.trace import TaskTrace
        import json
        import uuid

        repo = TaskRepository(self.host.store.db)
        ident = uuid.uuid4().hex
        spec = {"title": "trace", "prompt": "private task prompt", "trigger": {"kind": "immediate"}}
        self.host.store.db.execute(
            "INSERT INTO job_definitions(id,request_id,fingerprint,state,spec,created,updated) VALUES(?,?,?,'enabled',?,?,?)",
            (ident, str(uuid.uuid4()), "test", json.dumps(spec), 1, 1),
        )
        run = repo.enqueue(
            repo.definition(ident), "one", "running", {"execution_session_id": "private-thread"}
        )
        other = repo.enqueue(repo.definition(ident), "two", "running")
        trace = TaskTrace(repo)
        for n in range(3):
            trace.append(
                run["id"],
                {
                    "call_id": str(n),
                    "kind": "mcpToolCall",
                    "phase": "started",
                    "tool": "search",
                    "status": "running",
                },
            )
        trace.append(other["id"], {"call_id": "unrelated", "phase": "started"})
        self.host.runtime.plugins = {"tasks": SimpleNamespace()}
        self.host.runtime.invoke = AsyncMock(
            side_effect=lambda request, **kwargs: trace.read(request.arguments)
        )
        path = self.base + f"/api/tasks/{run['id']}/trace"
        response = await self.client.get(path)
        self.assertEqual(response.status, 401)
        self.host.runtime.invoke.assert_not_awaited()
        await self.login()
        response = await self.client.get(path + "?limit=2")
        self.assertEqual(response.status, 200)
        page = await response.json()
        self.assertEqual(len(page["items"]), 2)
        self.assertNotIn("private", str(page))
        self.assertEqual(
            set(page),
            {"run_id", "task_id", "state", "items", "next", "truncated", "outcome_scope", "lifecycle"},
        )
        self.assertEqual(self.host.runtime.invoke.call_args.args[0].action, "trace")
        response = await self.client.get(path + f"?after={page['next']}&limit=2")
        last = await response.json()
        self.assertEqual([x["call_id"] for x in last["items"]], ["2"])
        self.assertIsNone(last["next"])
        for query in ("?limit=101", "?after=-1", "?limit=oops", "?action=cancel_run"):
            response = await self.client.get(path + query)
            self.assertEqual(response.status, 400)
        response = await self.client.get(self.base + "/api/tasks/missing/trace")
        self.assertEqual(response.status, 400)
        self.assertEqual(repo.run(run["id"])["state"], "running")


class WebSettingsTest(unittest.TestCase):
    def test_invalid_listener_and_port_rejected(self):
        for value in (
            {"port": -1},
            {"host": "0.0.0.0", "enable": True},
            {"port": True},
            {"host": "external.example"},
            {"enable": "false"},
            {"extra": 1},
        ):
            with self.assertRaises(ValueError):
                WebSettings.parse(value)
