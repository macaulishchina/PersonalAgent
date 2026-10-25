"""Administrator credential changes are immediate, authenticated operator operations."""

from platform_support import private_mode

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assistant_runtime.core.configuration.auth import AdminAccess, PasswordPolicyError
from assistant_runtime.core.configuration.files import ConfigurationFiles


class AdminPasswordTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "admin-token"
        self.admin = AdminAccess(self.path)
        self.original = self.path.read_text().strip()
        self.session = self.admin.login(self.original, "browser")

    def test_custom_password_persists_and_invalidates_every_session(self):
        other = self.admin.login(self.original, "second-browser")
        password = "自定义管理员密码-123456"
        self.admin.change_password(self.original, password, password, "browser")
        self.assertFalse(self.admin.valid(self.session))
        self.assertFalse(self.admin.valid(other))
        self.assertEqual(self.path.read_text(encoding="utf-8"), password + "\n")
        self.assertEqual(private_mode(self.path), 0o600)
        fresh = AdminAccess(self.path)
        self.assertTrue(fresh.valid(fresh.login(password, "restarted")))
        with self.assertRaises(PermissionError):
            fresh.login(self.original, "old-password")

    def test_invalid_new_password_or_confirmation_preserves_credential_and_session(self):
        cases = [
            ("short", "short"),
            ("123456", "123456"),
            ("a" * 257, "a" * 257),
            (" leading-space-password", " leading-space-password"),
            ("trailing-space-password ", "trailing-space-password "),
            ("new-password\nwith-lines", "new-password\nwith-lines"),
            ("new-password\x00with-null", "new-password\x00with-null"),
            (None, None),
            ("valid-custom-password", "different-confirmation"),
        ]
        for index, (new, confirmation) in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(PasswordPolicyError) as caught:
                    self.admin.change_password(self.original, new, confirmation, str(index))
                self.assertNotIn(self.original, str(caught.exception))
                self.assertEqual(self.path.read_text().strip(), self.original)
                self.assertTrue(self.admin.valid(self.session))

    def test_current_password_required_and_failed_attempts_are_rate_limited(self):
        for _ in range(10):
            with self.assertRaises(PermissionError):
                self.admin.change_password("wrong", "custom-password", "custom-password", "attacker")
        with self.assertRaises(PermissionError):
            self.admin.change_password(self.original, "custom-password", "custom-password", "attacker")
        self.assertEqual(self.path.read_text().strip(), self.original)
        self.assertTrue(self.admin.valid(self.session))

    def test_seven_digits_or_letters_are_accepted_without_complexity_requirement(self):
        for password in ("1234567", "abcdefg", "Abc1234"):
            with self.subTest(password_kind=password):
                self.admin.change_password(self.original, password, password, "browser")
                self.assertTrue(self.admin.valid(self.admin.login(password, "browser")))
                self.assertTrue(AdminAccess(self.path).login(password, "restarted"))
                self.original = password

    def test_failed_atomic_write_does_not_invalidate_existing_access(self):
        with patch("assistant_runtime.core.configuration.files.atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.admin.change_password(self.original, "custom-password", "custom-password", "browser")
        self.assertEqual(self.path.read_text().strip(), self.original)
        self.assertTrue(self.admin.valid(self.session))


class ConsolePasswordTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        from assistant_runtime.core.configuration.console import Console, web

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / "config.toml"
        self.entry.write_text("broken = [\n")
        self.console = object.__new__(Console)
        self.console.access = AdminAccess(self.root / "state/configuration/admin-token")
        self.console.settings = SimpleNamespace(root=self.root, public_origin="")
        self.console.writes = asyncio.Lock()

        def never_validate(_):
            raise AssertionError("Password change must not parse or apply business configuration")

        self.console.files = ConfigurationFiles(self.entry, self.root / "state/configuration", never_validate)
        app = web.Application(middlewares=[self.console.guard])
        app.add_routes(
            [
                web.post("/api/login", self.console.login),
                web.get("/api/admin-password", self.console.password_policy),
                web.post("/api/admin-password", self.console.change_password),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url("/")).rstrip("/")
        self.headers = {"Origin": self.origin}
        self.original = self.console.access.path.read_text().strip()
        self.payload = {
            "current_password": self.original,
            "new_password": "custom-password-for-operator",
            "confirmation": "custom-password-for-operator",
        }

    async def login(self, password):
        return await self.client.post("/api/login", json={"token": password}, headers=self.headers)

    async def test_login_origin_policy_change_and_no_secret_echo_with_broken_business_configuration(self):
        response = await self.client.get("/api/admin-password")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/admin-password", json=self.payload, headers=self.headers)
        self.assertEqual(response.status, 401)
        self.assertEqual((await self.login(self.original)).status, 200)
        response = await self.client.get("/api/admin-password")
        self.assertEqual(await response.json(), AdminAccess.password_policy())
        response = await self.client.post(
            "/api/admin-password", json=self.payload, headers={"Origin": "https://untrusted.example"}
        )
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/admin-password", json=self.payload, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"changed": True, "reauthentication_required": True})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.cookies["personal_agent_admin"]["max-age"], "0")
        self.assertEqual((await self.client.get("/api/admin-password")).status, 401)
        self.assertEqual((await self.login(self.original)).status, 403)
        self.assertEqual((await self.login(self.payload["new_password"])).status, 200)
        self.assertEqual(self.entry.read_text(), "broken = [\n")
        audit = (self.console.files.state / "audit.jsonl").read_text()
        self.assertEqual(json.loads(audit)["result"], "administrator_password_changed")
        self.assertNotIn(self.original, audit)
        self.assertNotIn(self.payload["new_password"], audit)

    async def test_failed_verification_or_confirmation_keeps_session_and_reports_no_credentials(self):
        await self.login(self.original)
        for changes, code in [({"current_password": "wrong"}, 403), ({"confirmation": "different"}, 400)]:
            response = await self.client.post(
                "/api/admin-password", json={**self.payload, **changes}, headers=self.headers
            )
            self.assertEqual(response.status, code)
            text = await response.text()
            self.assertNotIn(self.original, text)
            self.assertNotIn(self.payload["new_password"], text)
            self.assertEqual((await self.client.get("/api/admin-password")).status, 200)
        self.assertFalse((self.console.files.state / "audit.jsonl").exists())

    async def test_audit_failure_does_not_report_credential_change_as_failed(self):
        await self.login(self.original)
        with patch.object(self.console.files, "_audit", side_effect=OSError("disk full")):
            with self.assertLogs("assistant_runtime.core.configuration.console", level="WARNING"):
                response = await self.client.post(
                    "/api/admin-password", json=self.payload, headers=self.headers
                )
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.login(self.payload["new_password"])).status, 200)
