"""Real HTTP Range/stream IO and filesystem recovery for the local media library."""

from platform_support import symlink_or_skip

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import aiohttp
from aiohttp import web

from assistant_runtime.plugins.media.downloads import Downloads, LocalFiles
from assistant_runtime.plugins.media.library import Library
from assistant_runtime.plugins.media.private import disk
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        self.library, self.stage = self.mount / "library", self.mount / "private-parts"
        self.library.mkdir()
        self.stage.mkdir()
        self.mount_patch = patch.object(Path, "is_mount", lambda path: path == self.mount)
        self.mount_patch.start()
        self.addCleanup(self.mount_patch.stop)
        self.local = {
            "path": str(self.library),
            "staging_path": str(self.stage),
            "mount_path": str(self.mount),
        }
        self.cloud = {
            "account": "aliyun",
            "drive_id": "drive",
            "folder_id": "folder",
            "openlist_path": "/cloud",
        }
        self.settings = MediaSettings.parse(
            {
                "targets": {"movies": self.cloud},
                "local_targets": {"movies": self.local},
                "download_min_free_bytes": 0,
            }
        )
        self.db = sqlite3.connect(self.mount / "operations.db")
        self.addCleanup(self.db.close)
        self.repo = MediaRepository(self.db, self.settings)
        self.content = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 12000
        self.file = {
            "file_id": "chosen",
            "name": "movie.mkv",
            "type": "file",
            "category": "video",
            "size": len(self.content),
            "content_hash_name": "sha1",
            "content_hash": hashlib.sha1(self.content).hexdigest(),
        }
        self.ranges, self.mode, self.calls = [], "normal", []
        self.download_options = {}
        self.received_cookie = None
        self.receiving = asyncio.Event()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/original", self.respond)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:%d/original" % site._server.sockets[0].getsockname()[1]
        self.http = aiohttp.ClientSession(trust_env=False)

        def get(url, **kwargs):
            self.assertEqual(url, "https://original.invalid/file?signed=private")
            return self.http.get(self.url, **kwargs)

        self.plugin = SimpleNamespace(
            repo=self.repo,
            settings=self.settings,
            provider=SimpleNamespace(call=self.provider),
            download_session=SimpleNamespace(get=get),
            reporter=Mock(),
        )
        self.plugin.target = lambda name: self.settings.targets[name]
        self.plugin.library = Library(self.plugin)
        self.downloads = Downloads(self.plugin)

    async def asyncTearDown(self):
        self.release.set()
        await self.http.close()
        await self.runner.cleanup()

    async def provider(self, action, arguments):
        self.calls.append(action)
        self.assertEqual(arguments["file_id"], "chosen")
        if action == "file":
            return dict(self.file)
        if action == "download_url":
            return {
                "file": dict(self.file),
                "url": "https://original.invalid/file?signed=private",
                **self.download_options,
            }
        raise AssertionError(action)

    async def respond(self, request):
        self.assertNotIn("Authorization", request.headers)
        self.received_cookie = request.headers.get("Cookie")
        self.ranges.append(request.headers["Range"])
        if self.mode == "redirect":
            return web.Response(status=302, headers={"Location": self.url + "?redirected"})
        if self.mode == "expired":
            self.mode = "normal"
            return web.Response(status=403)
        offset = int(request.headers["Range"][6:-1])
        headers = {
            "Content-Range": f"bytes {offset}-{len(self.content) - 1}/{len(self.content)}",
            "Content-Length": str(len(self.content) - offset),
        }
        if self.mode == "ignore-range":
            return web.Response(body=self.content)
        if self.mode == "wrong-range":
            headers["Content-Range"] = f"bytes 0-{len(self.content) - 1}/{len(self.content)}"
        response = web.StreamResponse(status=206, headers=headers)
        await response.prepare(request)
        if self.mode in {"disconnect", "hold"}:
            await response.write(self.content[offset : offset + 1048576])
            self.receiving.set()
            if self.mode == "hold":
                await self.release.wait()
            request.transport.close()
        else:
            data = self.content[offset:]
            if self.mode == "corrupt":
                data = b"!" * len(data)
            await response.write(data)
        return response

    def create(self):
        return self.repo.create(
            str(uuid.uuid4()),
            {
                "kind": "download",
                "file_id": "chosen",
                "target": "movies",
                "destination": self.cloud,
                "local_destination": self.local,
            },
            "assistant",
        )

    async def test_nested_download_mirrors_directory_and_rejects_symlink_parent(self):
        from unittest.mock import AsyncMock

        item = self.create()
        intent = item["data"]["intent"]
        intent["root_destination"] = dict(self.cloud)
        intent["destination"] = {
            **self.cloud,
            "folder_id": "nested",
            "openlist_path": "/cloud/Series/Season 01",
        }
        intent["relative_parts"] = ["Series", "Season 01"]
        self.repo.change(item["id"], item["state"], intent=intent)
        self.plugin.library.destination = AsyncMock(
            return_value=(intent["destination"], intent["relative_parts"])
        )
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual((self.library / "Series" / "Season 01" / "movie.mkv").read_bytes(), self.content)
        symlink_or_skip(self.library / "escape", self.stage, target_is_directory=True)
        files = LocalFiles({**self.local, "relative_parts": ["escape"]}, "another", "test.mkv", 0)
        try:
            with self.assertRaises(OSError):
                files.open()
        finally:
            files.close()

    async def test_original_full_checksum_atomic_publish_and_dedup(self):
        item = self.create()
        await self.downloads.process(item)
        result = self.repo.public(self.repo.get(item["id"]))
        self.assertEqual((self.library / "movie.mkv").read_bytes(), self.content)
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(result["verification"], "content_hash")
        self.assertEqual(result["progress"]["percent"], 100)
        self.assertEqual(result["kind"], "download")
        self.assertNotIn("signed", json.dumps(result))
        self.assertEqual(self.create()["id"], item["id"])
        notice = json.loads(self.db.execute("SELECT payload FROM media_outbox").fetchone()[0])
        self.assertEqual(notice["title"], "影视资源已下载")

    async def test_disconnect_then_restart_resumes_actual_partial_bytes(self):
        item = self.create()
        self.mode = "disconnect"
        with self.assertRaisesRegex(MediaError, "download_network_error"):
            await self.downloads.process(item)
        self.assertFalse((self.library / "movie.mkv").exists())
        partial = self.stage / (item["id"] + ".part")
        offset = partial.stat().st_size
        self.assertGreater(offset, 0)
        self.repo.recover()
        self.mode = "normal"
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(self.ranges[-1], f"bytes={offset}-")
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")

    async def test_ignored_or_wrong_resumed_range_never_appends_full_response(self):
        for mode in ("ignore-range", "wrong-range"):
            item = self.create()
            partial = self.stage / (item["id"] + ".part")
            partial.write_bytes(self.content[:1000])
            self.mode = mode
            with self.assertRaises(MediaError):
                await self.downloads.process(item)
            self.assertEqual(partial.stat().st_size, 1000)
            self.assertFalse((self.library / "movie.mkv").exists())
            self.repo.change(item["id"], "failed")

    async def test_expired_url_is_not_account_failure_and_can_refresh(self):
        item = self.create()
        self.mode = "expired"
        with self.assertRaisesRegex(MediaError, "download_url_expired"):
            await self.downloads.process(item)
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(self.calls.count("download_url"), 2)
        self.assertEqual(self.repo.account()["state"], "unknown")

    async def test_corrupt_bytes_removed_and_never_published(self):
        item = self.create()
        self.mode = "corrupt"
        with self.assertRaisesRegex(MediaError, "download_checksum_mismatch"):
            await self.downloads.process(item)
        self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(list(self.stage.iterdir()), [])

    async def test_md5_cookie_download_resumes_and_keeps_secrets_out_of_receipts(self):
        self.file.update(content_hash_name="md5", content_hash=hashlib.md5(self.content).hexdigest())
        self.download_options = {
            "headers": {"Cookie": "private-auth"},
            "credential_origin": "https://original.invalid",
        }
        item = self.create()
        self.mode = "disconnect"
        with self.assertRaisesRegex(MediaError, "download_network_error"):
            await self.downloads.process(item)
        self.repo.recover()
        self.mode = "normal"
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(self.received_cookie, "private-auth")
        self.assertEqual((self.library / "movie.mkv").read_bytes(), self.content)
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")
        for row in self.db.execute("SELECT data FROM media_operations"):
            self.assertNotIn("private-auth", row[0])
            self.assertNotIn("signed=", row[0])

    async def test_cookie_scope_and_redirects_never_forward_credentials(self):
        item = self.create()
        for opts in [
            {"headers": {"Cookie": "private-auth"}},
            {"headers": {"Cookie": "private-auth"}, "credential_origin": "https://other.invalid"},
            {"headers": {"Authorization": "Bearer private-auth"}},
            {"headers": {"Cookie": "x\r\nInjected: true"}, "credential_origin": "https://original.invalid"},
        ]:
            self.download_options = opts
            with self.subTest(opts=opts), self.assertRaisesRegex(MediaError, "invalid_download_headers"):
                await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(self.ranges, [])
        self.download_options = {
            "headers": {"Cookie": "private-auth"},
            "credential_origin": "https://original.invalid",
        }
        self.mode = "redirect"
        with self.assertRaisesRegex(MediaError, "download_range_unsupported"):
            await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.ranges), 1)

    async def test_md5_corruption_and_changed_source_never_publish(self):
        self.file.update(content_hash_name="md5", content_hash=hashlib.md5(self.content).hexdigest())
        item = self.create()
        self.mode = "corrupt"
        with self.assertRaisesRegex(MediaError, "download_checksum_mismatch"):
            await self.downloads.process(item)
        self.assertEqual(list(self.library.iterdir()), [])
        self.assertEqual(list(self.stage.iterdir()), [])
        self.file["content_hash"] = "0" * 32
        with self.assertRaisesRegex(MediaError, "download_source_changed"):
            await self.downloads.process(self.repo.get(item["id"]))

    async def test_disk_unmounted_space_exhausted_and_unsafe_names_fail_before_http(self):
        item = self.create()
        with patch.object(Path, "is_mount", return_value=False):
            with self.assertRaisesRegex(MediaError, "download_mount_unavailable"):
                await self.downloads.process(item)
        with patch.object(LocalFiles, "space", side_effect=MediaError("local_capacity_exhausted")):
            with self.assertRaisesRegex(MediaError, "local_capacity_exhausted"):
                await self.downloads.process(self.repo.get(item["id"]))
        self.file["name"] = "../escape.mkv"
        new = self.create()
        self.repo.change(new["id"], "queued", file=None)
        with self.assertRaisesRegex(MediaError, "unsafe_media_filename"):
            await self.downloads.process(self.repo.get(new["id"]))
        self.assertEqual(self.ranges, [])

    async def test_source_change_and_destination_collision_never_overwrite(self):
        item = self.create()
        self.repo.change(item["id"], "checking", file={**self.file, "size": 1})
        with self.assertRaisesRegex(MediaError, "download_source_changed"):
            await self.downloads.process(self.repo.get(item["id"]))
        (self.library / "movie.mkv").write_bytes(b"user-owned")
        self.repo.change(item["id"], "queued", file=None)
        with self.assertRaisesRegex(MediaError, "destination_name_conflict"):
            await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual((self.library / "movie.mkv").read_bytes(), b"user-owned")

    async def test_crash_after_publish_before_db_ack_reconciles_without_redownload(self):
        item = self.create()
        original = self.repo.change

        def fail(ident, state, **kwargs):
            if state == "completed":
                raise OSError("simulated lost commit")
            return original(ident, state, **kwargs)

        with patch.object(self.repo, "change", side_effect=fail):
            with self.assertRaises(OSError):
                await self.downloads.process(item)
        self.repo.recover()
        self.repo.request_cancel(item["id"])
        self.calls.clear()
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.ranges), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")

    async def test_unconfirmed_publication_does_not_publish_again_after_restart(self):
        item = self.create()
        with patch.object(LocalFiles, "publish", side_effect=OSError("lost publication result")) as publish:
            with self.assertRaises(OSError):
                await self.downloads.process(item)
            self.repo.recover()
            with self.assertRaisesRegex(MediaError, "download_publication_unconfirmed"):
                await self.downloads.process(self.repo.get(item["id"]))
            publish.assert_called_once()
        self.assertEqual(len(self.ranges), 1)
        self.assertFalse((self.library / self.file["name"]).exists())

    async def test_shutdown_retains_partial_and_cancel_removes_it_without_auth(self):
        item = self.create()
        self.mode = "hold"
        task = asyncio.create_task(self.downloads.process(item))
        await self.receiving.wait()
        await asyncio.sleep(0.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        partial = self.stage / (item["id"] + ".part")
        self.assertTrue(partial.exists())
        self.repo.recover()
        self.repo.change(item["id"], "queued", cancel_requested=True)
        self.calls.clear()
        await self.downloads.process(self.repo.get(item["id"]))
        self.assertFalse(partial.exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.repo.get(item["id"])["state"], "cancelled")

    async def test_filesystem_symlinks_are_rejected(self):
        item = self.create()
        symlink_or_skip(self.stage / (item["id"] + ".part"), self.mount / "private")
        with self.assertRaises(OSError):
            await self.downloads.process(item)
        self.assertFalse((self.mount / "private").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX filesystem FIFO test")
    async def test_fifo_destination_is_rejected_without_blocking_shutdown(self):
        item = self.create()
        os.mkfifo(self.library / "movie.mkv")
        async with asyncio.timeout(1):
            with self.assertRaisesRegex(MediaError, "destination_name_conflict"):
                await self.downloads.process(item)
        self.assertEqual(self.ranges, [])

    async def test_disk_cancellation_drains_owned_io(self):
        import threading
        import time

        finished = threading.Event()

        def io():
            time.sleep(0.05)
            finished.set()

        task = asyncio.create_task(disk(io))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_worker_waits_for_auth_and_resumes_without_cloud_copy(self):
        from unittest.mock import AsyncMock

        item = self.create()
        self.plugin.provider.call = AsyncMock(side_effect=MediaError("auth_required"))
        task = asyncio.create_task(self.downloads.worker())
        try:
            async with asyncio.timeout(2):
                while self.repo.get(item["id"])["state"] != "waiting_auth":
                    await asyncio.sleep(0.01)
            self.assertEqual(self.repo.account()["state"], "auth_required")
            self.assertEqual(self.repo.db.execute("SELECT count(*) FROM media_outbox").fetchone()[0], 1)
            self.plugin.provider.call = self.provider
            self.repo.resume_auth()
            async with asyncio.timeout(3):
                while self.repo.get(item["id"])["state"] != "completed":
                    await asyncio.sleep(0.01)
            self.assertNotIn("copy", self.calls)
            self.assertIsNone(self.repo.due())  # Cloud-copy worker cannot pick downloads.
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class LocalSettingsTest(unittest.TestCase):
    def test_reject_library_staging_overlap_and_system_root(self):
        cloud = {"account": "aliyun", "drive_id": "d", "folder_id": "f", "openlist_path": "/cloud"}
        for local in (
            {"path": "/mnt/disk/media", "staging_path": "/mnt/disk/media/tmp", "mount_path": "/mnt/disk"},
            {"path": "/tmp/media", "staging_path": "/tmp/parts", "mount_path": "/"},
            {"path": "/mnt/disk/../escape", "staging_path": "/mnt/disk/tmp", "mount_path": "/mnt/disk"},
        ):
            with self.assertRaises(ValueError):
                MediaSettings.parse({"targets": {"movies": cloud}, "local_targets": {"movies": local}})
