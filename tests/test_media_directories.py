"""Whole-object writes, bounded evidence and recovery without replay (no cloud IO)."""

import copy
from datetime import datetime, timezone
import sqlite3
import time
import unittest
import uuid
from types import SimpleNamespace

import test_media_library as fixtures
from assistant_runtime.plugins.media.directories import snapshot, verify_copy
from assistant_runtime.plugins.media.directory_transfer import process, reconcile
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.library import Library
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings
from assistant_runtime.plugins.media.verification import recycle_evidence


class RecycleEvidenceTest(unittest.TestCase):
    def test_recycle_parent_is_not_original_parent_but_identity_remains_required(self):
        before = fixtures.VIDEO
        submitted_at = time.time()
        actual = {
            **before,
            "parent_file_id": "recycle-bin",
            "trashed_at": datetime.now(timezone.utc).isoformat(),
        }
        evidence = recycle_evidence(before, actual, parent_scope="recycle", submitted_at=submitted_at)
        self.assertNotIn("parent_file_id", evidence["fields"])
        for field, changed in (
            ("file_id", "wrong"),
            ("name", "wrong.mkv"),
            ("size", 1),
            ("content_hash", "b" * 40),
        ):
            with self.subTest(field=field), self.assertRaises(MediaError) as caught:
                recycle_evidence(
                    before, {**actual, field: changed}, parent_scope="recycle", submitted_at=submitted_at
                )
            self.assertEqual(caught.exception.field, field)
        with self.assertRaises(MediaError) as caught:
            recycle_evidence(before, actual)
        self.assertEqual(caught.exception.field, "parent_file_id")
        for timestamp in (None, "2020-01-01T00:00:00Z"):
            with self.assertRaises(MediaError):
                recycle_evidence(
                    before,
                    {**actual, "trashed_at": timestamp},
                    parent_scope="recycle",
                    submitted_at=submitted_at,
                )

    def test_missing_identity_and_unknown_parent_semantics_fail_closed(self):
        with self.assertRaises(MediaError):
            recycle_evidence(fixtures.VIDEO, {}, parent_scope="recycle")
        with self.assertRaises(MediaError):
            recycle_evidence(fixtures.VIDEO, fixtures.VIDEO, parent_scope="guess")


class DirectoryTrashTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.LibraryTest.asyncSetUp
    plan = fixtures.LibraryTest.plan

    def fill(self):
        self.provider.objects["video"]["parent_file_id"] = "season"
        self.provider.objects["subtitle"] = {
            **fixtures.VIDEO,
            "file_id": "subtitle",
            "parent_file_id": "season",
            "name": "1.srt",
        }

    async def test_whole_folder_is_one_write_with_frozen_children_and_restore(self):
        self.fill()
        item = await self.plan({"action": "trash", "folder_id": "season"})
        operation = self.repo.get(item["operation_id"])
        self.assertEqual(len(operation["data"]["steps"]), 1)
        self.assertEqual(operation["data"]["steps"][0]["manifest"]["count"], 2)
        await self.library.process(operation)
        self.assertEqual(self.provider.calls.count("trash"), 1)
        self.assertEqual(self.repo.get(operation["id"])["state"], "completed")
        restored = await self.plan({"action": "restore", "file_id": "season"})
        await self.library.process(self.repo.get(restored["operation_id"]))
        self.assertEqual(self.provider.calls.count("restore"), 1)
        self.assertEqual(self.repo.get(restored["operation_id"])["state"], "completed")

    async def test_root_folder_and_overlapping_children_are_rejected_before_write(self):
        self.fill()
        with self.assertRaises(ValueError):
            await self.plan({"action": "trash", "folder_id": "root-folder"})
        item = await self.plan({"action": "trash", "folder_id": "season"})
        with self.assertRaisesRegex(MediaError, "busy"):
            await self.plan({"action": "trash", "file_id": "video"})
        self.assertNotIn("trash", self.provider.calls)
        self.repo.change(item["operation_id"], "cancelled")
        with self.assertRaisesRegex(MediaError, "busy"):
            await self.plan(
                {"action": "trash", "file_id": "video"}, {"action": "trash", "folder_id": "season"}
            )

    async def test_folder_changed_after_preview_does_not_dispatch(self):
        self.fill()
        item = await self.plan({"action": "trash", "folder_id": "season"})
        self.provider.objects["video"]["size"] += 1
        with self.assertRaisesRegex(MediaError, "precondition"):
            await self.library.process(self.repo.get(item["operation_id"]))
        self.assertNotIn("trash", self.provider.calls)

    async def test_lost_write_reply_is_reconciled_without_executing_remaining_steps(self):
        self.fill()
        item = await self.plan(
            {"action": "trash", "folder_id": "season"},
            {"action": "mkdir", "folder_id": "root-folder", "name": "Later"},
        )
        self.provider.lost = "trash"
        with self.assertRaises(MediaError):
            await self.library.process(self.repo.get(item["operation_id"]))
        self.repo.change(item["operation_id"], "uncertain", error="provider_network_error")
        result = await self.library.reconcile(self.repo.get(item["operation_id"]))
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(result["error"], "unsubmitted_steps_remain")
        self.assertEqual(result["first_error"]["code"], "provider_network_error")
        self.assertEqual(self.provider.calls.count("trash"), 1)
        self.assertNotIn("mkdir", self.provider.calls)
        self.assertEqual(result["steps"][0]["state"], "completed")

    async def test_cancel_before_submission_and_recycle_mismatch_keep_evidence(self):
        item = await self.plan({"action": "trash", "folder_id": "season"})
        self.repo.change(item["operation_id"], "queued", cancel_requested=True)
        await self.library.process(self.repo.get(item["operation_id"]))
        self.assertEqual(self.repo.get(item["operation_id"])["state"], "cancelled")
        self.assertNotIn("trash", self.provider.calls)

    async def test_background_observation_is_bounded_and_keeps_backoff_and_original_error(self):
        item = await self.plan({"action": "trash", "folder_id": "season"})
        self.provider.lost = "trash"
        with self.assertRaises(MediaError):
            await self.library.process(self.repo.get(item["operation_id"]))
        self.repo.change(item["operation_id"], "uncertain", due=0, error="provider_network_error")
        for attempt in range(3):
            observation = self.repo.observation_due("library")
            self.assertIsNotNone(observation)
            self.repo.begin_observation(observation)
            # Simulate an unavailable read: another update must not erase backoff.
            before = self.repo.get(observation["id"])["due"]
            self.repo.change(observation["id"], "uncertain", error="provider_unavailable")
            self.assertEqual(self.repo.get(observation["id"])["due"], before)
            self.assertIsNone(self.repo.observation_due("library"))
            self.repo.change(observation["id"], "uncertain", due=0)
        self.assertIsNone(self.repo.observation_due("library"))
        self.assertEqual(
            self.repo.get(item["operation_id"])["data"]["first_error"]["code"], "provider_network_error"
        )
        self.assertEqual(self.provider.calls.count("trash"), 1)


class FolderProvider(fixtures.Provider):
    def __init__(self):
        super().__init__()
        self.source_root = {"file_id": "shared", "type": "folder", "name": "Show"}
        self.source_entries = [
            {**fixtures.VIDEO, "parent_file_id": "shared"},
            {"file_id": "extras", "parent_file_id": "shared", "name": "Extras", "type": "folder"},
            {**fixtures.VIDEO, "file_id": "sub", "parent_file_id": "extras", "name": "1.srt"},
        ]
        self.async_copy = False
        self.pending = False
        self.lost_copy = False
        self.objects = {}

    async def call(self, action, args):
        if action in {"source", "inspect", "capacity", "copy", "copy_status", "file"}:
            self.calls.append(action)
            if action == "source":
                return copy.deepcopy(self.source_root)
            if action == "inspect":
                return {
                    "items": copy.deepcopy(
                        [r for r in self.source_entries if r["parent_file_id"] == args["parent_file_id"]]
                    ),
                    "marker": None,
                }
            if action == "capacity":
                return {"total": 1000000, "used": 0}
            if action == "file":
                return copy.deepcopy(self.objects[args["file_id"]])
            if action == "copy_status":
                return {"state": "pending"} if self.pending else {"file_id": "saved", "state": "completed"}
            if action == "copy":
                self.objects["saved"] = {
                    **self.source_root,
                    "file_id": "saved",
                    "parent_file_id": "root-folder",
                }
                mapping = {
                    "shared": "saved",
                    **{r["file_id"]: "saved-" + r["file_id"] for r in self.source_entries},
                }
                for row in self.source_entries:
                    self.objects[mapping[row["file_id"]]] = {
                        **row,
                        "file_id": mapping[row["file_id"]],
                        "parent_file_id": mapping[row["parent_file_id"]],
                    }
                if self.lost_copy:
                    raise MediaError("provider_network_error", uncertain=True)
                return {"task_id": "copy-task"} if self.async_copy else {"file_id": "saved"}
        return await super().call(action, args)


class DirectoryTransferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.repo = MediaRepository(self.db, MediaSettings(targets={"series": fixtures.ROOT}))
        self.provider = FolderProvider()
        self.plugin = SimpleNamespace(
            repo=self.repo,
            settings=self.repo.settings,
            provider=self.provider,
            target=lambda _: fixtures.ROOT,
        )
        self.plugin.library = Library(self.plugin)
        self.intent = {
            "kind": "directory",
            "share_id": "share",
            "password": "private",
            "file_id": "shared",
            "target": "series",
            "destination": fixtures.ROOT,
        }

    def create(self):
        return self.repo.create(str(uuid.uuid4()), self.intent, "assistant")

    async def test_nested_folder_and_subtitle_copy_once_with_destination_id_and_manifest(self):
        item = self.create()
        await process(self.plugin, item)
        result = self.repo.get(item["id"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["data"]["cloud_verification"]["entries"], 3)
        self.assertEqual(self.provider.calls.count("copy"), 1)
        await process(self.plugin, result)
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_async_copy_cancel_after_dispatch_only_observes_receipt(self):
        self.provider.async_copy = self.provider.pending = True
        item = self.create()
        await process(self.plugin, item)
        self.repo.change(item["id"], "verifying", cancel_requested=True)
        self.provider.pending = False
        await process(self.plugin, self.repo.get(item["id"]))
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_lost_response_restart_does_not_guess_by_name_or_replay_copy(self):
        self.provider.lost_copy = True
        item = self.create()
        with self.assertRaises(MediaError):
            await process(self.plugin, item)
        self.repo.recover()
        await process(self.plugin, self.repo.get(item["id"]))
        result = self.repo.get(item["id"])
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(result["data"]["error"], "directory_copy_receipt_unconfirmed")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_read_only_reconciliation_recovers_receipt_without_a_second_copy(self):
        self.provider.async_copy = self.provider.pending = True
        item = self.create()
        await process(self.plugin, item)
        self.repo.change(item["id"], "uncertain", error="provider_network_error")
        self.provider.pending = False
        result = await reconcile(self.plugin, self.repo.get(item["id"]))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.provider.calls.count("copy"), 1)
        self.repo.change(item["id"], "failed")
        unsubmitted = self.create()
        await reconcile(self.plugin, unsubmitted)
        self.assertEqual(self.repo.get(unsubmitted["id"])["state"], "queued")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_incomplete_target_is_not_success_and_cancel_before_dispatch_has_no_write(self):
        self.provider.async_copy = self.provider.pending = True
        item = self.create()
        await process(self.plugin, item)
        self.provider.pending = False
        del self.provider.objects["saved-sub"]
        with self.assertRaisesRegex(MediaError, "directory_copy_unconfirmed"):
            await process(self.plugin, self.repo.get(item["id"]))
        self.assertEqual(self.provider.calls.count("copy"), 1)
        self.repo.change(item["id"], "failed")
        item = self.create()
        self.repo.change(item["id"], "queued", cancel_requested=True)
        await process(self.plugin, self.repo.get(item["id"]))
        self.assertEqual(self.repo.get(item["id"])["state"], "cancelled")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_snapshot_rejects_cycles_duplicate_paths_and_overflow(self):
        root = self.provider.source_root
        for extra, limit in (
            ({**self.provider.source_entries[0], "file_id": "shared"}, 100),
            ({**self.provider.source_entries[0], "file_id": "duplicate"}, 100),
            (None, 1),
        ):
            with self.subTest(extra=extra, limit=limit):
                original = copy.deepcopy(self.provider.source_entries)
                if extra:
                    self.provider.source_entries.append(extra)
                with self.assertRaises(MediaError):
                    await snapshot(
                        self.provider, root, fixtures.ROOT, share={"share_id": "share"}, limit=limit
                    )
                self.provider.source_entries = original

    async def test_manifest_mismatched_hash_and_identity_are_not_relaxed(self):
        before = await snapshot(
            self.provider, self.provider.source_root, fixtures.ROOT, share={"share_id": "share"}
        )
        after = copy.deepcopy(before)
        after["entries"][0]["content_hash"] = "wrong"
        with self.assertRaises(MediaError):
            verify_copy(before, after)
