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
