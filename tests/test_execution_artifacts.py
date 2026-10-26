"""Native Windows publication handles, immutable reads and interrupted finalization."""

import asyncio
import base64
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from assistant_runtime.api.files import FileRequest
from assistant_runtime.core.execution.records import RunRecords
from assistant_runtime.core.workfiles import WorkFiles


@unittest.skipUnless(sys.platform == "win32", "Frozen publication backend requires native Windows")
class ArtifactTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / "state.db")
        self.addCleanup(self.db.close)
        self.files = WorkFiles(self.root, self.db)
        self.runs = RunRecords(self.db)
        self.run = str(uuid.uuid4())
        self.runs.create(self.run, "operator", "intent", {})
        self.runs.update(self.run, "completed")
        self.source = self.files.area("executions/" + self.run) / "artifacts" / "report.txt"
        self.source.write_bytes(b"FIRST VERSION")
        self.args = {
            "finalize_id": str(uuid.uuid4()),
            "execution_id": self.run,
            "name": "report.txt",
            "expires_at": time.time() + 3600,
        }

    async def call(self, action, **args):
        return await self.files.request(FileRequest(action, args))

    async def test_frozen_content_dedup_new_round_and_resumed_reads(self):
        first = await self.call("finalize", **self.args)
        self.source.write_bytes(b"SECOND VERSION")
        same = await self.call("finalize", **self.args)
        self.assertEqual(first, same)
        prefix = await self.call("artifact_read", id=first["id"], length=5)
        tail = await self.call("artifact_read", id=first["id"], offset=prefix["next_offset"])
        self.assertEqual(base64.b64decode(prefix["data"]) + base64.b64decode(tail["data"]), b"FIRST VERSION")
        self.assertTrue(tail["eof"])
        second = await self.call("finalize", **{**self.args, "finalize_id": str(uuid.uuid4())})
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["sha256"], second["sha256"])
        with self.assertRaisesRegex(ValueError, "different content"):
            await self.call("finalize", **{**self.args, "name": "other.txt"})

    async def test_open_writer_and_hardlinks_refuse_publication(self):
        with self.source.open("ab"):
            with self.assertRaises(OSError):
                await self.call("finalize", **self.args)
        failed = await self.call("artifact_get", id=self.args["finalize_id"])
        self.assertFalse(failed["readable"])
        self.assertEqual(failed["state"], "unavailable")
        os.link(self.source, self.source.with_name("link.txt"))
        with self.assertRaisesRegex(ValueError, "hard links"):
            await self.call("finalize", **{**self.args, "finalize_id": str(uuid.uuid4())})

    async def test_expiry_retain_release_and_collection(self):
        row = await self.call("finalize", **self.args)
        later = row["expires_at"] + 60
        retained = await self.call("artifact_retain", id=row["id"], expires_at=later)
        self.assertEqual(retained["expires_at"], later)
        await self.call("artifact_release", id=row["id"])
        with self.assertRaisesRegex(ValueError, "expired"):
            await self.call("artifact_read", id=row["id"])
        await self.files.publications.collect()
        self.assertFalse((self.files.publications.root / row["id"]).exists())
        self.assertEqual((await self.call("artifact_get", id=row["id"]))["state"], "expired")
        # Tombstone protects idempotency; an expired finalize must not snapshot new bytes.
        self.assertFalse((await self.call("finalize", **self.args))["readable"])

    async def test_recovery_never_publishes_partial_bytes_and_unknown_run_refused(self):
        ident = self.args["finalize_id"]
        with self.db:
            self.db.execute(
                "INSERT INTO published_artifacts VALUES(?,?,?,?,?,?,?,?)",
                (
                    ident,
                    "operator",
                    self.run,
                    json.dumps(self.args, sort_keys=True),
                    "preparing",
                    json.dumps({"name": "report.txt"}),
                    self.args["expires_at"],
                    time.time(),
                ),
            )
        self.files.publications.root.mkdir(parents=True)
        (self.files.publications.root / ident).write_bytes(b"PARTIAL")
        from assistant_runtime.core.workfiles.artifacts import ArtifactFiles

        self.files.publications = ArtifactFiles(self.files)
        self.assertEqual((await self.call("finalize", **self.args))["state"], "unavailable")
        with self.db:
            self.db.execute("UPDATE agent_runs SET state='unknown' WHERE id=?", (self.run,))
        with self.assertRaisesRegex(ValueError, "known terminal"):
            await self.call("finalize", **{**self.args, "finalize_id": str(uuid.uuid4())})

    async def test_tampering_ownership_paths_and_bounds(self):
        from assistant_runtime.api.plugins import CallIdentity, current_call

        row = await self.call("finalize", **self.args)
        token = current_call.set(CallIdentity("request", "another-owner"))
        try:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                await self.call("artifact_get", id=row["id"])
        finally:
            current_call.reset(token)
        for name in ("../outside", "other:stream", "NUL"):
            with self.assertRaises(ValueError):
                await self.call("finalize", **{**self.args, "finalize_id": str(uuid.uuid4()), "name": name})
        with self.assertRaises(ValueError):
            await self.call("artifact_read", id=row["id"], length=1024 * 1024 + 1)
        (self.files.publications.root / row["id"]).write_bytes(b"ALTERED")
        with self.assertRaisesRegex(ValueError, "integrity"):
            await self.call("artifact_read", id=row["id"])

    async def test_cancelled_caller_cannot_leave_snapshot_running_after_lock_released(self):
        import threading

        entered, release = threading.Event(), threading.Event()
        original = self.files.publications.snapshot

        def delayed(*args):
            entered.set()
            release.wait(5)
            return original(*args)

        with patch.object(self.files.publications, "snapshot", delayed):
            task = asyncio.create_task(self.call("finalize", **self.args))
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(self.files.publications.lock.locked())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue((await self.call("artifact_get", id=self.args["finalize_id"]))["readable"])
