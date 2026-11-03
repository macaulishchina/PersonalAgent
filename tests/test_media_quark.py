"""Quark protocol fixtures and account-isolated acquisition lifecycle checks."""

from platform_support import private_mode

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from aiohttp import web

from assistant_runtime.api.contracts import MediaRequest
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.private import save
from assistant_runtime.plugins.media.quark import Quark
from assistant_runtime.plugins.media.settings import MediaSettings
import test_media as fixtures


class QuarkProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.states = []
        self.adapter = Quark(Path(self.tmp.name), self.states.append, account="quark")
        self.addAsyncCleanup(self.adapter.close)
        save(self.adapter.credentials / "account.json", {"cookie": "__uid=user1; __pus=secret"})
        self.calls = []
        self.auth_fail = False
        self.incomplete = False
        self.lose_copy = False
        self.delay_copy = False
        self.download_row = {}
        self.download_reads = 0
        self.recycled = []
        self.recycle_total = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.row = {
            "fid": "video",
            "file_name": "episode.mp4",
            "size": 1024,
            "pdir_fid": "season",
            "category": 1,
            "file_type": 1,
            "share_fid_token": "private-file-token",
        }
        self.share_rows = None
        self.share_queries = []
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        patched = patch("assistant_runtime.plugins.media.quark.BASE", f"http://127.0.0.1:{port}")
        patched.start()
        self.addCleanup(patched.stop)

    async def handle(self, request):
        self.calls.append(request.path)
        if self.auth_fail:
            return web.json_response({"code": 401, "message": "private-upstream-message"})
        if request.path == "/member":
            response = web.json_response({"code": 0, "data": {"total_capacity": 4096, "use_capacity": 0}})
            response.set_cookie("__puus", "rotated-secret")
            return response
        if request.path == "/share/sharepage/token":
            return web.json_response({"code": 0, "data": {"stoken": "private-share-token"}})
        if request.path in {"/share/sharepage/detail", "/file/sort"}:
            rows = [self.row]
            if request.path == "/share/sharepage/detail":
                self.share_queries.append(dict(request.query))
                if self.share_rows is not None:
                    rows = self.share_rows
            return web.json_response(
                {
                    "code": 0,
                    "data": {"list": rows},
                    "metadata": {"_total": 9 if self.incomplete else len(rows)},
                }
            )
        if request.path == "/file/info":
            fid = request.query["fid"]
            if fid == "video" and self.recycled:
                return web.json_response({"code": 21001}, status=404)
            row = {**self.row, "fid": fid}
            if fid == "season":
                row.update(pdir_fid="elsewhere", dir=True)
            if fid == "elsewhere":
                row.update(pdir_fid="0", dir=True)
            return web.json_response({"code": 0, "data": row})
        if request.path == "/file/recycle/list":
            self.assertEqual(request.method, "GET")
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "list": self.recycled,
                        "total": len(self.recycled) if self.recycle_total is None else self.recycle_total,
                    },
                }
            )
        if request.path in {"/file/delete", "/file/recycle/recover"}:
            body = await request.json()
            if request.path == "/file/delete":
                self.assertEqual(body, {"action_type": 2, "filelist": ["video"], "exclude_fids": []})
                self.recycled = [{**self.row, "record_id": "recycle-1"}]
            else:
                self.assertEqual(body, {"select_mode": 2, "record_list": ["recycle-1"]})
                self.recycled = []
            if self.lose_copy:
                request.transport.close()
                return web.Response()
            return web.json_response({"code": 0, "data": {"task_id": "recycle-task", "finish": False}})
        if request.path == "/file/download":
            self.assertEqual((await request.json())["fids"], ["video"])
            self.download_reads += 1
            return web.json_response(
                {
                    "code": 0,
                    "data": [
                        {
                            **self.row,
                            "md5": "A" * 32,
                            "download_url": "https://dl-pc-zb.drive.quark.cn/original?signed=private",
                            **self.download_row,
                        }
                    ],
                }
            )
        if request.path in {"/file/rename", "/file/move"}:
            body = await request.json()
            if request.path.endswith("rename"):
                self.assertEqual(body["fid"], "video")
                self.row["file_name"] = body["file_name"]
            else:
                self.assertEqual(body["filelist"], ["video"])
                self.assertEqual(body["action_type"], 1)
                self.row["pdir_fid"] = body["to_pdir_fid"]
            if self.lose_copy:
                request.transport.close()
                return web.Response()
            return web.json_response(
                {
                    "code": 0,
                    "data": {}
                    if request.path.endswith("rename")
                    else {"task_id": "move-task", "finish": True},
                }
            )
        if request.path == "/file" and request.method == "POST":
            body = await request.json()
            self.assertEqual(body["pdir_fid"], "destination")
            self.assertEqual(body["file_name"], "Season 01")
            if self.lose_copy:
                request.transport.close()
                return web.Response()
            return web.json_response({"code": 0, "data": {"fid": "new-folder"}})
        if request.path == "/share/sharepage/save":
            body = await request.json()
            self.assertEqual(body["fid_token_list"], ["private-file-token"])
            self.assertEqual(body["pdir_fid"], "season")
            if self.lose_copy:
                request.transport.close()
                return web.Response()
            if self.delay_copy:
                self.entered.set()
                await self.release.wait()
            return web.json_response({"code": 0, "data": {"task_id": "copy-task"}})
        if request.path == "/task":
            return web.json_response(
                {"code": 0, "data": {"status": 2, "save_as": {"save_as_top_fids": ["saved"]}}}
            )
        return web.Response(status=404)

    def source_args(self):
        return {
            "share_id": "share123",
            "password": "",
            "parent_file_id": "season",
            "file_id": "video",
            "folder_id": "destination",
        }

    async def test_share_virtual_root_retains_original_parent_for_verified_copy(self):
        args = {**self.source_args(), "parent_file_id": "root"}
        result = await self.adapter.call("inspect", args)
        self.assertEqual(result["items"][0]["parent_file_id"], "root")
        self.assertEqual(self.share_queries[-1]["pdir_fid"], "0")
        self.assertEqual(self.share_queries[-1]["pwd_id"], args["share_id"])
        self.assertEqual(self.share_queries[-1]["stoken"], "private-share-token")
        source = await self.adapter.call("source", args)
        self.assertEqual(source["parent_file_id"], "root")
        self.assertNotIn("private", json.dumps(source))
        # The request handler verifies that copy uses original pdir=season and
        # the share's file token, after rereading membership under that token.
        receipt = await self.adapter.call("copy", args)
        self.assertEqual(receipt["task_id"], "copy-task")
        self.assertEqual(self.calls.count("/share/sharepage/save"), 1)
        with self.assertRaisesRegex(MediaError, "source_file_not_found"):
            await self.adapter.call("copy", {**args, "file_id": "outside-share"})
        self.assertEqual(self.calls.count("/share/sharepage/save"), 1)

    async def test_share_root_exception_does_not_relax_cloud_or_child_identity(self):
        # Ordinary cloud root must still consist of actual children of cloud root.
        with self.assertRaisesRegex(MediaError, "provider_file_identity_mismatch"):
            await self.adapter.call("list", {"folder_id": "root"})
        with self.assertRaisesRegex(MediaError, "provider_file_identity_mismatch"):
            await self.adapter.call("inspect", {**self.source_args(), "parent_file_id": "other-folder"})
        for rows in (
            [self.row, dict(self.row)],
            [{**self.row, "pdir_fid": "video"}],
            [{k: v for k, v in self.row.items() if k != "pdir_fid"}],
        ):
            self.share_rows = rows
            with (
                self.subTest(rows=rows),
                self.assertRaisesRegex(MediaError, "provider_file_identity_mismatch"),
            ):
                await self.adapter.call("inspect", {**self.source_args(), "parent_file_id": "root"})
        self.assertNotIn("/share/sharepage/save", self.calls)

    async def test_share_root_can_contain_items_from_distinct_original_parents(self):
        self.share_rows = [self.row, {**self.row, "fid": "second", "pdir_fid": "another-original-parent"}]
        result = await self.adapter.call("inspect", {**self.source_args(), "parent_file_id": "root"})
        self.assertEqual([item["parent_file_id"] for item in result["items"]], ["root", "root"])
        self.assertEqual([row["pdir_fid"] for row in self.share_rows], ["season", "another-original-parent"])

    async def test_recycle_record_roundtrip_and_lost_ack_never_replays(self):
        before = await self.adapter.call("file", {"file_id": "video", "folder_id": "season"})
        self.lose_copy = True
        with self.assertRaises(MediaError) as caught:
            await self.adapter.call("trash", {"file_id": "video"})
        self.assertTrue(caught.exception.uncertain)
        with self.assertRaisesRegex(MediaError, "file_not_found"):
            await self.adapter.call("file", {"file_id": "video", "folder_id": "season"})
        entry = (await self.adapter.call("trash_list"))["items"][0]
        self.assertEqual(entry["recycle_id"], "recycle-1")
        self.assertNotIn("share_fid_token", entry)
        with self.assertRaises(MediaError) as caught:
            await self.adapter.call(
                "restore",
                {
                    "file_id": "video",
                    "recycle_id": entry["recycle_id"],
                    "expected_file": before,
                },
            )
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(await self.adapter.call("file", {"file_id": "video", "folder_id": "season"}), before)
        self.assertEqual(self.calls.count("/file/delete"), 1)
        self.assertEqual(self.calls.count("/file/recycle/recover"), 1)

    async def test_restore_rejects_stale_record_wrong_metadata_and_incomplete_listing(self):
        before = await self.adapter.call("file", {"file_id": "video", "folder_id": "season"})
        await self.adapter.call("trash", {"file_id": "video"})
        args = {"file_id": "video", "recycle_id": "recycle-1", "expected_file": before}
        for overrides in [{"recycle_id": "older-record"}, {"expected_file": {**before, "size": 1}}]:
            with self.assertRaisesRegex(MediaError, "recycle_record_changed"):
                await self.adapter.call("restore", {**args, **overrides})
        self.recycle_total = 9
        with self.assertRaisesRegex(MediaError, "provider_incomplete_listing"):
            await self.adapter.call("restore", args)
        self.recycle_total = None
        self.recycled = []
        with self.assertRaisesRegex(MediaError, "recycle_record_missing"):
            await self.adapter.call("restore", args)
        with self.assertRaisesRegex(MediaError, "recycle_record_missing"):
            await self.adapter.call("restore", {**args, "recycle_id": None})
        self.assertNotIn("/file/recycle/recover", self.calls)
        with self.assertRaisesRegex(MediaError, "provider_action_unsupported"):
            await self.adapter.request("/file/recycle/remove", method="POST", body={})

    async def test_original_download_returns_md5_and_origin_bound_private_headers(self):
        result = await self.adapter.call("download_url", {"file_id": "video", "folder_id": "season"})
        self.assertEqual(result["file"]["content_hash_name"], "md5")
        self.assertEqual(result["file"]["content_hash"], "a" * 32)
        self.assertEqual(result["credential_origin"], "https://dl-pc-zb.drive.quark.cn")
        self.assertIn("rotated-secret", result["headers"]["Cookie"])
        self.assertNotIn("Cookie", json.dumps(result["file"]))

    async def test_download_rejects_wrong_identity_missing_hash_and_foreign_cookie_origin(self):
        for overrides in [
            {"fid": "other"},
            {"size": 1025},
            {"md5": ""},
            {"md5": "etag-2"},
            {"download_url": "https://dl-pc-zb.drive.quark.cn.evil.invalid/video"},
            {"download_url": "https://evil.invalid/video"},
            {"download_url": "http://dl-pc-zb.drive.quark.cn/video"},
            {"download_url": "https://user@dl-pc-zb.drive.quark.cn/video"},
        ]:
            self.download_row = overrides
            with self.subTest(overrides=overrides), self.assertRaises(MediaError):
                await self.adapter.call("download_url", {"file_id": "video", "folder_id": "season"})
        self.download_reads = 0
        with self.assertRaisesRegex(MediaError, "outside_configured_target"):
            await self.adapter.call("download_url", {"file_id": "video", "folder_id": "outside"})
        self.assertEqual(self.download_reads, 0)

    async def test_rename_move_and_lost_write_reply_require_observation_not_replay(self):
        result = await self.adapter.call("rename", {"file_id": "video", "name": "renamed.mp4"})
        self.assertEqual(result, {"file_id": "video"})
        result = await self.adapter.call("move", {"file_id": "video", "to_folder_id": "destination"})
        self.assertEqual(result["async_task_id"], "move-task")
        self.lose_copy = True
        with self.assertRaises(MediaError) as caught:
            await self.adapter.call("rename", {"file_id": "video", "name": "lost.mp4"})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.calls.count("/file/rename"), 2)
        self.assertEqual(
            (await self.adapter.call("file", {"file_id": "video", "folder_id": "destination"}))["name"],
            "lost.mp4",
        )
        with self.assertRaises(MediaError) as caught:
            await self.adapter.call("move", {"file_id": "video", "to_folder_id": "season"})
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.calls.count("/file/move"), 2)
        self.assertEqual(
            (await self.adapter.call("file", {"file_id": "video", "folder_id": "season"}))["name"],
            "lost.mp4",
        )

    async def test_mkdir_receipt_and_lost_reply_are_not_retried(self):
        args = {"folder_id": "destination", "name": "Season 01"}
        result = await self.adapter.call("mkdir", args)
        self.assertEqual(result, {"file_id": "new-folder"})
        self.lose_copy = True
        with self.assertRaises(MediaError) as caught:
            await self.adapter.call("mkdir", args)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.calls.count("/file"), 2)

    async def test_inspect_sanitizes_tokens_and_saves_rotated_cookie_privately(self):
        result = await self.adapter.call("inspect", self.source_args())
        self.assertEqual(result["items"][0]["parent_file_id"], "season")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        path = self.adapter.credentials / "account.json"
        self.assertIn("rotated-secret", json.loads(path.read_text())["cookie"])
        self.assertEqual(private_mode(path), 0o600)
        self.assertEqual(self.states[-1], "ready")

    async def test_async_copy_receipt_has_no_tokens_and_query_does_not_copy(self):
        receipt = await self.adapter.call("copy", self.source_args())
        self.assertEqual(receipt, {"task_id": "copy-task"})
        result = await self.adapter.call("copy_status", receipt)
        self.assertEqual(result["file_id"], "saved")
        self.assertEqual(self.calls.count("/share/sharepage/save"), 1)

    async def test_lost_ack_is_uncertain_and_no_automatic_write_retry(self):
        self.lose_copy = True
        with self.assertRaises(MediaError) as error:
            await self.adapter.call("copy", self.source_args())
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(self.calls.count("/share/sharepage/save"), 1)

    async def test_auth_and_account_switch_never_reach_copy(self):
        await self.adapter.call("status")
        save(self.adapter.credentials / "account.json", {"cookie": "__uid=another; __pus=other"})
        with self.assertRaisesRegex(MediaError, "account_mismatch"):
            await self.adapter.call("copy", self.source_args())
        self.assertNotIn("/share/sharepage/save", self.calls)
        save(self.adapter.credentials / "account.json", {"cookie": "__uid=user1; __pus=other"})
        self.auth_fail = True
        with self.assertRaisesRegex(MediaError, "auth_required"):
            await self.adapter.call("status")
        self.assertEqual(self.states[-1], "auth_required")
        self.auth_fail = False
        self.assertEqual((await self.adapter.call("status"))["state"], "ready")

    async def test_incomplete_snapshot_never_proves_absence(self):
        self.incomplete = True
        with self.assertRaisesRegex(MediaError, "provider_incomplete_listing"):
            await self.adapter.call("source", self.source_args())
        self.assertNotIn("/share/sharepage/save", self.calls)

    async def test_outside_root_rejected_and_lease_is_exclusive(self):
        with self.assertRaisesRegex(MediaError, "file_outside_configured_target"):
            await self.adapter.call("locate", {"file_id": "video", "folder_id": "allowed"})
        other = Quark(Path(self.tmp.name), lambda _: None, account="quark")
        self.addAsyncCleanup(other.close)
        with self.assertRaisesRegex(MediaError, "already_owned"):
            other.claim()

    async def test_cancel_during_submission_releases_lock_without_replaying(self):
        self.delay_copy = True
        task = asyncio.create_task(self.adapter.call("copy", self.source_args()))
        await asyncio.wait_for(self.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        await self.adapter.call("status")
        self.assertEqual(self.calls.count("/share/sharepage/save"), 1)


class AsyncCopyProvider(fixtures.FakeProvider):
    async def call(self, action, args=None):
        if action == "copy_status":
            self.calls.append(action)
            return {"state": "completed", "file_id": "saved"}
        result = await super().call(action, args)
        return {"task_id": "async-copy"} if action == "copy" else result


class MultiAccountTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MediaEngineTest.asyncSetUp
    asyncTearDown = fixtures.MediaEngineTest.asyncTearDown
    wait_state = fixtures.MediaEngineTest.wait_state

    def add_quark(self):
        q = AsyncCopyProvider()
        q.folder_id = "quark-folder"
        q.video.pop("content_hash")
        q.video.pop("content_hash_name")
        self.plugin.settings.accounts["quark"] = {"provider": "quark", "file_backend": "native"}
        self.plugin.settings.targets["quark_series"] = {
            "account": "quark",
            "folder_id": "quark-folder",
            "openlist_path": "/夸克网盘/电视剧",
        }
        self.plugin.provider.adapters["quark"] = q
        return q

    def arguments(self):
        return {
            "request_id": str(uuid.uuid4()),
            "target": "quark_series",
            "share_url": "https://pan.quark.cn/s/shared123",
            "file_id": "video",
            "parent_file_id": "season",
        }

    async def test_routing_async_completion_and_read_cache_account_isolation(self):
        q = self.add_quark()
        args = self.arguments()
        result = await self.runtime.invoke(MediaRequest("transfer", args))
        done = await self.wait_state(result["operation_id"], "completed")
        self.assertEqual(done["data"]["copy_receipt"]["task_id"], "async-copy")
        self.assertEqual(done["data"]["verification"], "provider_file_id")
        self.assertEqual(q.calls.count("copy"), 1)
        self.assertNotIn("copy", self.provider.calls)
        for url in ["https://pan.quark.cn/s/shared123", "https://www.alipan.com/s/shared123"]:
            result = await self.runtime.invoke(MediaRequest("inspect", {"share_url": url}))
            self.assertFalse(result["inspection"]["cached"])
        self.assertEqual(q.calls.count("inspect"), 1)
        self.assertEqual(self.provider.calls.count("inspect"), 1)

    async def test_wrong_provider_and_unsupported_actions_fail_before_enqueue(self):
        q = self.add_quark()
        with self.assertRaises(ValueError):
            await self.runtime.invoke(MediaRequest("transfer", {**self.arguments(), "target": "movies"}))
        with self.assertRaisesRegex(MediaError, "provider_action_unsupported"):
            self.plugin.provider.require("quark", "permanent_delete")
        with self.assertRaises(ValueError):
            await self.runtime.invoke(
                MediaRequest(
                    "download",
                    {"request_id": str(uuid.uuid4()), "target": "quark_series", "file_id": "video"},
                )
            )
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM media_operations").fetchone()[0], 0)
        self.assertNotIn("copy", q.calls)

    async def test_restart_queries_stored_task_and_never_copies_again(self):
        q = self.add_quark()
        target = self.plugin.target("quark_series")
        item = self.plugin.repo.create(
            str(uuid.uuid4()),
            {
                "target": "quark_series",
                "destination": target,
                "share_id": "shared123",
                "password": "",
                "file_id": "video",
            },
            "assistant",
        )
        q.items = [{**q.video, "file_id": "saved", "parent_file_id": target["folder_id"]}]
        self.plugin.repo.change(
            item["id"], "submitting", file=q.video, submitted=True, copy_receipt={"task_id": "async-copy"}
        )
        self.plugin.repo.recover()
        self.plugin.wakeup.set()
        await self.wait_state(item["id"], "completed")
        self.assertNotIn("copy", q.calls)
        self.assertIn("copy_status", q.calls)

    async def test_login_recovery_is_account_scoped(self):
        self.add_quark()
        items = []
        for target in ["movies", "quark_series"]:
            item = self.plugin.repo.create(
                str(uuid.uuid4()),
                {
                    "target": target,
                    "destination": self.plugin.target(target),
                    "share_id": "shared123",
                    "password": "",
                    "file_id": "video",
                },
                "assistant",
            )
            self.plugin.repo.change(item["id"], "waiting_auth")
            items.append(item)
        self.plugin.repo.resume_auth("aliyun")
        self.assertEqual(self.plugin.repo.get(items[0]["id"])["state"], "queued")
        self.assertEqual(self.plugin.repo.get(items[1]["id"])["state"], "waiting_auth")
        self.plugin.repo.account_state("auth_required", account="quark")
        self.assertEqual(self.plugin.repo.account("aliyun")["state"], "unknown")

    async def test_configuration_requires_account_and_correct_provider_fields(self):
        parsed = MediaSettings.parse(
            {
                "accounts": {"q": {"provider": "quark"}},
                "targets": {"series": {"account": "q", "folder_id": "root", "openlist_path": "/quark"}},
            }
        )
        self.assertEqual(parsed.targets["series"]["account"], "q")
        for target in [
            {k: v for k, v in fixtures.TARGET.items() if k != "account"},
            {**fixtures.TARGET, "account": "missing"},
        ]:
            with self.assertRaises(ValueError):
                MediaSettings.parse({"targets": {"movies": target}})

    async def test_collection_completion_and_auth_notice_never_mix_accounts(self):
        from assistant_runtime.plugins.media.notifications import context

        self.add_quark()
        library = self.plugin.library
        library.collection(
            {
                "collection": "show",
                "target": "movies",
                "revision": 0,
                "definition": {
                    "title": "show",
                    "season": 1,
                    "total_episodes": 2,
                    "required_state": "cloud",
                    "evidence": "user confirmed",
                },
            }
        )
        library.remember("movies", fixtures.VIDEO, collection="show", season=1, episode=1)
        library.remember("quark_series", fixtures.VIDEO, collection="show", season=1, episode=2)
        result = library.collection({"collection": "show"})
        self.assertEqual(result["missing"], [2])
        self.assertFalse(result["completion_candidate"])
        self.assertEqual(context(self.plugin, "media-auth:quark:1"), (None, "media:account:quark"))
        self.assertEqual(context(self.plugin, "media-auth:aliyun:1"), (None, "media:account:aliyun"))
        self.plugin.settings.targets["movies"]["folder_id"] = "changed-root"
        self.assertEqual(library.collection({"collection": "show"})["missing"], [1, 2])
