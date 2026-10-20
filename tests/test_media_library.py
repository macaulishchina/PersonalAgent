"""Library change/recovery tests with protocol fixtures and real local filesystem IO."""

import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from assistant_runtime.kernel.contracts import MediaRequest
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings
from assistant_runtime.plugins.media.library import Library
from assistant_runtime.plugins.media.local_library import LocalLibrary, local_call

ROOT = {"drive_id": "drive", "folder_id": "root-folder", "openlist_path": "/cloud"}
VIDEO = {
    "file_id": "video",
    "parent_file_id": "root-folder",
    "name": "1.mkv",
    "type": "file",
    "category": "video",
    "size": 123,
    "content_hash": "a" * 40,
    "content_hash_name": "sha1",
}


class Provider:
    def __init__(self):
        self.objects = {
            "video": dict(VIDEO),
            "season": {
                "file_id": "season",
                "parent_file_id": "root-folder",
                "name": "Season 01",
                "type": "folder",
            },
        }
        self.trash = {}
        self.calls = []
        self.lost = None

    async def call(self, action, args):
        self.calls.append(action)
        fid = args.get("file_id")
        if action == "locate":
            if fid not in self.objects:
                raise MediaError("file_not_found")
            item = self.objects[fid]
            parts = [item["name"]]
            parent = item["parent_file_id"]
            while parent != "root-folder":
                if parent not in self.objects:
                    raise MediaError("file_outside_configured_target")
                parts.insert(0, self.objects[parent]["name"])
                parent = self.objects[parent]["parent_file_id"]
            return {"file": copy.deepcopy(item), "relative_parts": parts}
        if action == "list":
            return {
                "items": [
                    copy.deepcopy(v)
                    for v in self.objects.values()
                    if v["parent_file_id"] == args["folder_id"]
                ],
                "marker": None,
            }
        if action == "trash_list":
            return {"items": list(self.trash.values()), "marker": None}
        if action == "rename":
            self.objects[fid]["name"] = args["name"]
        elif action == "move":
            self.objects[fid]["parent_file_id"] = args["to_folder_id"]
        elif action == "trash":
            self.trash[fid] = self.objects.pop(fid)
        elif action == "restore":
            self.objects[fid] = self.trash.pop(fid)
        elif action == "mkdir":
            fid = uuid.uuid4().hex
            self.objects[fid] = {
                "file_id": fid,
                "parent_file_id": args["folder_id"],
                "name": args["name"],
                "type": "folder",
            }
        else:
            raise AssertionError(action)
        if self.lost == action:
            self.lost = None
            raise MediaError("provider_network_error", uncertain=True)
        return {"file_id": fid}


class LibraryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.provider = Provider()
        self.settings = MediaSettings(targets={"series": ROOT})
        self.repo = MediaRepository(self.db, self.settings)
        self.plugin = SimpleNamespace(
            repo=self.repo, settings=self.settings, provider=self.provider, target=lambda _: ROOT
        )
        self.library = Library(self.plugin)

    async def plan(self, *changes):
        preview = await self.library.preview({"target": "series", "changes": list(changes)}, "assistant")
        return self.library.apply(
            {"plan_id": preview["plan_id"], "request_id": str(uuid.uuid4())}, "assistant", None
        )

    async def test_rename_lost_reply_reconciles_once_and_preserves_asset_identity(self):
        original = await self.library.listing("series")
        asset = next(i["asset_id"] for i in original["items"] if i["file"]["file_id"] == "video")
        item = await self.plan({"action": "rename", "file_id": "video", "name": "Series.S01E01.mkv"})
        self.provider.lost = "rename"
        with self.assertRaises(MediaError):
            await self.library.process(self.repo.get(item["operation_id"]))
        self.repo.recover()
        await self.library.process(self.repo.get(item["operation_id"]))
        self.assertEqual(self.provider.calls.count("rename"), 1)
        current = await self.library.listing("series")
        self.assertEqual(
            next(i["asset_id"] for i in current["items"] if i["file"]["file_id"] == "video"), asset
        )
        self.assertEqual(self.repo.get(item["operation_id"])["state"], "completed")

    async def test_changed_precondition_conflict_and_root_escape_do_not_write(self):
        item = await self.plan({"action": "rename", "file_id": "video", "name": "safe.mkv"})
        self.provider.objects["video"]["name"] = "external.mkv"
        with self.assertRaisesRegex(MediaError, "precondition"):
            await self.library.process(self.repo.get(item["operation_id"]))
        self.assertNotIn("rename", self.provider.calls)
        for change in (
            {"action": "rename", "file_id": "video", "name": "../escape.mkv"},
            {"action": "move", "file_id": "video", "folder_id": "outside"},
            {"action": "trash", "file_id": "root-folder"},
        ):
            with self.assertRaises((ValueError, MediaError)):
                await self.plan(change)

    async def test_trash_restore_original_parent_and_conflict(self):
        item = await self.plan({"action": "move", "file_id": "video", "folder_id": "season"})
        await self.library.process(self.repo.get(item["operation_id"]))
        item = await self.plan({"action": "trash", "file_id": "video"})
        await self.library.process(self.repo.get(item["operation_id"]))
        self.provider.objects["collision"] = {**VIDEO, "file_id": "collision", "parent_file_id": "season"}
        with self.assertRaisesRegex(MediaError, "conflict"):
            await self.plan({"action": "restore", "file_id": "video"})
        del self.provider.objects["collision"]
        item = await self.plan({"action": "restore", "file_id": "video"})
        await self.library.process(self.repo.get(item["operation_id"]))
        self.assertEqual(self.provider.objects["video"]["parent_file_id"], "season")

    async def test_cancel_after_first_step_never_repeats_completed_work(self):
        self.provider.objects["two"] = {**VIDEO, "file_id": "two", "name": "2.mkv"}
        item = await self.plan(
            {"action": "rename", "file_id": "video", "name": "one.mkv"},
            {"action": "rename", "file_id": "two", "name": "two.mkv"},
        )
        original = self.provider.call

        async def call(action, args):
            result = await original(action, args)
            if action == "rename":
                now = self.repo.get(item["operation_id"])
                self.repo.change(now["id"], now["state"], cancel_requested=True)
            return result

        self.provider.call = call
        await self.library.process(self.repo.get(item["operation_id"]))
        saved = self.repo.get(item["operation_id"])
        self.assertEqual(saved["state"], "cancelled")
        self.assertEqual(saved["data"]["steps"][0]["state"], "completed")
        self.assertEqual(self.provider.calls.count("rename"), 1)

    async def test_download_reservation_prevents_mutation(self):
        self.repo.create(
            str(uuid.uuid4()),
            {
                "kind": "download",
                "file_id": "video",
                "target": "series",
                "destination": ROOT,
                "local_destination": {},
            },
            "assistant",
        )
        with self.assertRaisesRegex(MediaError, "busy"):
            await self.plan({"action": "trash", "file_id": "video"})

    async def test_plan_ownership_uuid_and_collection_completion(self):
        p = await self.library.preview(
            {"target": "series", "changes": [{"action": "mkdir", "folder_id": "root-folder", "name": "new"}]},
            "task:one",
        )
        with self.assertRaisesRegex(ValueError, "owned"):
            self.library.apply({"plan_id": p["plan_id"], "request_id": str(uuid.uuid4())}, "task:two", "two")
        definition = {
            "title": "Series",
            "season": 1,
            "total_episodes": 2,
            "evidence": "User explicitly requested episodes 1..2",
            "required_state": "cloud",
        }
        args = {"collection": "show", "target": "series", "revision": 0, "definition": definition}
        self.library.collection(args)
        self.library.remember("series", VIDEO, collection="show", season=1, episode=1, trashed=False)
        self.assertEqual(self.library.collection({"collection": "show"})["missing"], [2])
        self.library.remember(
            "series", {**VIDEO, "file_id": "two"}, collection="show", season=1, episode=2, trashed=False
        )
        self.assertTrue(self.library.collection({"collection": "show"})["completion_candidate"])
        self.library.remember("series", VIDEO, trashed=True)
        self.assertFalse(self.library.collection({"collection": "show"})["completion_candidate"])


class LocalLibraryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await LibraryTest.asyncSetUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        (self.mount / "library").mkdir()
        (self.mount / "stage").mkdir()
        self.patch = patch.object(Path, "is_mount", lambda path: path == self.mount)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.binding = {
            "path": str(self.mount / "library"),
            "staging_path": str(self.mount / "stage"),
            "mount_path": str(self.mount),
        }
        self.settings.local_targets = {"series": self.binding}
        self.local = LocalLibrary(self.library)

    async def local_plan(self, change):
        p = await self.library.preview(
            {"location": "local", "target": "series", "changes": [change]}, "assistant"
        )
        return self.library.apply(
            {"plan_id": p["plan_id"], "request_id": str(uuid.uuid4())}, "assistant", None
        )

    async def test_real_local_trash_restore_and_conflict(self):
        path = self.mount / "library" / "video.mkv"
        path.write_bytes(b"content")
        original = local_call(self.binding, "info", "video.mkv")
        op = await self.local_plan({"action": "trash", "path": "video.mkv"})
        await self.library.process(self.repo.get(op["operation_id"]))
        self.assertFalse(path.exists())
        self.assertEqual(
            (self.mount / "stage" / "library-trash" / (original["file_id"] + ".mkv")).read_bytes(), b"content"
        )
        path.write_bytes(b"other")
        with self.assertRaisesRegex(MediaError, "conflict"):
            await self.local_plan({"action": "restore", "file_id": original["file_id"]})
        path.unlink()
        op = await self.local_plan({"action": "restore", "file_id": original["file_id"]})
        await self.library.process(self.repo.get(op["operation_id"]))
        self.assertEqual(path.read_bytes(), b"content")

    async def test_local_link_crash_reconciliation_and_symlink_rejection(self):
        path = self.mount / "library" / "video.mkv"
        path.write_bytes(b"content")
        op = await self.local_plan({"action": "rename", "path": "video.mkv", "name": "renamed.mkv"})
        item = self.repo.get(op["operation_id"])
        step = item["data"]["steps"][0]
        import os

        os.link(path, path.with_name("renamed.mkv"))
        step["state"] = "submitted"
        self.repo.change(item["id"], "submitting", steps=[step], submitted=True)
        await self.library.process(self.repo.get(item["id"]))
        self.assertFalse(path.exists())
        self.assertEqual(path.with_name("renamed.mkv").read_bytes(), b"content")
        (self.mount / "library" / "link").symlink_to(self.mount / "stage", target_is_directory=True)
        with self.assertRaises((OSError, MediaError)):
            local_call(self.binding, "listing", "link", "")


class BatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_media as fixtures

        await fixtures.MediaEngineTest.asyncSetUp(self)
        # Workers are stopped only in this test; tick explicitly exercises the
        # durable submission boundary before/after a simulated caller disconnect.
        workers = list(self.runtime.contexts["media"].tasks)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()

    async def test_batch_retry_no_duplicate_copy_and_one_summary(self):
        args = {
            "request_id": str(uuid.uuid4()),
            "target": "movies",
            "action": "transfer",
            "items": [{"share_url": "https://www.alipan.com/s/testshare", "file_id": "video"}],
        }
        first = await self.plugin.request(MediaRequest("batch", args))
        again = await self.plugin.request(MediaRequest("batch", args))
        self.assertEqual(first, again)
        ident = first["batch_id"]

        def data():
            return json.loads(
                self.store.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()[0]
            )

        await self.plugin.batches.tick(ident, "assistant", data())
        op = self.plugin.batches.get(ident)["items"][0]["operation_id"]
        await self.plugin.process(self.plugin.repo.get(op))
        await self.plugin.batches.tick(ident, "assistant", data())
        self.assertEqual(self.plugin.batches.get(ident)["state"], "completed")
        self.assertEqual(self.provider.calls.count("copy"), 1)
        notices = self.store.db.execute("SELECT key FROM media_outbox").fetchall()
        self.assertEqual(notices, [("media-batch:" + ident + ":completed",)])

    async def test_background_scope_and_cancel_before_submission(self):
        args = {
            "request_id": str(uuid.uuid4()),
            "target": "movies",
            "action": "transfer",
            "items": [{"share_url": "https://www.alipan.com/s/testshare", "file_id": "video"}],
        }
        self.dependencies.runs["run"] = {"state": "running", "task_id": "task", "resources": []}
        with self.assertRaisesRegex(ValueError, "resource"):
            await self.plugin.request(MediaRequest("batch", args, "run"))
        result = await self.plugin.request(MediaRequest("batch", args))
        ident = result["batch_id"]
        self.plugin.batches.cancel(ident, "assistant")
        data = json.loads(
            self.store.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()[0]
        )
        await self.plugin.batches.tick(ident, "assistant", data)
        self.assertEqual(self.plugin.batches.get(ident)["state"], "cancelled")
        self.assertNotIn("copy", self.provider.calls)

    async def test_cancelling_reused_operation_detaches_without_cancelling_original(self):
        entry = {"share_url": "https://www.alipan.com/s/testshare", "file_id": "video"}
        original = await self.plugin.request(
            MediaRequest("transfer", {**entry, "target": "movies", "request_id": str(uuid.uuid4())})
        )
        result = await self.plugin.request(
            MediaRequest(
                "batch",
                {"request_id": str(uuid.uuid4()), "target": "movies", "action": "transfer", "items": [entry]},
            )
        )
        ident = result["batch_id"]

        def data():
            return json.loads(
                self.store.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()[0]
            )

        await self.plugin.batches.tick(ident, "assistant", data())
        self.plugin.batches.cancel(ident, "assistant")
        await self.plugin.batches.tick(ident, "assistant", data())
        self.assertEqual(self.plugin.batches.get(ident)["state"], "cancelled")
        self.assertEqual(self.plugin.repo.get(original["operation_id"])["state"], "queued")

    async def test_queue_backpressure_retains_pending_batch_items(self):
        self.plugin.settings.queue_capacity = 1
        await self.plugin.request(
            MediaRequest(
                "transfer",
                {
                    "share_url": "https://www.alipan.com/s/first",
                    "file_id": "first",
                    "target": "movies",
                    "request_id": str(uuid.uuid4()),
                },
            )
        )
        result = await self.plugin.request(
            MediaRequest(
                "batch",
                {
                    "request_id": str(uuid.uuid4()),
                    "target": "movies",
                    "action": "transfer",
                    "items": [{"share_url": "https://www.alipan.com/s/second", "file_id": "second"}],
                },
            )
        )
        ident = result["batch_id"]
        data = json.loads(
            self.store.db.execute("SELECT data FROM media_batches WHERE id=?", (ident,)).fetchone()[0]
        )
        await self.plugin.batches.tick(ident, "assistant", data)
        result = self.plugin.batches.get(ident)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["items"][0]["state"], "pending")
