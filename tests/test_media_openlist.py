"""Real HTTP filesystem protocol, durable recovery, routing and scope checks."""

import copy
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import aiohttp
from aiohttp import web

from assistant_runtime.api.contracts import MediaRequest
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.library import Library
from assistant_runtime.plugins.media.openlist import OpenList
from assistant_runtime.plugins.media.openlist_files import OpenListFiles
from assistant_runtime.plugins.media.openlist_library import OpenListLibrary
from assistant_runtime.plugins.media.plugin import MediaPlugin
from assistant_runtime.plugins.media.private import save
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings


class OpenListTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.content = b"original video fixture" * 100
        self.rows = {
            "/cloud": self.row("cloud", folder=True),
            "/cloud/sub": self.row("sub", folder=True),
            "/cloud/movie.mkv": self.row("movie.mkv"),
        }
        self.calls = []
        self.mode = "normal"
        self.hashes = True
        self.token = "private-token"
        app = web.Application()
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_get("/api/me", self.me)
        app.router.add_post("/api/fs/{action}", self.handle)
        app.router.add_get("/p/{path:.*}", self.download)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.credentials = self.root / "openlist.json"
        save(self.credentials, {"username": "test", "password": "private", "base_path": "/cloud"})
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
        self.addAsyncCleanup(self.session.close)
        self.client = OpenList(self.session, self.url, self.credentials)
        self.files = OpenListFiles(self.client, "/cloud")
        self.settings = MediaSettings.parse(
            {
                "accounts": {"mounted": {"provider": "openlist"}},
                "targets": {"films": {"account": "mounted", "openlist_path": "/cloud"}},
                "openlist_url": self.url,
            }
        )
        self.db = sqlite3.connect(self.root / "operations.db")
        self.addCleanup(self.db.close)
        self.repo = MediaRepository(self.db, self.settings)
        self.plugin = SimpleNamespace(
            settings=self.settings,
            repo=self.repo,
            openlist=self.client,
            target=lambda t: self.settings.targets[t],
            provider=SimpleNamespace(call=AsyncMock()),
        )
        self.library = Library(self.plugin)
        self.plugin.library = self.library
        self.port = OpenListLibrary(self.library)

    def row(self, name, folder=False):
        return {
            "name": name,
            "size": 0 if folder else len(self.content),
            "is_dir": folder,
            "modified": "2026-10-21T00:00:00Z",
            "hash_info": {} if folder else {"md5": hashlib.md5(self.content).hexdigest()},
        }

    async def me(self, request):
        if request.headers.get("Authorization") != self.token:
            return web.json_response({"code": 401})
        return web.json_response({"code": 200, "data": {"base_path": "/cloud"}})

    async def login(self, request):
        self.calls.append(("login", {}))
        return web.json_response({"code": 200, "data": {"token": self.token}})

    async def handle(self, request):
        action, body = request.match_info["action"], await request.json()
        self.calls.append((action, body))
        if request.headers.get("Authorization") != self.token:
            return web.json_response({"code": 401, "message": "private-body"})
        if self.mode == "denied":
            return web.json_response({"code": 403, "message": "private-body"})
        if self.mode == "malformed":
            return web.Response(text="private-body")

        def absolute(key):
            value = body[key]
            return "/cloud" + (value if value != "/" else "")

        if action == "list":
            path = absolute("path")
            if path not in self.rows:
                return web.json_response({"code": 500, "message": "private-body"})
            all_rows = [
                copy.deepcopy(row)
                for p, row in sorted(self.rows.items())
                if str(Path(p).parent) == path and p != path
            ]
            if not self.hashes:
                for r in all_rows:
                    r["hash_info"] = {}
            start = (body["page"] - 1) * body["per_page"]
            rows = all_rows[start : start + body["per_page"]]
            total = len(all_rows) + (1 if self.mode == "incomplete" else 0)
            return web.json_response({"code": 200, "data": {"content": rows, "total": total}})
        if action == "get":
            data = copy.deepcopy(self.rows[absolute("path")])
            data.update(raw_url="https://attacker.invalid/private", sign="signed-private")
            if not self.hashes:
                data["hash_info"] = {}
            return web.json_response({"code": 200, "data": data})
        if action == "mkdir":
            path = absolute("path")
            self.rows[path] = self.row(Path(path).name, folder=True)
        elif action in {"rename", "move"}:
            if action == "rename":
                source = absolute("path")
                destination = str(Path(source).with_name(body["name"]))
            else:
                source = absolute("src_dir") + "/" + body["names"][0]
                destination = absolute("dst_dir") + "/" + body["names"][0]
            self.assertFalse(body["overwrite"])
            if self.mode != "delayed":
                self.rows[destination] = self.rows.pop(source)
                self.rows[destination]["name"] = Path(destination).name
                self.rows[destination]["modified"] = "2026-10-21T01:00:00Z"
        else:
            raise AssertionError(action)
        if self.mode == "lost":
            return web.json_response({"code": 500, "message": "private-body"})
        return web.json_response({"code": 200, "data": None})

    async def download(self, request):
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(request.query.get("sign"), "signed-private")
        if self.mode == "proxy_forbidden":
            return web.Response(status=403, text="private provider diagnostics")
        start = int(request.headers["Range"].split("=")[1].split("-")[0])
        return web.Response(
            status=206,
            body=self.content[start:],
            headers={"Content-Range": f"bytes {start}-{len(self.content) - 1}/{len(self.content)}"},
        )

    async def operation(self, changes):
        plan = await self.port.preview({"target": "films", "changes": changes}, "assistant")
        public = self.library.apply(
            {"request_id": str(uuid.uuid4()), "plan_id": plan["plan_id"]}, "assistant", None
        )
        return self.repo.get(public["operation_id"])

    def mutations(self):
        return [x for x in self.calls if x[0] in {"mkdir", "rename", "move"}]

    async def test_scoped_listing_metadata_paging_and_cursor_scope(self):
        for n in range(105):
            self.rows[f"/cloud/{n}.mkv"] = self.row(f"{n}.mkv")
        first = await self.files.listing()
        self.assertEqual(len(first["items"]), 100)
        self.assertFalse(first["complete"])
        second = await self.files.listing(marker=first["marker"])
        self.assertEqual(len(second["items"]), 7)
        self.assertTrue(second["complete"])
        self.assertEqual(first["items"][0]["identity_kind"], "path")
        with self.assertRaises(ValueError):
            await self.files.listing("sub", first["marker"])
        self.assertTrue(all(c[1]["path"].startswith("/") for c in self.calls if "path" in c[1]))

    async def test_bad_paths_unsupported_actions_and_foreign_roots_never_write(self):
        for path in ("../secret", "/secret", "a/../x", "a//b", "a\\b", "%2e%2e", "@s/share"):
            with self.assertRaises(ValueError):
                self.files.path(path)
        for action in ("remove", "copy", "restore", "other"):
            with self.assertRaises(MediaError):
                await self.files.change(action, "movie.mkv", "new.mkv")
        with self.assertRaisesRegex(MediaError, "outside_account_root"):
            await OpenListFiles(self.client, "/elsewhere").listing()
        self.assertFalse(self.mutations())

    async def test_changed_server_account_scope_is_rejected_before_mutation(self):
        save(self.credentials, {"username": "test", "password": "private", "base_path": "/elsewhere"})
        with self.assertRaisesRegex(MediaError, "account_root_changed"):
            await self.files.change("mkdir", "new", "new")
        self.assertFalse(self.mutations())

    async def test_incomplete_listing_is_not_absence_and_errors_are_sanitized(self):
        self.mode = "incomplete"
        with self.assertRaisesRegex(MediaError, "incomplete_listing"):
            await self.files.absent("missing.mkv")
        for mode, code in (("denied", "permission_denied"), ("malformed", "service_invalid_response")):
            self.mode = mode
            with self.assertRaisesRegex(MediaError, code) as caught:
                await self.files.listing()
            self.assertNotIn("private", str(caught.exception))

    async def test_read_reauth_once_write_auth_failure_not_replayed(self):
        await self.files.listing()
        self.client.token = "old"
        await self.files.listing()
        self.assertEqual(sum(a == "login" for a, _ in self.calls), 2)
        self.client.token = "old"
        with self.assertRaisesRegex(MediaError, "auth_required"):
            await self.files.change("mkdir", "new", "new")
        self.assertEqual(len(self.mutations()), 1)
        await self.files.change("mkdir", "new", "new")
        self.assertIn("/cloud/new", self.rows)

    async def test_mkdir_rename_move_readback_and_native_not_called(self):
        for change in (
            {"action": "mkdir", "path": "folder"},
            {"action": "rename", "path": "movie.mkv", "name": "new.mkv"},
            {"action": "move", "path": "new.mkv", "folder": "folder"},
        ):
            item = await self.operation([change])
            await self.library.process(item)
            result = self.repo.get(item["id"])
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["data"]["steps"][0]["evidence"], "observed_path_state")
        self.assertIn("/cloud/folder/new.mkv", self.rows)
        self.plugin.provider.call.assert_not_awaited()

    async def test_lost_rename_response_recovery_observes_hash_and_never_replays(self):
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        self.mode = "lost"
        with self.assertRaises(MediaError) as caught:
            await self.library.process(item)
        self.assertTrue(caught.exception.uncertain)
        self.mode = "normal"
        self.repo.recover()
        await Library(self.plugin).process(self.repo.get(item["id"]))
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")
        self.assertEqual(len(self.mutations()), 1)

    async def test_acknowledged_rename_without_hash_accepts_changed_modified(self):
        self.hashes = False
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        await self.library.process(item)
        self.assertEqual(self.repo.get(item["id"])["state"], "completed")

    async def test_changed_modified_before_submit_and_changed_hash_after_submit_are_rejected(self):
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        self.rows["/cloud/movie.mkv"]["modified"] = "2026-10-21T02:00:00Z"
        with self.assertRaisesRegex(MediaError, "precondition_changed"):
            await self.library.process(item)
        self.assertFalse(self.mutations())
        self.rows["/cloud/movie.mkv"]["modified"] = "2026-10-21T00:00:00Z"
        self.mode = "lost"
        with self.assertRaises(MediaError):
            await self.library.process(item)
        self.rows["/cloud/new.mkv"]["hash_info"] = {"md5": "0" * 32}
        self.mode = "normal"
        with self.assertRaisesRegex(MediaError, "unconfirmed"):
            await self.library.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.mutations()), 1)

    async def test_lost_response_without_hash_and_mkdir_stay_uncertain(self):
        self.hashes = False
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        self.mode = "lost"
        with self.assertRaises(MediaError):
            await self.library.process(item)
        self.mode = "normal"
        with self.assertRaisesRegex(MediaError, "unconfirmed"):
            await self.library.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.mutations()), 1)
        # A directory's name is not proof that our lost write created it.
        self.repo.change(item["id"], "failed")
        item = await self.operation([{"action": "mkdir", "path": "newdir"}])
        self.mode = "lost"
        with self.assertRaises(MediaError):
            await self.library.process(item)
        self.mode = "normal"
        with self.assertRaisesRegex(MediaError, "mkdir_identity_unconfirmed"):
            await self.library.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.mutations()), 2)

    async def test_conflict_changed_precondition_and_cancel_before_submit(self):
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        self.rows["/cloud/movie.mkv"]["size"] += 1
        with self.assertRaisesRegex(MediaError, "precondition_changed"):
            await self.library.process(item)
        self.rows["/cloud/movie.mkv"]["size"] -= 1
        self.rows["/cloud/new.mkv"] = self.row("new.mkv")
        with self.assertRaisesRegex(MediaError, "destination_name_conflict"):
            await self.library.process(item)
        self.repo.change(item["id"], "queued", cancel_requested=True)
        await self.library.process(self.repo.get(item["id"]))
        self.assertEqual(self.repo.get(item["id"])["state"], "cancelled")
        self.assertFalse(self.mutations())

    async def test_delayed_write_not_success_and_cancel_does_not_replay(self):
        item = await self.operation([{"action": "rename", "path": "movie.mkv", "name": "new.mkv"}])
        self.mode = "delayed"
        with self.assertRaisesRegex(MediaError, "unconfirmed"):
            await self.library.process(item)
        self.repo.change(item["id"], "verifying", cancel_requested=True)
        with self.assertRaisesRegex(MediaError, "unconfirmed"):
            await self.library.process(self.repo.get(item["id"]))
        self.assertEqual(len(self.mutations()), 1)

    async def test_configuration_changed_blocks_saved_plans(self):
        item = await self.operation([{"action": "mkdir", "path": "new"}])
        self.settings.openlist_url += "/changed"
        with self.assertRaisesRegex(MediaError, "configuration_changed"):
            await self.library.process(item)
        self.assertFalse(self.mutations())

    async def test_download_ignores_raw_url_requires_hash_and_no_credentials(self):
        result = await self.files.download("movie.mkv")
        self.assertTrue(result["url"].startswith(self.url + "/p/cloud/movie.mkv?"))
        self.assertEqual(result["headers"], {})
        async with self.session.get(result["url"], headers={"Range": "bytes=0-"}) as response:
            self.assertEqual(await response.read(), self.content)
        self.hashes = False
        with self.assertRaisesRegex(MediaError, "checksum_unavailable"):
            await self.files.download("movie.mkv")

    async def test_task_permissions_apply_to_openlist_plans(self):
        plugin = object.__new__(MediaPlugin)
        plugin.__dict__.update(self.plugin.__dict__)
        plugin.accepting = True
        plugin.api = SimpleNamespace(
            invoke=AsyncMock(return_value={"run": {"state": "running", "task_id": "task", "resources": []}})
        )
        with self.assertRaisesRegex(ValueError, "media:films"):
            await plugin.request(
                MediaRequest(
                    "preview", {"target": "films", "changes": [{"action": "mkdir", "path": "new"}]}, "run"
                )
            )
        result = await plugin.request(MediaRequest("capabilities", {}, "run"))
        self.assertEqual(result["accounts"]["mounted"]["files"]["backend"], "openlist")
        self.assertNotIn("trash", result["accounts"]["mounted"]["files"]["actions"])
        self.assertFalse(self.mutations())

    async def prepare_download(self):
        from assistant_runtime.plugins.media.downloads import Downloads

        local, stage = self.root / "published", self.root / "staging"
        local.mkdir()
        stage.mkdir()
        self.settings.local_targets["films"] = {
            "path": str(local),
            "staging_path": str(stage),
            "mount_path": str(self.root),
        }
        self.settings.download_min_free_bytes = 0
        self.plugin.download_session = self.session
        self.plugin.reporter = Mock()
        self.downloads = Downloads(self.plugin)
        return local, stage

    async def test_shared_download_full_hash_resume_idempotency_and_no_credentials_in_receipt(self):
        local, stage = await self.prepare_download()
        args = {"target": "films", "path": "movie.mkv", "request_id": str(uuid.uuid4())}
        first = await self.port.enqueue_download(args, "assistant", None)
        second = await self.port.enqueue_download(args, "assistant", None)
        self.assertEqual(first["operation_id"], second["operation_id"])
        ident = first["operation_id"]
        # Real partial bytes and DB restart recovery, not a mocked streaming method.
        (stage / (ident + ".part")).write_bytes(self.content[:101])
        self.repo.recover()
        with patch.object(Path, "is_mount", lambda path: path == self.root):
            await self.downloads.process(self.repo.get(ident))
        self.assertEqual(self.repo.get(ident)["state"], "completed")
        self.assertEqual((local / "movie.mkv").read_bytes(), self.content)
        saved = str(self.repo.get(ident))
        for secret in ("private-token", "signed-private", "attacker.invalid"):
            self.assertNotIn(secret, saved)
        self.plugin.provider.call.assert_not_awaited()

    async def test_queued_path_replacement_fails_and_cancel_never_publishes(self):
        local, stage = await self.prepare_download()
        result = await self.port.enqueue_download(
            {"target": "films", "path": "movie.mkv", "request_id": str(uuid.uuid4())}, "assistant", None
        )
        ident = result["operation_id"]
        self.rows["/cloud/movie.mkv"]["modified"] = "2026-10-21T01:00:00Z"
        with self.assertRaisesRegex(MediaError, "source_changed"):
            await self.downloads.process(self.repo.get(ident))
        self.repo.change(ident, "queued", cancel_requested=True)
        with patch.object(Path, "is_mount", lambda path: path == self.root):
            await self.downloads.process(self.repo.get(ident))
        self.assertEqual(self.repo.get(ident)["state"], "cancelled")
        self.assertEqual(list(local.iterdir()), [])
        self.assertEqual(list(stage.glob("*.part")), [])

    async def test_proxy_forbidden_is_not_expiry_and_never_publishes(self):
        local, _ = await self.prepare_download()
        result = await self.port.enqueue_download(
            {"target": "films", "path": "movie.mkv", "request_id": str(uuid.uuid4())}, "assistant", None
        )
        self.mode = "proxy_forbidden"
        with patch.object(Path, "is_mount", lambda path: path == self.root):
            with self.assertRaisesRegex(MediaError, "openlist_proxy_forbidden"):
                await self.downloads.process(self.repo.get(result["operation_id"]))
        self.assertEqual(list(local.iterdir()), [])
        self.assertNotIn("private provider diagnostics", str(self.repo.get(result["operation_id"])))

    async def test_batch_paths_and_mcp_download_share_existing_permission_route(self):
        from fastmcp import Client
        from assistant_runtime.plugins.media.tools import create_server
        from assistant_runtime.plugins.media.batches import Batches

        batch = Batches(self.plugin)
        result = batch.create(
            {
                "request_id": str(uuid.uuid4()),
                "target": "films",
                "action": "download",
                "items": [{"path": "movie.mkv"}],
            },
            "assistant",
            None,
        )
        self.assertEqual(result["state"], "queued")
        with self.assertRaises(ValueError):
            batch.create(
                {
                    "request_id": str(uuid.uuid4()),
                    "target": "films",
                    "action": "download",
                    "items": [{"file_id": "native-id"}],
                },
                "assistant",
                None,
            )
        local = SimpleNamespace(invoke=AsyncMock(return_value={"state": "queued"}))
        async with Client(create_server(local, "run")) as client:
            await client.call_tool(
                "download_media", {"request_id": str(uuid.uuid4()), "target": "films", "path": "movie.mkv"}
            )
        request = local.invoke.await_args.args[0]
        self.assertEqual(request.caller_run, "run")
        self.assertEqual(request.arguments["path"], "movie.mkv")
        self.assertNotIn("file_id", request.arguments)

    async def test_actual_connection_loss_and_cancellation_leave_write_uncertain(self):
        import asyncio

        entered = asyncio.Event()

        async def hold(request):
            entered.set()
            await asyncio.sleep(0.1)
            request.transport.close()
            return web.Response()

        app = web.Application()
        app.router.add_post("/api/fs/mkdir", hold)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        client = OpenList(
            self.session,
            "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]),
            self.credentials,
        )
        client.token = "preauthenticated"
        with self.assertRaises(MediaError) as caught:
            await client.post("/api/fs/mkdir", {"path": "/folder"}, write=True)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(caught.exception.code, "provider_network_error")

    async def test_untrusted_metadata_is_not_a_valid_download_checksum(self):
        self.rows["/cloud/movie.mkv"]["hash_info"] = {"md5": "multipart-etag-32"}
        with self.assertRaisesRegex(MediaError, "invalid_checksum"):
            await self.files.download("movie.mkv")
        self.rows["/cloud/movie.mkv"]["hash_info"] = {"crc32": "ffffffff"}
        with self.assertRaisesRegex(MediaError, "checksum_unavailable"):
            await self.files.download("movie.mkv")
        self.rows["/cloud/movie.mkv"]["name"] = "../outside"
        with self.assertRaises(ValueError):
            await self.files.listing()


class SettingsTest(unittest.TestCase):
    def test_native_override_and_default_reuse_are_independent_of_acquisition(self):
        settings = MediaSettings.parse(
            {
                "accounts": {
                    "a": {"provider": "aliyun", "file_backend": "native"},
                    "q": {"provider": "quark", "file_backend": "native"},
                    "reuse": {"provider": "quark"},
                    "other": {"provider": "openlist"},
                }
            }
        )
        self.assertEqual(
            [settings.files_for(x) for x in ("a", "q", "reuse", "other")],
            ["native", "native", "openlist", "openlist"],
        )
        self.assertEqual(MediaSettings.parse({}).files_for("aliyun"), "native")
        for data in (
            {"file_backend": "auto"},
            {"accounts": {"a": {"provider": "openlist", "file_backend": "native"}}},
            {"accounts": {"a": {"provider": "quark", "file_backend": "anything"}}},
        ):
            with self.assertRaises(ValueError):
                MediaSettings.parse(data)
