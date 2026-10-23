"""Registered input ownership, snapshots, bounds and execution tool scopes."""

import base64
from pathlib import Path
import tempfile
import unittest

from assistant_runtime.api import FileRequest
from assistant_runtime.api.plugins import CallIdentity, current_call
from assistant_runtime.app.store import Store
from assistant_runtime.core.workfiles import WorkFiles
from assistant_runtime.core.pi.scope import select


class InputFilesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.files = WorkFiles(self.root, self.store.db)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def put(self, **extra):
        return await self.files.inputs.request(
            FileRequest(
                "put", {"name": "sample.txt", "data": base64.b64encode(b"original").decode(), **extra}
            )
        )

    async def test_caller_ownership_and_durable_materialization(self):
        token = current_call.set(CallIdentity("request", "work-a"))
        try:
            ref = await self.put()
            records = self.files.inputs.capture([ref["id"]])
        finally:
            current_call.reset(token)
        token = current_call.set(CallIdentity("other", "work-b"))
        try:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.files.inputs.capture([ref["id"]])
        finally:
            current_call.reset(token)
        reopened = WorkFiles(self.root, self.store.db)
        self.assertEqual(reopened.inputs.get(ref["id"])["sha256"], ref["sha256"])
        area = self.files.area("executions/test")
        images, files = reopened.inputs.materialize(records, area)
        self.assertEqual(images, [])
        self.assertEqual(Path(files[0]["path"]).read_text(), "original")
        self.assertEqual(Path(files[0]["path"]).stat().st_mode & 0o777, 0o600)
        self.files.inputs.path(ref["id"]).write_text("modified")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.files.inputs.materialize(records, area)

    async def test_malformed_and_unbounded_resources_rejected(self):
        for extra in (
            {"name": "../secret"},
            {"data": "!!!!"},
            {"path": "/etc/passwd"},
            {"kind": "url"},
            {"data": "A" * (3 * 1024 * 1024)},
            {"data": ""},
        ):
            with self.subTest(extra=list(extra)):
                with self.assertRaises(ValueError):
                    await self.put(**extra)
        ref = await self.put()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.files.inputs.capture([ref["id"], ref["id"]])
        path = self.files.inputs.path(ref["id"])
        path.unlink()
        path.symlink_to(self.root / "state.db")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.files.inputs.get(ref["id"])

    def test_scope_narrows_only_and_captures_without_mutating_config(self):
        servers = {"sample": {"enabled_tools": ["read", "write"]}, "off": {"enabled": False}}
        paths = [Path("/registered/alpha"), Path("/registered/beta")]
        result = select(["read", "mcp__sample__read"], ["beta"], servers, paths)
        self.assertEqual(result["mcp_servers"]["sample"]["enabled_tools"], ["read"])
        self.assertEqual(servers["sample"]["enabled_tools"], ["read", "write"])
        self.assertEqual(result["skill_paths"], ["/registered/beta"])
        self.assertFalse(result["mcp_servers"]["off"]["enabled"])
        self.assertEqual(select([], [], servers, paths)["skill_paths"], [])
        for tools, skills in [
            (["bogus"], None),
            (["mcp__off__read"], None),
            (["mcp__sample__delete"], None),
            (None, ["missing"]),
            (["read", "read"], None),
            ("read", None),
        ]:
            with self.assertRaises(ValueError):
                select(tools, skills, servers, paths)
