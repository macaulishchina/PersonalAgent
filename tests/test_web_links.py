"""Real HTTP checks for scoped links; no PP, provider or production instance."""

import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
import uuid

from aiohttp import ClientSession, CookieJar, FormData, web
from PIL import Image

from assistant_runtime.api.conversation import ConversationRequest
from assistant_runtime.api.files import FileRequest
from assistant_runtime.app.bundled import context_for
from assistant_runtime.core.configuration.chat_gateway import ChatGateway
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.web.plugin import WebPlugin
from assistant_runtime.plugins.web.links import Links, COOKIE
import test_conversation_sessions as session_fixtures
from test_conversation import Owned


class WebLinksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await session_fixtures.SessionsTest.asyncSetUp(self)
        self.host.config = replace(
            self.host.config,
            plugin_settings={
                **self.host.config.plugin_settings,
                "web": {"enable": True, "host": "127.0.0.1", "port": 0},
            },
        )
        self.host.runtime = Runtime(self.store)

        async def invoke(request, timeout=None):
            if isinstance(request, ConversationRequest):
                return await self.registry.request(request)
            return await self.other(request)

        self.other = AsyncMock(return_value={"data": "scoped result", "eof": True})
        self.host.runtime.invoke = invoke
        self.plugin = WebPlugin(context_for(self.host, "web"))
        self.web_context = Owned()
        await self.plugin.start(self.web_context)
        self.gateway = ChatGateway(
            SimpleNamespace(
                process=SimpleNamespace(returncode=None),
                request=AsyncMock(side_effect=lambda _: self.plugin.frontend.endpoint),
            )
        )

        @web.middleware
        async def routing(request, handler):
            request["gateway_scheme"] = "http"
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
        self.a = self.registry.sessions.create("会话 A")["id"]
        self.b = self.registry.sessions.create("会话 B")["id"]
        self.default = self.registry.main.session
        for ident in (self.default, self.a, self.b):
            self.registry.main.store.put(
                "message-" + ident, ident, text="private-" + ident, status="complete"
            )

    async def asyncTearDown(self):
        await self.client.close()
        await self.gateway.close()
        await self.gateway_runner.cleanup()
        await self.web_context.close()
        await session_fixtures.SessionsTest.asyncTearDown(self)

    async def link(self, session=None, permission="chat", client=None):
        result = await self.plugin.scoped.manage(
            "create_link",
            {
                "session_id": session or self.a,
                "permission": permission,
            },
        )
        token = result["path"].split("#")[1]
        response = await (client or self.client).post(
            self.base + "/c/" + result["id"] + "/api/link", json={"token": token}
        )
        self.assertEqual(response.status, 200, await response.text())
        exchanged = await response.json()
        return self.origin + exchanged["path"].rstrip("/"), result, token

    async def history(self, base):
        response = await self.client.get(base + "/api/messages")
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_multiple_tabs_are_isolated_and_never_rebind_default(self):
        a, link, token = await self.link(self.a)
        b, _, _ = await self.link(self.b)
        for base, ident, title in ((a, self.a, "会话 A"), (b, self.b, "会话 B"), (a, self.a, "会话 A")):
            result = await self.history(base)
            self.assertEqual({m["session_id"] for m in result["messages"]}, {ident})
            self.assertEqual(result["state"]["conversation_title"], title)
        self.assertEqual(self.registry.sessions.bound("web"), self.default)
        response = await self.client.get(self.base + "/api/messages")
        self.assertEqual(response.status, 401)
        async with ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": self.origin}) as other:
            # A reusable NFC-style link also works in a fresh browser.
            response = await other.post(a + "/api/link", json={"token": token})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["path"], "/chat/c/" + link["id"] + "/")

    async def test_grant_does_not_authorize_other_views_or_global_apis(self):
        a, _, _ = await self.link()
        other = await self.plugin.scoped.manage("create_link", {"session_id": self.b})
        response = await self.client.get(self.base + "/c/" + other["id"] + "/api/messages")
        self.assertEqual(response.status, 401)
        for route in ("tasks", "notifications", "control", "login"):
            response = await self.client.post(a + "/api/" + route, json={"action": "list"})
            self.assertEqual(response.status, 404, route)
        for route in ("models", "model-avatar/builtin/default"):
            self.assertEqual((await self.client.get(a + "/api/" + route)).status, 404)
        for suffix in ("/api/tasks", "/api/notifications", "/api/session"):
            response = await self.client.get(self.base + suffix)
            self.assertEqual(response.status, 401, suffix)
        response = await self.client.post(
            a + "/api/conversation",
            json={
                "action": "cancel",
                "session_id": self.b,
            },
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            a + "/api/messages",
            json={
                "request_id": str(uuid.uuid4()),
                "text": "no",
                "session_id": self.b,
            },
        )
        self.assertEqual(response.status, 400)

    async def test_simultaneous_first_open_uses_independent_cookie_paths(self):
        (a, first, token), (b, second, _) = await asyncio.gather(self.link(self.a), self.link(self.b))
        self.assertEqual((await self.history(a))["state"]["session_id"], self.a)
        self.assertEqual((await self.history(b))["state"]["session_id"], self.b)
        paths = {c["path"] for c in self.client.cookie_jar if c.key == COOKIE}
        self.assertEqual(paths, {"/chat/c/" + first["id"] + "/", "/chat/c/" + second["id"] + "/"})
        response = await self.client.post(b + "/api/link", json={"token": token})
        self.assertEqual(response.status, 401)

    async def test_read_only_blocks_every_write_but_allows_logout(self):
        a, _, _ = await self.link(permission="read")
        self.assertEqual((await self.history(a))["state"]["conversation_access"], "read")
        for route in ("messages", "conversation", "uploads"):
            response = await self.client.post(a + "/api/" + route, json={})
            self.assertEqual(response.status, 403, route)
        response = await self.client.post(a + "/api/logout", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 401)

    async def test_upload_attachment_and_result_ownership(self):
        a, _, _ = await self.link(self.a)
        b, _, _ = await self.link(self.b)
        raw = io.BytesIO()
        Image.new("RGB", (4, 4), "blue").save(raw, "PNG")
        form = FormData()
        form.add_field("image", raw.getvalue(), filename="test.png", content_type="image/png")
        response = await self.client.post(a + "/api/uploads", data=form)
        self.assertEqual(response.status, 201, await response.text())
        upload = await response.json()
        self.assertTrue(upload["url"].startswith(a.removeprefix(self.origin) + "/api/uploads/"))
        response = await self.client.get(a + "/api/uploads/" + upload["id"])
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.get(b + "/api/uploads/" + upload["id"])).status, 404)
        reference = {"id": uuid.uuid4().hex, "version": "v1"}
        self.registry.main.store.put(
            "result-a", self.a, text="excerpt", status="complete", result_ref=reference
        )
        response = await self.client.get(b + "/api/results/" + reference["id"])
        self.assertEqual(response.status, 404)
        self.other.assert_not_awaited()
        response = await self.client.get(a + "/api/results/" + reference["id"] + "?version=wrong")
        self.assertEqual(response.status, 404)
        self.other.assert_not_awaited()
        response = await self.client.get(a + "/api/results/" + reference["id"])
        self.assertEqual(response.status, 200)
        self.assertIsInstance(self.other.await_args.args[0], FileRequest)
        self.assertEqual(self.other.await_args.args[0].arguments["version"], "v1")

    async def test_duplicate_input_has_one_dispatch_and_cross_session_reuse_fails(self):
        a, _, _ = await self.link(self.a)
        b, _, _ = await self.link(self.b)
        body = {"request_id": str(uuid.uuid4()), "text": "only once"}
        responses = await asyncio.gather(
            *[self.client.post(a + "/api/messages", json=body) for _ in range(2)]
        )
        self.assertEqual([r.status for r in responses], [202, 202])
        async with asyncio.timeout(2):
            while not self.registry.opened[self.a].backend.calls:
                await asyncio.sleep(0.01)
        self.assertEqual(self.registry.opened[self.a].backend.calls, ["only once"])
        response = await self.client.post(b + "/api/messages", json=body)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.registry.opened[self.b].backend.calls)
        self.assertEqual(self.registry.main.store.get("web:" + body["request_id"])["session_id"], self.a)

    async def test_scoped_cancel_cannot_cancel_other_session(self):
        a, _, _ = await self.link(self.a)
        chat = await self.registry.get(self.b)
        request_id = str(uuid.uuid4())
        await chat.submit(request_id, "other running", [])
        async with asyncio.timeout(2):
            while not chat.backend.busy:
                await asyncio.sleep(0.01)
        response = await self.client.post(
            a + "/api/conversation",
            json={
                "action": "cancel",
                "request_id": request_id,
            },
        )
        self.assertEqual(response.status, 400)
        self.assertTrue(chat.backend.busy)
        view = await self.plugin.scoped.views.open(self.a)
        with self.assertRaises(ValueError):
            await view.control("stop")  # A bound view never stops global voice output.

    async def test_sse_is_scoped_and_revocation_ends_existing_stream(self):
        a, link, _ = await self.link()
        async with self.client.get(a + "/api/events") as stream:
            self.assertEqual(stream.status, 200)
            chunk = await asyncio.wait_for(stream.content.readuntil(b"\n\n"), 2)
            self.assertNotIn(self.b.encode(), chunk)
            self.assertNotIn(self.default.encode(), chunk)
            await self.plugin.scoped.manage("revoke_link", {"id": link["id"]})
            await asyncio.wait_for(stream.read(), 2)
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 401)

    async def test_revoke_and_logout_are_local_and_tokens_are_not_exported(self):
        a, link, token = await self.link(self.a)
        b, _, _ = await self.link(self.b)
        listing = await self.plugin.scoped.manage("links", {})
        self.assertNotIn(token, str(listing))
        await self.client.post(a + "/api/logout", json={})
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 401)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM web_link_browsers").fetchone()[0], 1)
        await self.history(b)
        await self.plugin.scoped.manage("revoke_link", {"id": link["id"]})
        response = await self.client.post(a + "/api/link", json={"token": token})
        self.assertEqual(response.status, 401)
        await self.history(b)

    async def test_expiry_restart_and_credential_rotation(self):
        a, link, _ = await self.link()
        cookie = next(c.value for c in self.client.cookie_jar if c.key == COOKIE)
        restored = Links(self.store.db, self.plugin.auth.digest)
        self.assertEqual(restored.resolve(cookie, link["id"])["session_id"], self.a)
        with patch("assistant_runtime.plugins.web.links.time.time", return_value=10**12):
            with self.assertRaises(PermissionError):
                restored.resolve(cookie, link["id"])
        restored.credential = b"rotated"
        with self.assertRaises(PermissionError):
            restored.resolve(cookie, link["id"])
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 200)
        with self.store.db:
            self.store.db.execute("UPDATE web_links SET expires=1 WHERE id=?", (link["id"],))
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 401)

    async def test_exchange_origin_guards_rate_limit_and_no_owner_cookie(self):
        result = await self.plugin.scoped.manage("create_link", {"session_id": self.a})
        token = result["path"].split("#")[1]
        exchange = self.base + "/c/" + result["id"] + "/api/link"
        for origin in ("http://evil.example", ""):
            response = await self.client.post(exchange, json={"token": token}, headers={"Origin": origin})
            self.assertEqual(response.status, 403)
        for _ in range(10):
            response = await self.client.post(exchange, json={"token": "invalid"})
            self.assertEqual(response.status, 401)
            self.assertNotIn(token, await response.text())
        response = await self.client.post(exchange, json={"token": token})
        self.assertEqual(response.status, 400)
        self.assertFalse(any(c.key == "personal_agent_session" for c in self.client.cookie_jar))

    async def test_owner_login_and_scoped_logout_remain_independent(self):
        a, _, _ = await self.link()
        token = (self.plugin.api.state_dir.parent / "web-access-token").read_text().strip()
        response = await self.client.post(self.base + "/api/login", json={"token": token})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.history(self.base))["state"]["session_id"], self.default)
        await self.client.post(a + "/api/logout", json={})
        await self.history(self.base)
        self.assertEqual((await self.client.get(a + "/api/messages")).status, 401)

    async def test_management_rejects_unbounded_or_missing_targets(self):
        with self.assertRaises(ValueError):
            await self.plugin.scoped.manage("create_link", {"session_id": uuid.uuid4().hex})
        for args in (
            {"session_id": self.a, "expires_in": -1},
            {"session_id": self.a, "permission": "admin"},
            {"session_id": self.a, "expires_in": True},
        ):
            with self.assertRaises(ValueError):
                await self.plugin.scoped.manage("create_link", args)
        result = await self.plugin.scoped.manage("create_session", {"title": "independent"})
        self.assertNotIn(result["id"], {self.a, self.b, self.default})
        self.assertEqual(self.registry.sessions.bound("web"), self.default)
