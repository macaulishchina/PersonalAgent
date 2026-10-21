"""Durable, coalesced refreshes are independent from confirmed cloud mutations."""

import asyncio
import copy
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.openlist_sync import OpenListSync
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings


class SyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = sqlite3.connect(self.root / "ops.db")
        self.addCleanup(lambda: self.db.close())
        self.target = {
            "account": "aliyun",
            "drive_id": "drive",
            "folder_id": "root",
            "openlist_path": "/cloud",
        }
        self.settings = MediaSettings.parse(
            {"openlist_url": "http://localhost:5244", "targets": {"films": self.target}}
        )
        self.repo = MediaRepository(self.db, self.settings)

        async def destination(target, folder):
            return {**self.target, "openlist_path": "/cloud/" + folder}, []

        self.client = SimpleNamespace(post=AsyncMock(return_value={"content": [], "total": 0}))
        self.plugin = SimpleNamespace(
            root=self.root,
            settings=self.settings,
            repo=self.repo,
            openlist=self.client,
            target=lambda t: self.settings.targets[t],
            library=SimpleNamespace(destination=AsyncMock(side_effect=destination)),
        )
        self.sync = OpenListSync(self.plugin)

    def create(self, steps=None, **intent):
        value = {
            "target": "films",
            "destination": copy.deepcopy(self.target),
            "plan_id": uuid.uuid4().hex,
            "share_id": "share",
            "file_id": uuid.uuid4().hex,
            "local_destination": {},
            **intent,
        }
        if steps is not None:
            value["kind"] = "library"
        return self.repo.create(str(uuid.uuid4()), value, "assistant", initial={"steps": steps or []})

    def step(self, action="trash", source="old", dest="new", state="completed"):
        return {
            "action": action,
            "state": state,
            "before": {"parent_file_id": source},
            **({"destination": [dest, "video.mkv"]} if action != "trash" else {}),
        }

    def finish(self, item, steps):
        return self.repo.change(item["id"], "completed", steps=steps)

    def due_now(self):
        for (ident,) in self.db.execute("SELECT id FROM media_operations").fetchall():
            item = self.repo.get(ident)
            sync = item["data"].get("openlist_sync")
            if sync:
                sync["due"] = 0
                for e in sync["directories"].values():
                    e["due"] = 0
                self.repo.change(ident, item["state"], openlist_sync=sync)

    async def test_partial_batch_coalesces_across_operations_and_exposes_state(self):
        items = [self.create([self.step(state="pending")]) for _ in range(2)]
        self.repo.change(items[0]["id"], "checking", steps=[self.step(), self.step(state="pending")])
        self.repo.change(items[0]["id"], "cancelled")
        self.finish(items[1], [self.step() for _ in range(20)])
        self.due_now()
        self.assertEqual(await self.sync.run_once(), 1)
        self.client.post.assert_awaited_once()
        for item in items:
            public = self.repo.public(self.repo.get(item["id"]))
            self.assertEqual(public["openlist_sync"]["state"], "synced")
        self.assertEqual(self.repo.get(items[0]["id"])["state"], "cancelled")

    async def test_each_mutation_tracks_correct_parents(self):
        for action, expected in [
            ("trash", {"id:old"}),
            ("rename", {"id:old"}),
            ("move", {"id:old", "id:new"}),
            ("restore", {"id:old"}),
            ("mkdir", {"id:new"}),
        ]:
            step = self.step(action, dest="old" if action in {"rename", "restore"} else "new")
            if action == "mkdir":
                step["before"] = None
            item = self.create([step])
            result = self.finish(item, [step])
            self.assertEqual(set(result["data"]["openlist_sync"]["directories"]), expected)

    async def test_transfer_and_openlist_backend_but_not_local_or_download(self):
        item = self.create()
        result = self.repo.change(item["id"], "completed", file={"name": "video.mkv"})
        self.assertEqual(set(result["data"]["openlist_sync"]["directories"]), {"id:root"})
        step = {"action": "move", "state": "completed", "path": "a/movie.mkv", "destination": "b/movie.mkv"}
        result = self.finish(self.create([step], file_backend="openlist"), [step])
        self.assertEqual(set(result["data"]["openlist_sync"]["directories"]), {"path:a", "path:b"})
        for intent in [{"kind": "download"}, {"location": "local"}]:
            item = self.create(**intent)
            result = self.repo.change(item["id"], "completed", file={"name": "local.mkv"})
            self.assertNotIn("openlist_sync", result["data"])

    async def test_failure_retries_only_read_and_persists_over_restart(self):
        item = self.create([self.step()])
        self.finish(item, [self.step()])
        self.client.post.side_effect = MediaError("permission_denied")
        for _ in range(8):
            self.due_now()
            await self.sync.run_once()
        result = self.repo.get(item["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["data"]["openlist_sync"]["state"], "failed")
        self.assertEqual(self.client.post.await_count, 8)
        self.assertEqual(await self.sync.run_once(), 0)
        self.db.close()
        self.db = sqlite3.connect(self.root / "ops.db")
        self.repo = MediaRepository(self.db, self.settings)
        self.plugin.repo = self.repo
        self.sync = OpenListSync(self.plugin)
        self.client.post.side_effect = None
        self.sync.retry(item["id"])
        await self.sync.run_once()
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "synced")
        for call in self.client.post.await_args_list:
            self.assertEqual(call.args[0], "/api/fs/list")
            self.assertTrue(call.args[1]["refresh"])
            self.assertTrue(call.kwargs["scope"])

    async def test_new_change_during_refresh_cannot_be_marked_synced(self):
        item = self.create([self.step()])
        self.finish(item, [self.step()])
        self.due_now()
        entered, release = asyncio.Event(), asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"content": [], "total": 0}

        self.client.post.side_effect = wait
        task = asyncio.create_task(self.sync.run_once())
        await entered.wait()
        self.repo.change(item["id"], "completed", steps=[self.step(), self.step()])
        release.set()
        await task
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "pending")
        self.client.post.side_effect = None
        self.due_now()
        await self.sync.run_once()
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "synced")

    async def test_sync_and_manual_retry_preserve_partial_operation_backoff(self):
        item = self.create([self.step(state="pending")])
        due = time.time() + 300
        self.repo.change(
            item["id"],
            "verifying",
            due=due,
            steps=[self.step(), self.step(source="other", state="submitted")],
            submitted=True,
            attempts=3,
        )
        for error in (MediaError("permission_denied"), None):
            self.sync.retry(item["id"])
            self.assertEqual(self.repo.get(item["id"])["due"], due)
            self.client.post.side_effect = error
            await self.sync.run_once()
            current = self.repo.get(item["id"])
            self.assertEqual(current["due"], due)
            self.assertEqual(current["state"], "verifying")
            self.assertEqual(current["data"]["attempts"], 3)
            self.assertTrue(current["data"]["submitted"])
            self.assertIsNone(self.repo.due("library"))

    async def test_crash_before_refresh_and_transaction_atomicity(self):
        item = self.create([self.step()])
        self.db.execute(
            "CREATE TRIGGER reject_notice BEFORE INSERT ON media_outbox BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.finish(item, [self.step()])
        self.assertNotIn("openlist_sync", self.repo.get(item["id"])["data"])
        self.db.execute("DROP TRIGGER reject_notice")
        self.finish(item, [self.step()])
        self.db.close()
        self.db = sqlite3.connect(self.root / "ops.db")
        self.repo = MediaRepository(self.db, self.settings)
        self.plugin.repo = self.repo
        self.sync = OpenListSync(self.plugin)
        self.due_now()
        await self.sync.run_once()
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "synced")

    async def test_configuration_guard_and_invalid_response_do_not_claim_sync(self):
        item = self.create([self.step()])
        self.finish(item, [self.step()])
        self.due_now()
        self.settings.targets["films"]["folder_id"] = "different"
        await self.sync.run_once()
        self.client.post.assert_not_awaited()
        self.assertEqual(
            self.repo.get(item["id"])["data"]["openlist_sync"]["directories"]["id:old"]["error"],
            "target_configuration_changed",
        )
        self.settings.targets["films"]["folder_id"] = "root"
        self.client.post.return_value = {"total": 1, "content": []}
        self.due_now()
        await self.sync.run_once()
        self.assertEqual(
            self.repo.get(item["id"])["data"]["openlist_sync"]["directories"]["id:old"]["error"],
            "openlist_incomplete_listing",
        )

    async def test_failed_later_step_does_not_drop_already_completed_refresh(self):
        item = self.create([self.step(state="pending")])
        self.repo.change(
            item["id"], "checking", steps=[self.step(), self.step(source="other", state="submitted")]
        )
        self.repo.change(item["id"], "uncertain")
        self.due_now()
        await self.sync.run_once()
        self.assertEqual(self.repo.get(item["id"])["state"], "uncertain")
        self.client.post.assert_awaited_once()

    async def test_shutdown_mid_refresh_retains_pending_work(self):
        item = self.create([self.step()])
        self.finish(item, [self.step()])
        self.due_now()
        entered = asyncio.Event()

        async def hold(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.client.post.side_effect = hold
        task = asyncio.create_task(self.sync.run_once())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "pending")
        self.client.post.side_effect = None
        await self.sync.run_once()
        self.assertEqual(self.repo.get(item["id"])["data"]["openlist_sync"]["state"], "synced")

    async def test_transfer_visibility_stays_separate_and_does_not_force_second_refresh(self):
        item = self.create()
        self.repo.change(item["id"], "completed", file={"name": "video.mkv"})
        self.client.verify = AsyncMock(return_value={"state": "visible", "path_verified": True})
        self.due_now()
        await self.sync.run_once()
        self.client.post.assert_awaited_once()
        self.assertFalse(self.client.verify.await_args.kwargs["refresh"])
        result = self.repo.get(item["id"])
        self.assertEqual(result["data"]["openlist_sync"]["state"], "synced")
        self.assertEqual(result["data"]["openlist"]["state"], "visible")

    async def test_mapping_correction_uses_current_path_but_never_another_cloud_root(self):
        step = {"action": "rename", "state": "completed", "path": "movie.mkv", "destination": "new.mkv"}
        item = self.create([step], file_backend="openlist")
        self.finish(item, [step])
        self.settings.targets["films"]["openlist_path"] = "/corrected"
        self.due_now()
        await self.sync.run_once()
        self.assertEqual(self.client.post.await_args.args[1]["path"], "/corrected")
