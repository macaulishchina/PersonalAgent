"""Provider failures, refresh serialization, login identity and bounded HTTP IO."""

import asyncio
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
from fastmcp import Client
import requests

from assistant_runtime.mcp.media import create_server
from assistant_runtime.plugins.media.aliyun import Aliyun
from assistant_runtime.plugins.media import aliyun_worker as worker
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.openlist import OpenList
from assistant_runtime.plugins.media.private import save
from assistant_runtime.plugins.media.search import PanSou, share_id, extraction_code
from assistant_runtime.plugins.media.settings import MediaSettings


class ProviderProtocolTest(unittest.TestCase):
    def response(self, status, body):
        response = Mock(status_code=status)
        response.iter_content.return_value = [json.dumps(body).encode()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        return response

    def test_auth_permission_and_share_errors_are_distinct_without_payloads(self):
        for status, code, expected in (
            (401, "AccessTokenInvalid", "access_expired"),
            (403, "ForbiddenNoPermission.File", "permission_denied"),
            (400, "ShareLink.Forbidden", "share_forbidden"),
        ):
            auth = SimpleNamespace(session=Mock())
            auth.session.post.return_value = self.response(
                status, {"code": code, "secret": "raw-sensitive-body"}
            )
            with self.assertRaises(MediaError) as caught:
                worker.request(auth, "/v2/file/get", {})
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("raw-sensitive-body", str(caught.exception))

    def test_copy_timeout_or_server_failure_is_not_automatically_replayed(self):
        auth = SimpleNamespace(session=Mock())
        auth.session.post.side_effect = requests.Timeout("signed-url-secret")
        with self.assertRaises(MediaError) as caught:
            worker.request(auth, "/v2/file/copy", {}, write=True)
        self.assertTrue(caught.exception.uncertain)
        auth.session.post.assert_called_once()
        self.assertNotIn("signed-url", str(caught.exception))
        auth.session.post.side_effect = None
        auth.session.post.return_value = self.response(502, {"code": "InternalError"})
        with self.assertRaises(MediaError) as caught:
            worker.request(auth, "/v2/file/copy", {}, write=True)
        self.assertTrue(caught.exception.uncertain)

    def test_large_response_is_bounded_and_marks_write_uncertain(self):
        auth = SimpleNamespace(session=Mock())
        auth.session.post.return_value = self.response(200, {})
        auth.session.post.return_value.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
        with self.assertRaises(MediaError) as caught:
            worker.request(auth, "/v2/file/copy", {}, write=True)
        self.assertEqual(caught.exception.code, "provider_response_limit")
        self.assertTrue(caught.exception.uncertain)

    def test_share_url_validation_rejects_lookalikes_and_credentials(self):
        self.assertEqual(share_id("https://www.alipan.com/s/public123?pwd=abcd"), "public123")
        for url in (
            "https://www.alipan.com.evil.invalid/s/public123",
            "https://user:password@www.alipan.com/s/public123",
            "http://www.alipan.com/s/public123",
            "https://www.alipan.com/s/../../secret",
        ):
            with self.assertRaises(ValueError):
                share_id(url)
        self.assertEqual(extraction_code("https://www.alipan.com/s/public123?pwd=abcd"), "abcd")
        self.assertEqual(extraction_code("https://www.alipan.com/s/public123?pwd=abcd", "user"), "user")

    def test_settings_reject_unknowns_bad_destinations_and_boolean_limits(self):
        for settings in (
            {"surprise": True},
            {"queue_capacity": True},
            {"search_url": "http://user:pwd@host/"},
            {"targets": {"movies": {"drive_id": "d", "folder_id": "f", "openlist_path": "/../secret"}}},
        ):
            with self.assertRaises(ValueError):
                MediaSettings.parse(settings)

    def test_private_atomic_write_has_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "account.json"
            save(path, {"token": "private"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            save(path, {"token": "replacement"})
            self.assertEqual(json.loads(path.read_text()), {"token": "replacement"})
            self.assertEqual(list(path.parent.glob(".media-*")), [])

    def test_download_uses_original_endpoint_and_requires_configured_parent(self):
        file = {
            "file_id": "video",
            "parent_file_id": "allowed",
            "name": "film.mkv",
            "size": 123,
            "type": "file",
            "category": "video",
        }
        with patch.object(
            worker, "request", side_effect=[file, {"size": 123, "url": "https://original.invalid/signed"}]
        ) as request:
            result = worker.execute(
                None, "download_url", {"file_id": "video", "drive_id": "drive", "folder_id": "allowed"}
            )
        self.assertEqual(result["file"]["file_id"], "video")
        self.assertEqual(request.call_args_list[1].args[1], "/v2/file/get_download_url")
        with patch.object(worker, "request", return_value=file) as request:
            with self.assertRaisesRegex(MediaError, "file_outside_configured_target"):
                worker.execute(
                    None, "download_url", {"file_id": "video", "drive_id": "drive", "folder_id": "unrelated"}
                )
            request.assert_called_once()


class AuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.states = []
        self.provider = Aliyun(Path(self.tmp.name), self.states.append)
        self.addAsyncCleanup(self.provider.close)

    async def test_parallel_expiry_refreshes_once_and_other_calls_use_new_credentials(self):
        expired = True
        calls = []

        async def run(action, args=None):
            nonlocal expired
            calls.append(action)
            await asyncio.sleep(0.001)
            if action == "refresh":
                expired = False
            elif expired:
                raise MediaError("access_expired")
            return {"state": "ready"}

        self.provider.run = run
        await asyncio.gather(*(self.provider.call("inspect") for _ in range(5)))
        self.assertEqual(calls.count("refresh"), 1)
        self.assertEqual(calls.count("inspect"), 6)

    async def test_uncertain_copy_does_not_refresh_or_retry(self):
        self.provider.run = AsyncMock(side_effect=MediaError("provider_network_error", uncertain=True))
        with self.assertRaises(MediaError):
            await self.provider.call("copy")
        self.provider.run.assert_awaited_once_with("copy", None)

    async def test_refresh_failure_requires_login(self):
        self.provider.run = AsyncMock(side_effect=[MediaError("access_expired"), MediaError("auth_required")])
        with self.assertRaisesRegex(MediaError, "auth_required"):
            await self.provider.call("source")
        self.assertEqual(self.states, ["auth_required"])
        with self.assertRaisesRegex(MediaError, "auth_required"):
            await self.provider.call("source")
        self.assertEqual(self.provider.run.await_count, 2)

    async def test_interrupted_rotation_stays_blocked_after_restart(self):
        save(self.provider.credentials / "account.json", {"refresh_token": "original-private"})
        self.provider.run = AsyncMock(side_effect=[MediaError("access_expired"), asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await self.provider.call("inspect")
        restored = Aliyun(self.provider.root, self.states.append)
        self.addAsyncCleanup(restored.close)
        restored.run = AsyncMock()
        with self.assertRaisesRegex(MediaError, "auth_required"):
            await restored.call("inspect")
        restored.run.assert_not_awaited()
        self.assertEqual(self.states[-1], "auth_required")

    async def test_completed_atomic_rotation_recovers_after_lost_ack(self):
        save(self.provider.credentials / "account.json", {"refresh_token": "original-private"})
        await self.provider.block_auth()
        save(self.provider.credentials / "account.json", {"refresh_token": "rotated-private"})
        restored = Aliyun(self.provider.root, self.states.append)
        self.addAsyncCleanup(restored.close)
        restored.run = AsyncMock(return_value={"state": "ready"})
        await restored.call("inspect")
        restored.run.assert_awaited_once_with("inspect", None)
        self.assertFalse(restored.barrier.exists())

    async def test_sdk_device_metadata_change_does_not_unblock_rotation(self):
        save(self.provider.credentials / "account.json", {"refresh_token": "original-private"})
        await self.provider.block_auth()
        save(
            self.provider.credentials / "account.json",
            {"refresh_token": "original-private", "x_device_id": "new-device"},
        )
        restored = Aliyun(self.provider.root, Mock())
        self.addAsyncCleanup(restored.close)
        self.assertTrue(restored.auth_required)

    async def test_login_storage_failure_is_terminal_and_allows_another_login(self):
        async def run(action, directory=None, on_qr=None):
            save(directory / "account.json", {"refresh_token": "private"})
            return {"identity": "original"}

        self.provider.run = run
        session = {"login_id": "storage", "state": "starting", "expires_at": 9999999999}
        self.provider.login = session
        with patch(
            "assistant_runtime.plugins.media.aliyun.save", side_effect=OSError("private disk failure")
        ):
            await self.provider.login_worker(session, Mock())
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["error"], "login_storage_failed")
        self.assertNotIn("qr_base64", session)
        context = SimpleNamespace(spawn=lambda coroutine, name: coroutine.close())
        new = self.provider.begin_login(context, Mock())
        self.assertNotEqual(new["login_id"], session["login_id"])

    async def test_second_owner_cannot_refresh_the_same_credential_store(self):
        self.provider.claim()
        second = Aliyun(Path(self.tmp.name), Mock())
        self.addAsyncCleanup(second.close)
        with self.assertRaisesRegex(MediaError, "already_owned"):
            second.claim()

    async def test_wrong_account_login_does_not_replace_existing_credentials(self):
        save(self.provider.credentials / "identity.json", {"identity": "original"})
        save(self.provider.credentials / "account.json", {"refresh_token": "original-private"})

        async def run(action, directory=None, on_qr=None):
            save(directory / "account.json", {"refresh_token": "different-private"})
            return {"identity": "different"}

        self.provider.run = run
        session = {"login_id": "new", "state": "starting", "expires_at": 9999999999}
        done = Mock()
        await self.provider.login_worker(session, done)
        self.assertEqual(session["error"], "account_mismatch")
        self.assertEqual(
            json.loads((self.provider.credentials / "account.json").read_text())["refresh_token"],
            "original-private",
        )
        done.assert_not_called()
        self.assertFalse((self.provider.root / "login/new").exists())

    async def test_worker_cancellation_kills_and_reaps_child(self):
        process = SimpleNamespace(
            returncode=None,
            stdin=Mock(),
            stdout=SimpleNamespace(readline=AsyncMock()),
            kill=Mock(),
            wait=AsyncMock(),
        )
        process.stdin.drain = AsyncMock()

        async def wait_forever():
            await asyncio.Future()

        process.stdout.readline.side_effect = wait_forever
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            # SimpleNamespace is unhashable; subprocess identity normally is hashable.
            class Process:
                pass

            child = Process()
            child.__dict__.update(process.__dict__)
            with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
                task = asyncio.create_task(self.provider.run("copy"))
                await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                child.kill.assert_called_once()
                child.wait.assert_awaited_once()
                self.assertFalse(self.provider.children)

    async def test_qr_is_native_image_and_never_copied_to_structured_results(self):
        value = {
            "login_id": "login",
            "state": "scan_required",
            "qr_base64": base64.b64encode(b"PNG fixture").decode(),
        }
        client = SimpleNamespace(invoke=AsyncMock(return_value=value))
        async with Client(create_server(client)) as mcp:
            result = await mcp.call_tool("get_media_login", {"login_id": "login"})
            self.assertNotIn("qr_base64", result.structured_content)
            self.assertTrue(any(item.type == "image" for item in result.content))
            self.assertIn("qr_base64", value)


class HttpAdaptersTest(unittest.IsolatedAsyncioTestCase):
    async def serve(self, app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def test_search_filters_mislabeled_domains_null_links_and_duplicates(self):
        async def search(request):
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "total": 999,
                        "results": [
                            {"title": "none", "links": None},
                            {"title": "invalid schema", "links": {"url": "invalid"}},
                            {
                                "title": "movie",
                                "links": [
                                    {"type": "aliyun", "url": "https://pan.quark.cn/s/wrong"},
                                    {"type": "quark", "url": "https://www.alipan.com/s/public123"},
                                    {"url": "https://www.aliyundrive.com/s/public123"},
                                ],
                            },
                        ],
                    },
                }
            )

        app = web.Application()
        app.router.add_post("/api/search", search)
        url = await self.serve(app)
        async with aiohttp.ClientSession() as session:
            adapter = PanSou(session, url)
            result = await adapter.search("movie")
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["complete"])
        self.assertFalse(result["items"][0]["verified"])

    async def test_search_invalid_result_shape_has_a_sanitized_error(self):
        app = web.Application()

        async def search(request):
            return web.json_response({"code": 0, "data": {"results": {"private": "unexpected"}}})

        app.router.add_post("/api/search", search)
        url = await self.serve(app)
        async with aiohttp.ClientSession() as session:
            with self.assertRaisesRegex(MediaError, "search_invalid_response"):
                await PanSou(session, url).search("movie")

    async def test_openlist_reauthenticates_once_and_never_sends_token_to_download(self):
        calls = []
        app = web.Application()
        url = None

        async def login(request):
            calls.append("login")
            return web.json_response({"code": 200, "data": {"token": "new-private"}})

        async def listing(request):
            if request.headers.get("Authorization") != "new-private":
                return web.json_response({"code": 401})
            return web.json_response({"code": 200, "data": {"content": []}})

        async def get(request):
            return web.json_response(
                {
                    "code": 200,
                    "data": {
                        "name": "movie.mkv",
                        "size": 100,
                        "raw_url": url + "/download?signed-secret=private",
                    },
                }
            )

        async def download(request):
            self.assertNotIn("Authorization", request.headers)
            self.assertEqual(request.headers.get("Range"), "bytes=0-4095")
            return web.Response(status=206, body=bytes.fromhex("1a45dfa3") + b"x" * 4092)

        app.router.add_post("/api/auth/login", login)
        app.router.add_post("/api/fs/list", listing)
        app.router.add_post("/api/fs/get", get)
        app.router.add_get("/download", download)
        url = await self.serve(app)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openlist.json"
            save(path, {"username": "media", "password": "private"})
            async with aiohttp.ClientSession() as session:
                adapter = OpenList(session, url, path)
                adapter.token = "expired"
                result = await adapter.verify(
                    {"openlist_path": "/movies"}, {"name": "movie.mkv", "size": 100}, probe=True
                )
        self.assertEqual(calls, ["login"])
        self.assertEqual(result["state"], "visible")
        self.assertTrue(result["mkv_header_valid"])
        self.assertEqual(result["probe_bytes"], 4096)
        self.assertNotIn("signed-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
