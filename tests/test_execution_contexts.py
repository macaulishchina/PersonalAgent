"""Persistent context/resource lifecycle; no provider or isolation claim from unit tests."""

import sqlite3
import time
import unittest
import uuid
from types import SimpleNamespace

from assistant_runtime.core.execution.authority import Authority
from assistant_runtime.core.execution.contexts import ContextRecords
from assistant_runtime.core.execution.records import RunRecords


class ContextTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.service = SimpleNamespace(
            records=RunRecords(self.db), authority=Authority(self.db, {"bridge": ["owner"]})
        )
        self.store = ContextRecords(self.service)
        self.workspace = str(uuid.uuid4())
        self.ws = {
            "workspace_id": self.workspace,
            "owner": "owner",
            "version": "v1",
            "usage_scope_id": "analysis",
            "expires_at": time.time() + 600,
        }
        self.store.workspace_create("bridge", self.ws)
        self.args = self.context()

    def context(self):
        ident, grant = str(uuid.uuid4()), str(uuid.uuid4())
        binding = dict(
            owner="owner",
            actor="member",
            context_id=ident,
            workspace_id=self.workspace,
            workspace_version="v1",
            usage_scope_id="analysis",
        )
        self.service.authority.issue(
            "bridge",
            dict(grant_id=grant, binding=binding, actions=["execute", "read"], expires_at=time.time() + 30),
        )
        args = dict(
            context_id=ident, grant_id=grant, expires_at=time.time() + 300, model="model", effort="auto"
        )
        self.store.context_create("bridge", args)
        return args

    def row(self):
        return self.store.get("context", self.args["context_id"])

    def test_workspace_conflicts_cover_different_contexts_and_unknown_retains_claim(self):
        other = self.context()
        first = self.store.reserve(self.row(), "run", 1)
        self.service.records.create("run", "owner", "run", {"model": "model", "effort": "auto"})
        second = self.store.get("context", other["context_id"])
        with self.assertRaisesRegex(RuntimeError, "Workspace"):
            self.store.reserve(second, "other", 1)
        with self.assertRaisesRegex(RuntimeError, "Context"):
            self.store.reserve(first, "another", first["revision"])
        self.service.records.update("run", "unknown")
        self.store.finish(first["id"], "run", stopped=False)
        self.assertEqual(self.row()["data"]["state"], "uncertain")
        self.assertEqual(self.store.get("workspace", self.workspace)["data"]["active_execution"], "run")
        with self.assertRaises(RuntimeError):
            self.store.close("bridge", "context", dict(id=first["id"], revision=self.row()["revision"]))
        # A future trusted reconciler may prove stops; unknown result/context remain unknown.
        self.store.finish(first["id"], "run", stopped=True)
        self.assertEqual(self.service.records.get("run")["state"], "unknown")
        self.assertEqual(self.row()["data"]["state"], "uncertain")
        self.store.reserve(second, "explicit-new-run", 1)

    def test_restart_does_not_infer_stopped_or_replay(self):
        self.store.reserve(self.row(), "run", 1)
        restored = ContextRecords(self.service)
        restored.recover()
        restored.recover()
        self.assertEqual(restored.get("context", self.args["context_id"])["data"]["state"], "uncertain")
        self.assertEqual(restored.get("workspace", self.workspace)["data"]["active_execution"], "run")

    def test_idempotency_expiry_revision_and_retention_references(self):
        self.assertEqual(self.store.context_create("bridge", self.args)["revision"], 1)
        self.assertEqual(self.store.workspace_create("bridge", self.ws)["revision"], 1)
        with self.assertRaises(ValueError):
            self.store.context_create("bridge", {**self.args, "model": "different"})
        with self.assertRaises(ValueError):
            self.store.reserve(self.row(), "run", 2)
        with self.assertRaises(RuntimeError):
            self.store.close("bridge", "workspace", dict(id=self.workspace, revision=1))
        with self.assertRaises(ValueError):
            self.store.retain(
                "bridge",
                "context",
                dict(id=self.args["context_id"], revision=1, expires_at=time.time() + 800),
            )
        closed = self.store.close("bridge", "context", dict(id=self.args["context_id"], revision=1))
        self.assertEqual(closed["state"], "closed")
        released = self.store.close("bridge", "workspace", dict(id=self.workspace, revision=1))
        self.assertTrue(released["released"])
        with self.assertRaises(PermissionError):
            self.store.verify(self.row())

    def test_reauthorization_can_change_actor_but_not_usage_or_workspace(self):
        row = self.row()
        binding = {**row["data"]["binding"], "actor": "reviewer"}
        grant = str(uuid.uuid4())
        self.service.authority.issue(
            "bridge", dict(grant_id=grant, binding=binding, actions=["execute"], expires_at=time.time() + 30)
        )
        view = self.store.authorize("bridge", dict(context_id=row["id"], grant_id=grant, revision=1))
        self.assertEqual(view["binding"]["actor"], "reviewer")
        invalid = str(uuid.uuid4())
        self.service.authority.issue(
            "bridge",
            dict(
                grant_id=invalid,
                binding={**binding, "usage_scope_id": "fresh-budget"},
                actions=["execute"],
                expires_at=time.time() + 30,
            ),
        )
        with self.assertRaises(PermissionError):
            self.store.authorize("bridge", dict(context_id=row["id"], grant_id=invalid, revision=2))
