"""Device-free access approval and owner login over actual local HTTP."""

import asyncio
import time
import unittest
import uuid
from unittest.mock import patch
from aiohttp import ClientSession, CookieJar
from assistant_runtime.api.conversation import ConversationAccessRequest
from assistant_runtime.api.ai_usage import current_usage
import test_web_links as fixtures


class WebAccessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.WebLinksTest.asyncSetUp(self)
        self.endpoint = "mock:device-a"
        self.entry = await self.plugin.scoped.manage(
            "create_entrance",
            {
                "session_id": self.a,
                "endpoint_id": self.endpoint,
                "title": "设备入口",
                "permission": "read",
            },
        )
        self.entry_base = self.origin + self.entry["path"].rstrip("/")

    async def asyncTearDown(self):
        await fixtures.WebLinksTest.asyncTearDown(self)

    async def manage(self, action, args=None):
        return await self.plugin.scoped.manage(action, args or {})

    async def device(self, action, args=None, endpoint=None):
        return await self.plugin.scoped.entrances.device_request(
            ConversationAccessRequest(action, endpoint or self.endpoint, args or {})
        )

    async def start(self, client=None):
        response = await (client or self.client).post(self.entry_base + "/api/request", json={})
        self.assertEqual(response.status, 202, await response.text())
        return (await response.json())["request"]

    async def approve(self, item, seconds=600):
        return await self.device(
            "decide", {"request_id": item["id"], "decision": "approve", "seconds": seconds}
        )

    async def claim(self, client=None):
        response = await (client or self.client).post(self.entry_base + "/api/claim", json={})
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def login(self):
        token = (self.plugin.api.state_dir.parent / "web-access-token").read_text().strip()
        response = await self.client.post(self.base + "/api/login", json={"token": token})
        self.assertEqual(response.status, 200)

    async def test_public_entry_exposes_no_conversation_or_approver_authority(self):
        response = await self.client.get(self.entry_base + "/api/access")
        result = await response.json()
        self.assertEqual(result, {"title": "设备入口", "account_signed_in": False, "request": None})
        for mode in ("account", "visit"):
            self.assertEqual(
                (await self.client.get(self.entry_base + "/" + mode + "/api/messages")).status, 401
            )
        for action in ("approve", "decide", "pending"):
            self.assertEqual(
                (await self.client.post(self.entry_base + "/api/" + action, json={})).status, 404
            )
        for action in ("request", "claim", "account"):
            response = await self.client.post(
                self.entry_base + "/api/" + action, json={"seconds": 600, "session_id": self.b}
            )
            self.assertEqual(response.status, 400)

    async def test_authenticated_endpoint_decides_duration_and_lease_never_slides(self):
        item = await self.start()
        self.assertEqual(item["state"], "pending")
        self.assertEqual(len(item["code"]), 6)
        self.assertEqual((await self.device("pending"))["requests"][0]["request_id"], item["id"])
        self.assertEqual((await self.device("pending", endpoint="mock:other"))["requests"], [])
        with self.assertRaises(PermissionError):
            await self.device(
                "decide",
                {"request_id": item["id"], "decision": "approve", "seconds": 600},
                endpoint="mock:other",
            )
        self.assertEqual((await self.client.post(self.entry_base + "/api/claim", json={})).status, 401)
        approved = await self.approve(item)
        claimed = await self.claim()
        self.assertEqual(claimed["expires"], approved["expires"])
        visitor = self.origin + claimed["path"].rstrip("/")
        response = await self.client.get(visitor + "/api/messages")
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual({m["session_id"] for m in result["messages"]}, {self.a})
        self.assertEqual(result["state"]["conversation_expires"], approved["expires"])
        self.assertEqual(result["state"]["conversation_access"], "read")
        self.assertEqual((await self.client.post(visitor + "/api/messages", json={})).status, 403)
        with patch("assistant_runtime.plugins.web.access.time.time", return_value=approved["expires"] + 1):
            self.assertEqual((await self.client.get(visitor + "/api/session")).status, 401)
        self.assertEqual(self.registry.sessions.bound("web"), self.default)

    async def test_request_and_decision_retries_cannot_extend_or_reopen_authority(self):
        item = await self.start()
        self.assertEqual((await self.start())["id"], item["id"])
        self.assertEqual(len([e for e in self.web_context.events if e[0] == "web.access_requested"]), 1)
        first = await self.approve(item)
        second = await self.approve(item)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            await self.approve(item, 1200)
        grant = await self.claim()
        self.assertEqual(await self.claim(), grant)  # Cookie received, acknowledgement lost.
        async with ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": self.origin}) as other:
            response = await other.post(self.entry_base + "/api/claim", json={})
            self.assertEqual(response.status, 401)
        accesses = (await self.manage("accesses"))["accesses"]
        self.assertEqual(len(accesses), 1)
        await self.manage("close_access", {"kind": "temporary", "id": accesses[0]["id"]})
        self.assertEqual((await self.client.post(self.entry_base + "/api/claim", json={})).status, 401)
        await self.approve(item)
        self.assertEqual((await self.client.post(self.entry_base + "/api/claim", json={})).status, 401)

    async def test_denial_cancellation_expiry_and_approval_deadline(self):
        item = await self.start()
        await self.device("decide", {"request_id": item["id"], "decision": "deny"})
        self.assertEqual((await self.client.post(self.entry_base + "/api/claim", json={})).status, 401)
        next_item = await self.start()
        await self.client.post(self.entry_base + "/api/cancel", json={})
        with self.assertRaises(ValueError):
            await self.approve(next_item)
        expired = await self.start()
        with patch("assistant_runtime.plugins.web.access.time.time", return_value=time.time() + 301):
            with self.assertRaises(ValueError):
                await self.approve(expired)
        late = await self.start()
        approved = await self.approve(late)
        with patch("assistant_runtime.plugins.web.access.time.time", return_value=approved["expires"] - 500):
            self.assertEqual((await self.client.post(self.entry_base + "/api/claim", json={})).status, 401)

    async def test_account_login_bypasses_device_duration_and_read_only_policy(self):
        self.assertEqual((await self.client.post(self.entry_base + "/api/account", json={})).status, 401)
        await self.login()
        response = await self.client.post(self.entry_base + "/api/account", json={})
        self.assertEqual(response.status, 200)
        account = self.origin + (await response.json())["path"].rstrip("/")
        response = await self.client.get(account + "/api/messages")
        result = await response.json()
        self.assertEqual(result["state"]["conversation_access"], "chat")
        self.assertEqual(result["state"]["conversation_auth"], "account")
        self.assertGreater(result["state"]["conversation_expires"], time.time() + 6 * 86400)
        self.assertEqual((await self.client.get(account + "/api/tasks")).status, 200)
        self.assertEqual((await self.client.get(account + "/api/notifications")).status, 200)
        self.assertEqual(
            (
                await self.client.post(
                    account + "/api/messages", json={"request_id": str(uuid.uuid4()), "text": "account input"}
                )
            ).status,
            202,
        )
        self.assertEqual((await self.device("pending"))["requests"], [])
        self.assertEqual(self.registry.sessions.bound("web"), self.default)

    async def test_admin_lists_every_access_and_can_close_individual_or_owner_login(self):
        await self.login()
        account_response = await self.client.post(self.entry_base + "/api/account", json={})
        account = self.origin + (await account_response.json())["path"].rstrip("/")
        legacy, _, _ = await fixtures.WebLinksTest.link(self, self.b)
        item = await self.start()
        await self.approve(item)
        grant = await self.claim()
        visitor = self.origin + grant["path"].rstrip("/")
        accesses = (await self.manage("accesses"))["accesses"]
        self.assertEqual({a["kind"] for a in accesses}, {"account_login", "account", "temporary", "link"})
        self.assertNotIn("binding", str(accesses))
        temporary = next(a for a in accesses if a["kind"] == "temporary")
        async with self.client.get(visitor + "/api/events") as stream:
            self.assertEqual(stream.status, 200)
            await self.manage("close_access", {"id": temporary["id"], "kind": "temporary"})
            await asyncio.wait_for(stream.read(), 2)
        self.assertEqual((await self.client.get(visitor + "/api/messages")).status, 401)
        self.assertEqual((await self.client.get(account + "/api/messages")).status, 200)
        account_access = next(a for a in accesses if a["kind"] == "account")
        await self.manage("close_access", {"id": account_access["id"], "kind": "account"})
        self.assertEqual((await self.client.get(account + "/api/session")).status, 401)
        self.assertEqual((await self.client.get(self.base + "/api/session")).status, 200)
        self.assertEqual((await self.client.post(self.entry_base + "/api/account", json={})).status, 200)
        owner = next(a for a in accesses if a["kind"] == "account_login")
        await self.manage("close_access", {"id": owner["id"], "kind": "account_login"})
        self.assertEqual((await self.client.get(self.base + "/api/session")).status, 401)
        self.assertEqual((await self.client.get(account + "/api/session")).status, 401)
        legacy_access = next(a for a in accesses if a["kind"] == "link")
        await self.manage("close_access", {"id": legacy_access["id"], "kind": "link"})
        self.assertEqual((await self.client.get(legacy + "/api/messages")).status, 401)

    async def test_expired_or_closed_visit_never_escalates_to_owner_cookie(self):
        await self.login()
        self.assertEqual((await self.client.get(self.entry_base + "/visit/api/session")).status, 401)
        self.assertEqual((await self.client.get(self.entry_base + "/account/api/session")).status, 401)
        await self.client.post(self.entry_base + "/api/account", json={})
        item = await self.start()
        await self.approve(item, 1)
        await self.claim()
        with patch("assistant_runtime.plugins.web.access.time.time", return_value=time.time() + 2):
            self.assertEqual((await self.client.get(self.entry_base + "/visit/api/session")).status, 401)
            self.assertEqual((await self.client.get(self.entry_base + "/account/api/session")).status, 200)

    async def test_capacity_rate_limit_revoked_entry_and_model_scope_are_rejected(self):
        for _ in range(6):
            await self.start()
            await self.client.post(self.entry_base + "/api/cancel", json={})
        self.assertEqual((await self.client.post(self.entry_base + "/api/request", json={})).status, 400)
        marker = current_usage.set(object())
        try:
            with self.assertRaises(PermissionError):
                await self.device("pending")
        finally:
            current_usage.reset(marker)
        await self.manage("revoke_entrance", {"id": self.entry["id"]})
        self.assertEqual((await self.client.get(self.entry_base + "/api/access")).status, 401)

    async def test_migration_preserves_existing_link_and_request_state(self):
        from assistant_runtime.plugins.web.access import AccessRequests

        item = await self.start()
        reopened = AccessRequests(self.store.db, self.plugin.auth.digest)
        self.assertEqual(reopened.pending(self.endpoint)[0]["request_id"], item["id"])
        await self.approve(item)
        grant = await self.claim()
        secret = next(c.value for c in self.client.cookie_jar if c.key == "personal_agent_visit")
        self.assertEqual(reopened.resolve(self.entry["id"], "temporary", secret)["expires"], grant["expires"])
        reopened.credential = b"changed-owner-credential"
        with self.assertRaises(PermissionError):
            reopened.resolve(self.entry["id"], "temporary", secret)

    async def test_request_capacity_and_foreign_browser_cannot_claim(self):
        clients = [
            ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": self.origin})
            for _ in range(4)
        ]
        try:
            item = await self.start(clients[0])
            await self.approve(item)
            self.assertEqual((await clients[1].post(self.entry_base + "/api/claim", json={})).status, 401)
            await self.start(clients[1])
            await self.start(clients[2])
            self.assertEqual((await clients[3].post(self.entry_base + "/api/request", json={})).status, 400)
        finally:
            await asyncio.gather(*(client.close() for client in clients))

    async def test_device_rejects_missing_or_invalid_duration_and_foreign_session(self):
        item = await self.start()
        for seconds in (None, True, 0, -1, 10**20, "600"):
            with self.assertRaises(ValueError):
                await self.approve(item, seconds)
        with self.assertRaises(ValueError):
            await self.device(
                "decide",
                {"request_id": item["id"], "decision": "approve", "seconds": 600, "session_id": self.b},
            )
