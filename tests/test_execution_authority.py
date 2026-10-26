"""Verified in-process controller identity and bounded grant leases."""

import sqlite3
import unittest
import uuid
from types import SimpleNamespace

from assistant_runtime.core.execution.authority import Authority
from assistant_runtime.core.execution.declaration import settings
from assistant_runtime.core.execution.service import ExecutionService
from assistant_runtime.api.plugins import current_call, CallIdentity


class GrantTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.now, self.tick = 1000.0, 10.0
        self.controllers = {"bridge": ["owner-a"]}
        self.service = Authority(
            self.db, self.controllers, wall=lambda: self.now, monotonic=lambda: self.tick
        )
        self.args = {
            "grant_id": str(uuid.uuid4()),
            "expires_at": self.now + 30,
            "actions": ["execute", "read"],
            "binding": dict(
                owner="owner-a",
                actor="member",
                context_id="context",
                workspace_id="workspace",
                workspace_version="v1",
                usage_scope_id="analysis",
            ),
        }

    def verify(self, **kwargs):
        return self.service.verify(
            "bridge",
            self.args["grant_id"],
            self.args["binding"],
            kwargs.get("action", "read"),
            kwargs.get("generation", 1),
        )

    def test_identity_scope_and_action_are_not_json_authority(self):
        with self.assertRaises(PermissionError):
            self.service.issue("untrusted", self.args)
        with self.assertRaises(ValueError):
            self.service.issue("bridge", {**self.args, "controller": "bridge"})
        receipt = self.service.issue("bridge", self.args)
        self.assertTrue(receipt["verified_this_start"])
        self.verify()
        with self.assertRaises(PermissionError):
            self.verify(action="edit")
        with self.assertRaises(PermissionError):
            self.service.verify(
                "bridge",
                self.args["grant_id"],
                {**self.args["binding"], "workspace_version": "v2"},
                "read",
                1,
            )
        with self.assertRaises(PermissionError):
            self.service.owned("other", self.args["grant_id"])
        self.controllers.clear()
        with self.assertRaises(PermissionError):
            self.verify()

    def test_expiry_monotonic_limit_and_duplicate_never_extend(self):
        self.service.issue("bridge", self.args)
        self.now -= 100  # Wall clock rollback invalidates even an otherwise live lease.
        with self.assertRaises(PermissionError):
            self.verify()
        self.tick += 31
        self.assertTrue(self.service.issue("bridge", self.args)["expired"])
        with self.assertRaises(PermissionError):
            self.verify()
        with self.assertRaises(ValueError):
            self.service.issue("bridge", {**self.args, "expires_at": self.now + 10})
        for expires in (self.now, self.now + 61, float("inf"), True):
            with self.assertRaises(ValueError):
                self.service.issue(
                    "bridge", {**self.args, "grant_id": str(uuid.uuid4()), "expires_at": expires}
                )

    def test_restart_requires_upstream_reverification_and_revocation_is_final(self):
        self.service.issue("bridge", self.args)
        self.service = Authority(
            self.db, self.controllers, wall=lambda: self.now, monotonic=lambda: self.tick
        )
        with self.assertRaises(PermissionError):
            self.verify()
        renewal = {"grant_id": self.args["grant_id"], "generation": 1, "expires_at": self.now + 30}
        self.service.renew("bridge", renewal)
        self.verify()
        first = self.service.revoke("bridge", {"grant_id": self.args["grant_id"]})
        second = self.service.revoke("bridge", {"grant_id": self.args["grant_id"]})
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(first["generation"], 2)
        with self.assertRaises(PermissionError):
            self.verify()
        with self.assertRaises(PermissionError):
            self.service.renew("bridge", {**renewal, "generation": 2})

    def test_configuration_has_no_wildcard_or_default_controller(self):
        self.assertEqual(settings({})["controllers"], {})
        for raw in ({"bridge": ["*"]}, {"bridge": []}, {"bad/name": ["owner"]}, {"bridge": "owner"}):
            with self.assertRaises(ValueError):
                settings({"controllers": raw})


class ModelBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_tool_cannot_invoke_control_handle(self):
        service = SimpleNamespace(stopping=False)
        token = current_call.set(CallIdentity("request", "work"))
        try:
            with self.assertRaises(PermissionError):
                await ExecutionService.control(service, "bridge", "grant_issue", {})
        finally:
            current_call.reset(token)


class ControlInjectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_resource_binds_installed_plugin_identity_outside_request_json(self):
        from assistant_runtime.app.bundled import resources_for

        calls = []

        async def control(principal, action, arguments):
            calls.append((principal, action, arguments))
            return {"ok": True}

        host = SimpleNamespace(backend=SimpleNamespace(executions=SimpleNamespace(control=control)))
        handle = resources_for(host, "trusted_bridge", ("execution_control",))["execution_control"]
        result = await handle.request("grant_get", {"grant_id": "example"})
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "trusted_bridge")
        self.assertEqual(calls[0][2], {"grant_id": "example"})
        host.backend.executions = None
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await handle.request("grant_get", {})
