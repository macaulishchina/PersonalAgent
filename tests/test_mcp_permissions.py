"""MCP operator edits preserve private transport state and share runtime policy."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.core.integrations.configuration import patch_registry, tool_permission
from assistant_runtime.core.integrations.registry import McpRegistry
from assistant_runtime.core.integrations.settings import definition


def registry():
    return {
        "revision": 2,
        "removed": [],
        "servers": {
            "remote": {
                "revision": 1,
                "definition": definition(
                    {
                        "approval": "reject",
                        "transport": {
                            "type": "http",
                            "url": "https://example.invalid/private?token=hidden-url-value",
                            "headers": {"Authorization": "hidden-header-value"},
                            "env_headers": {"x-key": "SHARED_KEY"},
                        },
                    }
                ),
            },
            "local": {
                "revision": 2,
                "definition": definition(
                    {
                        "approval": "approve",
                        "transport": {
                            "type": "stdio",
                            "command": "python",
                            "args": ["hidden-argument-value", "", "repeat", "repeat"],
                            "env": {"TOKEN": "hidden-environment-value"},
                        },
                    }
                ),
            },
        },
    }


def edit(changes, name="remote", revision=2):
    return {"revision": revision, "changes": [{"name": name, "op": "configure", "changes": changes}]}


class PolicyTest(unittest.TestCase):
    def test_partial_edits_preserve_hidden_fields_other_services_and_unmodified_policy(self):
        old = registry()
        candidate = patch_registry(old, edit({"enabled": False}))
        self.assertEqual(
            candidate["servers"]["remote"]["definition"]["transport"],
            old["servers"]["remote"]["definition"]["transport"],
        )
        self.assertEqual(candidate["servers"]["remote"]["definition"]["approval"], "reject")
        self.assertEqual(candidate["servers"]["local"], old["servers"]["local"])
        self.assertEqual(old, registry())
        candidate = patch_registry(old, edit({"transport": {"headers": {}}}))
        self.assertEqual(
            candidate["servers"]["remote"]["definition"]["transport"]["url"],
            old["servers"]["remote"]["definition"]["transport"]["url"],
        )
        self.assertEqual(candidate["servers"]["remote"]["definition"]["transport"]["headers"], {})
        self.assertEqual(patch_registry(old, edit({})), old)

    def test_shared_approve_default_and_only_explicit_allow_deny_policies(self):
        with tempfile.TemporaryDirectory() as folder:
            r = McpRegistry(folder, {})
            r.configure("new", {"transport": {"type": "stdio", "command": "python"}}, 0)
            self.assertEqual(r.get("new")["definition"]["approval"], "approve")
            with self.assertRaises(ValueError):
                r.configure(
                    "unsupported",
                    {"transport": {"type": "stdio", "command": "python"}, "approval": "prompt"},
                    1,
                )
        candidate = patch_registry(
            registry(), edit({"transport": {"type": "stdio", "command": "python"}}, "new")
        )
        self.assertEqual(candidate["servers"]["new"]["definition"]["approval"], "approve")
        self.assertEqual(candidate["servers"]["remote"]["definition"]["approval"], "reject")
        with self.assertRaises(ValueError):
            patch_registry(registry(), edit({"approval": "prompt"}, "local"))

    def test_revision_removal_and_transport_switch_are_explicit(self):
        with self.assertRaises(ValueError):
            patch_registry(registry(), edit({}, revision=1))
        candidate = patch_registry(registry(), edit({"transport": {"type": "stdio", "command": "python"}}))
        self.assertNotIn("headers", candidate["servers"]["remote"]["definition"]["transport"])
        removed = patch_registry(registry(), {"revision": 2, "changes": [{"name": "remote", "op": "remove"}]})
        self.assertEqual(removed["removed"], ["remote"])
        self.assertNotIn("remote", removed["servers"])

    def test_effective_policy_precedence_and_no_new_tools_in_allowlist(self):
        spec = registry()["servers"]["remote"]["definition"]
        self.assertEqual(tool_permission(spec, "read"), "rejected")
        spec["tool_approvals"]["read"] = "approve"
        self.assertEqual(tool_permission(spec, "read"), "rejected")
        spec["approval"] = "approve"
        self.assertEqual(tool_permission(spec, "read"), "allowed")
        spec["deny_tools"] = ["read"]
        self.assertEqual(tool_permission(spec, "read"), "denied")
        spec["deny_tools"] = []
        spec["allow_tools"] = ["read"]
        self.assertEqual(tool_permission(spec, "future"), "not_selected")
        spec["approval"] = "reject"
        self.assertEqual(tool_permission(spec, "read"), "rejected")
        spec["enabled"] = False
        self.assertEqual(tool_permission(spec, "read"), "disabled")
        spec["enabled"] = True
        spec["approval"] = "approve"
        spec["contexts"] = ["foreground"]
        self.assertEqual(tool_permission(spec, "read", "background"), "scope_excluded")
        self.assertEqual(tool_permission(spec, "read", "foreground", []), "execution_excluded")


class OperatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / "config.toml"
        self.entry.write_text("plugins=[]\n")
        self.registry_path = self.root / "config/mcp/servers.json"
        self.registry_path.parent.mkdir(parents=True)
        self.registry_path.write_text(json.dumps(registry()))
        self.business = BusinessProcess(ManagementSettings(self.root, self.entry))
        self.console = Console(self.business)
        app = web.Application(middlewares=[self.console.guard])
        app.add_routes(
            [
                web.post("/api/login", self.console.login),
                web.get("/api/permissions/mcp", self.console.permissions_inventory),
                web.post("/api/permissions/mcp", self.console.permissions_preview),
                web.post("/api/permissions/mcp/verify", self.console.permissions_verify),
                web.post("/api/configuration", self.console.save),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addAsyncCleanup(self.console.close)
        self.headers = {"Origin": str(self.client.make_url("/")).rstrip("/")}
        self.version = self.business.files.snapshot().version

    async def login(self):
        await self.client.post(
            "/api/login", json={"token": self.console.access.path.read_text().strip()}, headers=self.headers
        )

    async def test_auth_origin_and_stale_versions_are_enforced_without_business(self):
        self.assertEqual((await self.client.get("/api/permissions/mcp")).status, 401)
        await self.login()
        response = await self.client.get("/api/permissions/mcp")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body["running"])
        self.assertEqual(body["servers"][0]["definition"]["approval"], "reject")
        for secret in (
            "hidden-url-value",
            "hidden-header-value",
            "hidden-environment-value",
            "hidden-argument-value",
        ):
            self.assertNotIn(secret, json.dumps(body))
        self.assertEqual(
            (
                await self.client.post(
                    "/api/permissions/mcp",
                    json={"version": self.version},
                    headers={"Origin": "https://evil.invalid"},
                )
            ).status,
            403,
        )
        self.assertEqual(
            (
                await self.client.post("/api/permissions/mcp", json={"version": "old"}, headers=self.headers)
            ).status,
            409,
        )

    async def test_shared_save_preserves_secrets_detects_external_writers_and_audits_no_values(self):
        await self.login()
        before = self.registry_path.read_bytes()
        data = {"version": self.version, "patch": edit({"approval": "approve"})}
        response = await self.client.post("/api/permissions/mcp", json=data, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.registry_path.read_bytes(), before)
        response = await self.client.post(
            "/api/configuration",
            json={
                "version": self.version,
                "patches": [],
                "resources": [{"id": "mcp", "op": "patch", "value": data["patch"]}],
            },
            headers=self.headers,
        )
        self.assertEqual(response.status, 200, await response.text())
        saved = json.loads(self.registry_path.read_text())
        self.assertEqual(saved["servers"]["remote"]["definition"]["approval"], "approve")
        self.assertEqual(
            saved["servers"]["remote"]["definition"]["transport"],
            registry()["servers"]["remote"]["definition"]["transport"],
        )
        self.assertEqual(saved["servers"]["local"], registry()["servers"]["local"])
        audit = (self.business.state / "audit.jsonl").read_text()
        self.assertNotIn("hidden-", audit)
        self.assertIn("mcp.remote.approval", audit)
        self.assertEqual(
            (await self.client.post("/api/permissions/mcp", json=data, headers=self.headers)).status, 409
        )

    async def test_cli_writer_conflict_and_disk_failure_preserve_complete_configuration(self):
        await self.login()
        writer = McpRegistry(self.root, {})
        writer.configure("remote", {"enabled": False}, 2)
        response = await self.client.post(
            "/api/permissions/mcp",
            json={"version": self.version, "patch": edit({"approval": "approve"})},
            headers=self.headers,
        )
        self.assertEqual(response.status, 409)
        self.assertFalse(
            json.loads(self.registry_path.read_text())["servers"]["remote"]["definition"]["enabled"]
        )
        env = self.root / ".env"
        env.write_text("SHARED_KEY=hidden-original-value\n")
        before_registry, before_env = self.registry_path.read_bytes(), env.read_bytes()
        from assistant_runtime.core.configuration.files import atomic

        failed = False

        def fail_once(path, value):
            nonlocal failed
            if Path(path) == self.registry_path and not failed:
                failed = True
                raise OSError("synthetic disk failure")
            atomic(path, value)

        version = self.business.files.snapshot().version
        operation = {"id": "mcp", "op": "patch", "value": edit({"approval": "approve"}, revision=3)}
        with (
            patch("assistant_runtime.core.configuration.files.atomic", side_effect=fail_once),
            self.assertRaises(OSError),
        ):
            self.business.files.patch(
                version,
                [],
                resource_changes=[
                    operation,
                    {
                        "id": "credentials",
                        "key": "SHARED_KEY",
                        "op": "replace_secret",
                        "value": "replacement-value",
                    },
                ],
            )
        self.assertEqual(self.registry_path.read_bytes(), before_registry)
        self.assertEqual(env.read_bytes(), before_env)

    async def test_draft_verification_never_saves_and_failed_receipts_do_not_show_cached_tools_as_current(
        self,
    ):
        await self.login()
        before = self.registry_path.read_bytes()
        self.business.request = AsyncMock(
            return_value={
                "state": "verified",
                "tools_count": 1,
                "tools": [
                    {"name": "read", "description": "synthetic", "annotations": {"readOnlyHint": True}}
                ],
            }
        )
        response = await self.client.post(
            "/api/permissions/mcp/verify",
            json={"version": self.version, "name": "remote", "patch": edit({"approval": "approve"})},
            headers=self.headers,
        )
        self.assertEqual(response.status, 200)
        check = await response.json()
        self.assertTrue(check["current"])
        self.assertEqual(check["configuration_version"], self.version)
        self.assertEqual(check["server_revision"], 3)
        self.assertEqual(check["validation"], "initialize_and_discover_no_tool_execution")
        self.assertEqual(check["tools"][0]["permission"], "allowed")
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertEqual(self.business.request.call_args.args[0]["action"], "verify_draft")
        inventory = self.console.permissions.inventory()
        self.assertFalse(inventory["servers"][0]["verification"]["foreground"]["current"])
        self.business.request = AsyncMock(return_value={"state": "unavailable", "tools": []})
        await self.console.permissions.verify({"version": self.version, "name": "remote"})
        check = self.console.permissions.inventory()["servers"][0]["verification"]["foreground"]
        self.assertEqual(check["state"], "unavailable")
        self.assertEqual(check["tools"], [])
        self.business.request = AsyncMock(return_value={"state": "verified", "tools": [{"name": "read"}]})
        await self.console.permissions.verify({"version": self.version, "name": "remote"})
        self.business.request = AsyncMock(side_effect=TimeoutError())
        failed = await self.console.permissions.verify({"version": self.version, "name": "remote"})
        self.assertEqual(failed["state"], "unavailable")
        self.assertEqual(failed["tools"], [])
        self.assertIn("未就绪", failed["reason"])
        check = self.console.permissions.inventory()["servers"][0]["verification"]["foreground"]
        self.assertEqual(check["state"], "unavailable")
        self.assertEqual(check["tools"], [])

    async def test_verification_cancellation_closes_owned_work(self):
        started = asyncio.Event()

        async def wait(_):
            started.set()
            await asyncio.Event().wait()

        self.business.request = wait
        task = asyncio.create_task(
            self.console.permissions.verify({"version": self.version, "name": "remote"})
        )
        await started.wait()
        await self.console.permissions.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.console.permissions.pending)
