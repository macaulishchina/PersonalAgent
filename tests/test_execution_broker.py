"""Controlled model-side file and check operations use host-bound grants."""

import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace

from assistant_runtime.core.execution.authority import Authority
from assistant_runtime.core.execution.contexts import ContextRecords
from assistant_runtime.core.execution.broker import ResourceBroker
from assistant_runtime.core.execution.records import RunRecords


@unittest.skipUnless(os.name == "nt", "Windows controlled resources")
class BrokerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.service = SimpleNamespace(
            records=RunRecords(self.db),
            authority=Authority(self.db, {"bridge": ["owner"]}),
            files=SimpleNamespace(root=Path(self.temporary.name)),
            finished={},
        )
        self.service.contexts = ContextRecords(self.service)
        self.broker = ResourceBroker(self.service)
        self.workspace, self.context, self.execution, self.grant = [str(uuid.uuid4()) for _ in range(4)]
        self.binding = dict(
            owner="owner",
            actor="member",
            context_id=self.context,
            workspace_id=self.workspace,
            workspace_version="v1",
            usage_scope_id="analysis",
        )
        self.service.contexts.workspace_create(
            "bridge",
            dict(
                workspace_id=self.workspace,
                owner="owner",
                version="v1",
                usage_scope_id="analysis",
                expires_at=time.time() + 600,
            ),
        )
        await self.broker.prepare(
            "bridge",
            dict(
                workspace_id=self.workspace,
                area="source",
                path="reference.txt",
                text="SOURCE",
                expected_sha256=None,
            ),
        )
        self.service.authority.issue(
            "bridge",
            dict(
                grant_id=self.grant,
                binding=self.binding,
                actions=["execute", "read", "edit", "check", "publish"],
                expires_at=time.time() + 30,
            ),
        )

    def begin(self):
        self.service.contexts.context_create(
            "bridge",
            dict(
                context_id=self.context,
                grant_id=self.grant,
                expires_at=time.time() + 300,
                model="model",
                effort="auto",
            ),
        )
        self.service.contexts.reserve(self.service.contexts.get("context", self.context), self.execution, 1)
        self.service.records.create(
            self.execution,
            "owner",
            self.execution,
            dict(context_id=self.context, model="model", effort="auto"),
        )
        self.service.records.update(self.execution, "running")
        self.broker.files.execution_area(self.workspace, self.execution)
        self.broker.open(self.execution)

    async def test_files_are_deduplicated_and_revocation_blocks_new_access(self):
        self.begin()
        args = dict(area="work", path="code.txt", text="CODE", expected_sha256=None)
        first = await self.broker.request(self.execution, "tool-write", "write", args)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(await self.broker.request(self.execution, "tool-write", "write", args), first)
        with self.assertRaises(ValueError):
            await self.broker.request(self.execution, "tool-write", "write", {**args, "text": "changed"})
        with self.assertRaises(PermissionError):
            await self.broker.request(self.execution, "tool-source", "write", {**args, "area": "source"})
        read = await self.broker.request(
            self.execution, "tool-read", "read", dict(area="work", path="code.txt")
        )
        self.assertEqual(read["result"]["text"], "CODE")
        self.service.authority.revoke("bridge", dict(grant_id=self.grant))
        with self.assertRaises(PermissionError):
            await self.broker.request(self.execution, "tool-late", "read", dict(area="work", path="code.txt"))
        self.assertTrue(await self.broker.fence(self.execution))
        self.assertEqual(self.db.execute("SELECT state FROM controlled_fences").fetchone()[0], "closed")

    async def test_registered_check_is_fixed_and_script_hash_is_enforced(self):
        await self.broker.register_check(
            "bridge",
            dict(
                workspace_id=self.workspace,
                name="verify",
                script='@echo off\r\necho VERIFIED>checked.txt\r\ntype "%PA_INPUTS%\\material.txt" > "%PA_ARTIFACTS%\\check.txt"\r\nexit /b 0\r\n',
                timeout_seconds=5,
            ),
        )
        self.begin()
        (self.broker.files.area(self.workspace, "inputs", self.execution) / "material.txt").write_text(
            "ROUND_INPUT"
        )
        result = await self.broker.request(self.execution, "check-one", "check", {"name": "verify"})
        self.assertEqual(
            (self.broker.files.area(self.workspace, "artifacts", self.execution) / "check.txt").read_text(),
            "ROUND_INPUT",
        )
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(result["result"]["tree_stopped"])
        self.assertEqual(
            (self.broker.files.root(self.workspace) / "work/checked.txt").read_text().strip(), "VERIFIED"
        )
        with self.assertRaises(ValueError):
            await self.broker.request(
                self.execution, "bad-args", "check", {"name": "verify", "command": "anything"}
            )
        script = self.broker.files.root(self.workspace) / "checks/verify.cmd"
        script.write_text("echo TAMPERED>bad.txt", encoding="utf-8")
        rejected = await self.broker.request(self.execution, "check-two", "check", {"name": "verify"})
        self.assertEqual(rejected["state"], "failed")
        self.assertFalse((self.broker.files.root(self.workspace) / "work/bad.txt").exists())
        self.assertTrue(await self.broker.fence(self.execution))

    async def test_prior_host_open_fence_is_never_assumed_stopped(self):
        self.begin()
        self.service.records.operations.host_start_id = str(uuid.uuid4())
        restored = ResourceBroker(self.service)
        self.assertFalse(await restored.fence(self.execution))
        with self.assertRaises(PermissionError):
            await restored.request(self.execution, "read", "read", dict(area="source", path="reference.txt"))
