"""Acquisition persistence, cancellation, authorization and real MCP schema behavior."""

import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.kernel.contracts import (
    MediaRequest,
    NotificationRequest,
    NotificationLink,
    TaskRequest,
)
from assistant_runtime.kernel.runtime import Manifest, Runtime
from assistant_runtime.mcp.catalog import MEDIA_TOOLS
from assistant_runtime.mcp.media import create_server
from assistant_runtime.plugins.codex.mcp import server_config
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.plugin import MediaPlugin
from assistant_runtime.plugins.media.repository import MediaRepository
from assistant_runtime.plugins.media.settings import MediaSettings
from assistant_runtime.store import Store

TARGET = {"account": "aliyun", "drive_id": "drive", "folder_id": "folder", "openlist_path": "/test/movies"}
VIDEO = {
    "file_id": "video",
    "name": "movie.mkv",
    "type": "file",
    "size": 100,
    "category": "video",
    "content_hash": "a" * 40,
    "content_hash_name": "sha1",
}


def intent(**overrides):
    return {
        "share_id": "publicshare",
        "password": "",
        "file_id": "video",
        "target": "movies",
        "destination": dict(TARGET),
        **overrides,
    }


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.repo = MediaRepository(self.db, MediaSettings())
        self.addCleanup(self.db.close)

    def create(self, **overrides):
        return self.repo.create(str(uuid.uuid4()), intent(**overrides), "assistant")

    def test_lost_reply_reuses_receipt_and_rejects_changed_request(self):
        request_id = str(uuid.uuid4())
        original = self.repo.create(request_id, intent(), "assistant")
        repeated = self.repo.create(request_id, intent(), "assistant")
        self.assertEqual(original["id"], repeated["id"])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.repo.create(request_id, intent(file_id="another"), "assistant")
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_operations").fetchone()[0], 1)

    def test_episode_identity_deduplicates_across_share_and_task_run(self):
        first = self.create(collection="show:2015", season=1, episode=2)
        again = self.repo.create(
            str(uuid.uuid4()),
            intent(share_id="newshare", file_id="newfile", collection="show:2015", season=1, episode=2),
            "task:series",
        )
        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["id"], self.create(collection="show:2015", season=1, episode=3)["id"])

    def test_recover_checks_safe_work_and_never_requeues_submitted_write(self):
        first, second = self.create(), self.create(file_id="two")
        self.repo.change(first["id"], "checking")
        self.repo.change(second["id"], "submitting", submitted=True)
        self.repo.recover()
        self.assertEqual(self.repo.get(first["id"])["state"], "queued")
        self.assertEqual(self.repo.get(second["id"])["state"], "verifying")

    def test_completion_and_outbox_are_atomic_and_duration_is_stable(self):
        item = self.create()
        self.repo.change(item["id"], "checking", file=VIDEO)
        self.db.execute(
            "CREATE TRIGGER reject_notice BEFORE INSERT ON media_outbox BEGIN SELECT RAISE(ABORT,'outbox full'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.change(item["id"], "completed")
        self.assertEqual(self.repo.get(item["id"])["state"], "checking")
        self.db.execute("DROP TRIGGER reject_notice")
        first = self.repo.change(item["id"], "completed")
        second = self.repo.change(item["id"], "completed", openlist={"state": "visible"})
        self.assertEqual(first["data"]["duration_seconds"], second["data"]["duration_seconds"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_outbox").fetchone()[0], 1)

    def test_delegated_success_is_progress_but_standalone_success_and_failure_are_reminders(self):
        import json

        for caller in (None, "run-one"):
            item = self.repo.create(str(uuid.uuid4()), intent(file_id=str(uuid.uuid4())), "assistant", caller)
            self.repo.change(item["id"], "completed", file=VIDEO)
            raw = self.db.execute(
                "SELECT payload FROM media_outbox WHERE key=?", ("media-complete:" + item["id"],)
            ).fetchone()[0]
            self.assertEqual(json.loads(raw)["kind"], "result")
            self.assertEqual(json.loads(raw).get("report_with_task", False), bool(caller))
        failure = self.repo.create(str(uuid.uuid4()), intent(file_id="fail"), "assistant", "run-one")
        self.repo.change(failure["id"], "failed")
        raw = self.db.execute(
            "SELECT payload FROM media_outbox WHERE key=?", ("media-attention:" + failure["id"],)
        ).fetchone()[0]
        self.assertEqual(json.loads(raw)["kind"], "attention")

    def test_auth_notice_is_transition_scoped_and_retry_does_not_duplicate(self):
        self.repo.account_state("auth_required")
        self.repo.account_state("auth_required")
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_outbox").fetchone()[0], 1)
        self.repo.account_state("ready")
        self.repo.account_state("auth_required")
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_outbox").fetchone()[0], 2)

    def test_pages_have_no_overlap_and_reject_cursor_with_different_filter(self):
        ids = {self.create(file_id=str(i), collection="film")["id"] for i in range(5)}
        page = self.repo.page(limit=2, collection="film")
        found = set()
        while True:
            found.update(item["operation_id"] for item in page["items"])
            if not page["before"]:
                break
            with self.assertRaises(ValueError):
                self.repo.page(before=page["before"], collection="other")
            page = self.repo.page(before=page["before"], limit=2, collection="film")
        self.assertEqual(found, ids)

    def test_capacity_and_public_results_do_not_leak_passwords(self):
        self.repo.settings.queue_capacity = 1
        item = self.create(password="private-extraction-code")
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.create(file_id="second")
        self.assertNotIn("private-extraction-code", json.dumps(self.repo.public(item)))


class FakeProvider:
    def claim(self):
        pass

    def __init__(self):
        self.calls = []
        self.items = []
        self.copy_error = None
        self.auth_required = False
        self.hold_source = None
        self.hold_copy = None
        self.entered = asyncio.Event()
        self.login = None
        self.video = dict(VIDEO)
        self.folder_id = "folder"

    async def call(self, action, args=None):
        self.calls.append(action)
        if self.auth_required:
            raise MediaError("auth_required")
        if action == "status":
            return {"state": "ready"}
        if action == "inspect":
            return {"items": [dict(self.video)], "marker": None}
        if action == "folder":
            return {"type": "folder"}
        if action == "source":
            if self.hold_source:
                self.entered.set()
                await self.hold_source.wait()
            return dict(self.video)
        if action == "list":
            return {"items": copy.deepcopy(self.items), "marker": None}
        if action == "file":
            file = next((v for v in self.items if v["file_id"] == args["file_id"]), None)
            if file is None:
                raise MediaError("file_not_found")
            return {"parent_file_id": self.folder_id, **copy.deepcopy(file)}
        if action == "capacity":
            return {"total": 1000, "used": 0}
        if action == "copy":
            self.items = [{**self.video, "file_id": "saved", "parent_file_id": args["folder_id"]}]
            if self.hold_copy:
                self.entered.set()
                await self.hold_copy.wait()
            if self.copy_error:
                raise self.copy_error
            return {"file_id": "saved"}
        raise AssertionError(action)

    async def close(self):
        pass


class Dependencies:
    manifest = Manifest("notifications")

    def __init__(self):
        self.notices = []
        self.runs = {}

    async def start(self, context):
        async def notify(request):
            self.notices.append(request.arguments)
            return {"id": "notice"}

        async def tasks(request):
            return self.runs

        context.register(NotificationRequest, notify)
        context.register(NotificationLink, lambda request: asyncio.sleep(0, result={"linked": False}))
        context.register(TaskRequest, tasks)


class MediaEngineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "host.db")
        self.runtime = Runtime(self.store)
        self.dependencies = Dependencies()
        self.runtime.add(self.dependencies)
        self.provider = FakeProvider()
        self.host = SimpleNamespace(
            config=SimpleNamespace(
                media={"targets": {"movies": dict(TARGET)}}, state_dir=Path(self.tmp.name)
            ),
            store=self.store,
            runtime=self.runtime,
        )
        self.plugin = MediaPlugin(self.host, {"aliyun": self.provider})
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()

    async def submit(self, caller=None, **overrides):
        args = {
            "request_id": str(uuid.uuid4()),
            "share_url": "https://www.alipan.com/s/publicshare",
            "file_id": "video",
            "target": "movies",
            **overrides,
        }
        return await self.runtime.invoke(MediaRequest("transfer", args, caller))

    async def wait_state(self, ident, *states):
        async with asyncio.timeout(2):
            while True:
                item = self.plugin.repo.get(ident)
                if item["state"] in states:
                    return item
                await asyncio.sleep(0.005)

    async def test_search_history_and_inspection_keep_active_task_read_boundary(self):
        self.plugin.search_history.record({"queries": ["movie"]})
        for action, args in [
            ("search_history", {}),
            ("search_sources", {}),
            ("inspect", {"share_url": "https://www.alipan.com/s/publicshare"}),
        ]:
            with self.assertRaisesRegex(ValueError, "active task"):
                await self.runtime.invoke(MediaRequest(action, args, "stale"))
        self.dependencies.runs["reader"] = {"state": "running", "task_id": "task", "resources": []}
        result = await self.runtime.invoke(MediaRequest("search_history", {}, "reader"))
        self.assertEqual(result["items"][0]["queries"], ["movie"])
        args = {"share_url": "https://www.alipan.com/s/publicshare"}
        first = await self.runtime.invoke(MediaRequest("inspect", args, "reader"))
        second = await self.runtime.invoke(MediaRequest("inspect", args))
        self.assertFalse(first["inspection"]["cached"])
        self.assertTrue(second["inspection"]["cached"])
        self.assertEqual(self.provider.calls.count("inspect"), 1)
        await self.runtime.invoke(MediaRequest("inspect", {**args, "refresh": True}))
        self.assertEqual(self.provider.calls.count("inspect"), 2)
        self.assertNotIn("copy", self.provider.calls)
        caps = await self.runtime.invoke(MediaRequest("capabilities", {}))
        self.assertIn("quark", caps["discovery"]["platforms"])
        self.assertEqual(caps["acquisition_providers"], ["aliyun"])
        self.assertEqual(
            caps["target_bindings"]["movies"],
            {"account": "aliyun", "provider": "aliyun", "task_resource": "media:movies"},
        )

    async def test_transfer_copies_once_and_verifies_content(self):
        item = await self.submit()
        done = await self.wait_state(item["operation_id"], "completed")
        self.assertEqual(self.provider.calls.count("copy"), 1)
        self.assertEqual(done["data"]["saved_file_id"], "saved")
        self.assertEqual(done["data"]["cloud_verification"]["folder_id"], "folder")
        self.assertEqual(done["data"]["cloud_verification"]["state"], "verified")
        self.assertIn("file", self.provider.calls)
        await self.submit()
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_reverify_detects_missing_file_without_copying_again(self):
        item = await self.submit()
        await self.wait_state(item["operation_id"], "completed")
        self.provider.items = []
        result = await self.runtime.invoke(MediaRequest("verify", {"operation_id": item["operation_id"]}))
        self.assertEqual(result["cloud_verification"]["state"], "unconfirmed")
        self.assertEqual(result["cloud_verification"]["error"], "file_not_found")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_copy_ack_and_matching_list_cannot_complete_wrong_parent(self):
        self.provider.hold_copy = asyncio.Event()
        item = await self.submit()
        await asyncio.wait_for(self.provider.entered.wait(), 2)
        self.provider.items[0]["parent_file_id"] = "wrong-folder"
        self.provider.hold_copy.set()
        async with asyncio.timeout(2):
            while True:
                saved = self.plugin.repo.get(item["operation_id"])
                if saved["data"].get("error") == "cloud_file_verification_mismatch":
                    break
                await asyncio.sleep(0.005)
        self.assertEqual(saved["state"], "verifying")
        self.assertNotIn("cloud_verification", saved["data"])
        self.assertEqual(self.provider.calls.count("copy"), 1)
        self.assertFalse(any("已转存" in n.get("title", "") for n in self.dependencies.notices))
        self.assertIsNone(
            self.store.db.execute(
                "SELECT 1 FROM media_outbox WHERE key=?", ("media-complete:" + item["operation_id"],)
            ).fetchone()
        )

    async def test_reverify_uses_current_mount_without_recopying(self):
        from unittest.mock import AsyncMock

        item = await self.submit()
        await self.wait_state(item["operation_id"], "completed")
        target = self.plugin.settings.targets["movies"]
        target["openlist_path"] = "/mounted-directly/movies"
        self.plugin.openlist.verify = AsyncMock(
            return_value={
                "state": "visible",
                "path_verified": True,
                "path": "/mounted-directly/movies/movie.mkv",
            }
        )
        result = await self.runtime.invoke(MediaRequest("verify", {"operation_id": item["operation_id"]}))
        self.assertEqual(result["cloud_verification"]["state"], "verified")
        self.assertEqual(
            self.plugin.openlist.verify.await_args.args[0]["openlist_path"], target["openlist_path"]
        )
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_download_requires_configured_destination_and_background_resource(self):
        args = {"request_id": str(uuid.uuid4()), "target": "movies", "file_id": "video"}
        with self.assertRaisesRegex(ValueError, "No local"):
            await self.runtime.invoke(MediaRequest("download", args))
        self.dependencies.runs["run"] = {"state": "running", "task_id": "plan", "resources": []}
        with self.assertRaisesRegex(ValueError, "media:movies"):
            await self.runtime.invoke(MediaRequest("download", args, "run"))
        with self.assertRaisesRegex(ValueError, "main conversation"):
            await self.runtime.invoke(MediaRequest("login_status", {"login_id": "main-login"}, "run"))

    async def test_timeout_after_commit_reconciles_without_replaying_copy(self):
        self.provider.copy_error = MediaError("provider_network_error", uncertain=True)
        item = await self.submit()
        pending = await self.wait_state(item["operation_id"], "verifying")
        self.assertTrue(pending["data"]["submitted"])
        self.plugin.repo.change(pending["id"], "verifying", due=0)
        self.plugin.wakeup.set()
        await self.wait_state(pending["id"], "completed")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_restart_recovery_does_not_repeat_external_write(self):
        self.provider.items = [{**VIDEO, "file_id": "saved-before-crash"}]
        item = self.plugin.repo.create(str(uuid.uuid4()), intent(), "assistant")
        self.plugin.repo.change(item["id"], "submitting", file=VIDEO, submitted=True)
        self.plugin.repo.recover()
        self.plugin.wakeup.set()
        done = await self.wait_state(item["id"], "completed")
        self.assertEqual(done["data"]["saved_file_id"], "saved-before-crash")
        self.assertNotIn("copy", self.provider.calls)

    async def test_same_name_different_content_does_not_overwrite(self):
        self.provider.items = [{**VIDEO, "content_hash": "b" * 40}]
        item = await self.submit()
        failed = await self.wait_state(item["operation_id"], "failed")
        self.assertEqual(failed["data"]["error"], "destination_name_conflict")
        self.assertNotIn("copy", self.provider.calls)

    async def test_share_without_hash_is_verified_by_returned_destination_identity(self):
        self.provider.video.pop("content_hash")
        self.provider.video.pop("content_hash_name")
        item = await self.submit()
        done = await self.wait_state(item["operation_id"], "completed")
        self.assertEqual(done["data"]["verification"], "provider_file_id")
        self.assertEqual(self.provider.calls.count("copy"), 1)

    async def test_openlist_failure_does_not_revoke_confirmed_cloud_completion(self):
        from unittest.mock import AsyncMock

        self.plugin.openlist.verify = AsyncMock(side_effect=RuntimeError("private-failure-detail"))
        item = await self.submit()
        done = await self.wait_state(item["operation_id"], "completed")
        await self.plugin.verify_openlist(done)
        done = self.plugin.repo.get(item["operation_id"])
        self.assertEqual(done["data"]["openlist"]["state"], "openlist_unavailable")
        self.assertNotIn("private-failure-detail", json.dumps(self.plugin.repo.public(done)))

    async def test_credentials_saved_before_restart_restore_waiting_work(self):
        self.provider.auth_required = True
        item = await self.submit()
        await self.wait_state(item["operation_id"], "waiting_auth")
        self.provider.auth_required = False
        await self.plugin.restore_authorization()
        await self.wait_state(item["operation_id"], "completed")

    async def test_lost_receipt_without_hash_stays_uncertain_even_if_name_size_match(self):
        self.provider.video.pop("content_hash")
        self.provider.copy_error = MediaError("provider_network_error", uncertain=True)
        item = await self.submit()
        pending = await self.wait_state(item["operation_id"], "verifying")
        self.plugin.repo.change(pending["id"], "verifying", submitted_at=0, due=0)
        self.plugin.wakeup.set()
        await self.wait_state(pending["id"], "uncertain")
        self.assertEqual(self.provider.calls.count("copy"), 1)
        self.assertEqual(len(self.provider.items), 1)

    async def test_same_name_size_without_hash_does_not_identify_preexisting_file(self):
        self.provider.video.pop("content_hash")
        self.provider.items = [{**self.provider.video, "file_id": "unrelated"}]
        item = await self.submit()
        await self.wait_state(item["operation_id"], "failed")
        self.assertNotIn("copy", self.provider.calls)

    async def test_cancel_before_submit_does_not_copy(self):
        self.provider.hold_source = asyncio.Event()
        item = await self.submit()
        await self.provider.entered.wait()
        await self.runtime.invoke(MediaRequest("cancel", {"operation_id": item["operation_id"]}))
        self.provider.hold_source.set()
        await self.wait_state(item["operation_id"], "cancelled")
        self.assertNotIn("copy", self.provider.calls)

    async def test_cancel_after_submit_keeps_cloud_result_and_reports_it(self):
        self.provider.hold_copy = asyncio.Event()
        item = await self.submit()
        await self.provider.entered.wait()
        pending = await self.runtime.invoke(MediaRequest("cancel", {"operation_id": item["operation_id"]}))
        self.assertTrue(pending["cancel_requested"])
        self.assertEqual(pending["state"], "submitting")
        self.provider.hold_copy.set()
        await self.wait_state(item["operation_id"], "completed")
        self.assertEqual(len(self.provider.items), 1)

    async def test_auth_loss_waits_without_occupying_an_ai_execution_and_resumes(self):
        self.provider.auth_required = True
        item = await self.submit()
        await self.wait_state(item["operation_id"], "waiting_auth")
        self.assertEqual(self.plugin.repo.account()["state"], "auth_required")
        self.provider.auth_required = False
        self.plugin.login_completed()
        await self.wait_state(item["operation_id"], "completed")

    async def test_background_target_scope_and_lifecycle_are_checked(self):
        run = {"state": "running", "task_id": "series", "resources": []}
        self.dependencies.runs = {"run": run}
        with self.assertRaisesRegex(ValueError, "media:movies"):
            await self.submit("run")
        run["resources"] = ["media:movies"]
        item = await self.submit("run")
        await self.wait_state(item["operation_id"], "completed")
        with self.assertRaisesRegex(ValueError, "main conversation"):
            await self.runtime.invoke(MediaRequest("login", {}, "run"))
        run["state"] = "completed"
        with self.assertRaisesRegex(ValueError, "active"):
            await self.submit("run")

    async def test_waiting_auth_can_be_cancelled_without_login(self):
        self.provider.auth_required = True
        item = await self.submit()
        await self.wait_state(item["operation_id"], "waiting_auth")
        result = await self.runtime.invoke(MediaRequest("cancel", {"operation_id": item["operation_id"]}))
        self.assertEqual(result["state"], "cancelled")

    async def test_mcp_catalogue_and_background_registration(self):
        class Local:
            async def invoke(local, request):
                return await self.runtime.invoke(request)

        async with Client(create_server(Local())) as client:
            tools = {t.name: t for t in await client.list_tools()}
            self.assertEqual(set(tools), set(MEDIA_TOOLS))
            self.assertTrue(tools["get_media_operation"].annotations.read_only_hint)
            with self.assertRaises(ToolError):
                await client.call_tool(
                    "transfer_media",
                    {
                        "request_id": "bad",
                        "share_url": "https://evil.invalid/s/fake",
                        "file_id": "video",
                        "target": "movies",
                    },
                )
            response = await client.call_tool("media_status", {})
            self.assertIn("targets", response.structured_content)
        configs = server_config("/tmp/socket", task_run="run", media=True)
        self.assertIn("--task-run", configs["media"]["args"])
        self.assertFalse(server_config("/tmp/socket", media=True, enabled=False)["media"]["enabled"])


if __name__ == "__main__":
    unittest.main()
