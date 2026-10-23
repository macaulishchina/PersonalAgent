"""Assistant images remain private, ordered, replayable and visible through HTTP."""

import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from aiohttp import ClientSession
from fastmcp import Client
from PIL import Image

from assistant_runtime.plugins.pi.conversation import task_event
from assistant_runtime.kernel.contracts import VideoFrame
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.mcp.tasks import create_server
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
from assistant_runtime.plugins.tasks.repository import TaskRepository
from test_conversation import fixture, Owned
import test_web


def picture():
    image = io.BytesIO()
    Image.new("RGB", (64, 48), "blue").save(image, format="PNG")
    return {"type": "image", "mimeType": "image/png", "data": base64.b64encode(image.getvalue()).decode()}


def provider_item(blocks, ident="image-tool"):
    return {
        "run_id": "web-turn",
        "item": {
            "type": "mcp",
            "id": ident,
            "status": "completed",
            "tool": "capture_image",
            "result": {"content": blocks},
        },
    }


class ProviderImageTest(unittest.TestCase):
    def test_historical_task_results_restore_only_the_selected_runs_media(self):
        ref = {"id": "a" * 32, "name": "snapshot.jpg", "type": "image/jpeg"}
        value = {"runs": [{"data": {"attachments": [ref]}}, {"data": {"attachments": [{"id": "b" * 32}]}}]}
        event = provider_item([{"type": "text", "text": json.dumps(value)}])
        event["item"].update(server="tasks", tool="get_task")
        self.assertEqual(task_event("activity.completed", event)["attachments"], [ref])
        event["item"]["server"] = "other"
        self.assertNotIn("attachments", task_event("activity.completed", event))
        event["item"]["server"] = "tasks"
        value["runs"][0]["data"]["attachments"] = []
        event["item"]["result"]["content"][0]["text"] = json.dumps(value)
        self.assertNotIn("attachments", task_event("activity.completed", event))

    def test_direct_and_wrapped_content_share_the_same_image_contract(self):
        native = picture()
        wrapped = {"type": "text", "text": json.dumps({"content": [native], "isError": False})}
        for blocks in ([native], [wrapped]):
            event = task_event("activity.completed", provider_item(blocks))
            self.assertEqual(event["images"], [{"data_base64": native["data"], "mime_type": "image/png"}])
            self.assertNotIn(native["data"], event["text"])
        self.assertNotIn("images", task_event("activity.started", provider_item([native])))

    def test_non_image_malformed_and_untrusted_text_does_not_open_paths(self):
        for result in (
            {"content": [{"type": "text", "text": "{/tmp/private.png"}]},
            {"content": [{"type": "text", "text": '[{"type":"image"}]'}]},
            {"content": ["malformed"]},
            None,
        ):
            event = provider_item([])
            event["item"]["result"] = result
            self.assertNotIn("images", task_event("activity.completed", event))

    def test_excessively_nested_tool_json_does_not_interrupt_event_projection(self):
        nested = '{"value":' * 10000 + "0" + "}" * 10000
        item = provider_item([{"type": "text", "text": nested}])
        event = task_event("activity.completed", item)
        self.assertEqual(event["kind"], "message")
        self.assertNotIn("images", event)


class ReplyImageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = fixture(Path(self.temp.name))
        self.host.config = replace(self.host.config, web={"enable": True})
        self.context = Owned()
        await self.host.conversation.start(self.context)

    async def asyncTearDown(self):
        await self.context.close()
        self.host.store.db.close()
        self.temp.cleanup()

    async def test_replay_keeps_one_private_attachment_ordinal_timestamp_and_empty_transcripts(self):
        c = self.host.conversation
        event = task_event("activity.completed", provider_item([picture()]))
        c.provider_event(event)
        before = c.store.get("task:session:image-tool")
        cursor = c.store.cursor()
        await asyncio.wait_for(c.image_queue.join(), 2)
        message = c.store.get(before["id"])
        asset = c.media.folder / message["attachments"][0]["id"]
        with Image.open(asset) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (64, 48))
        self.assertEqual(asset.stat().st_mode & 0o777, 0o600)
        self.assertEqual(message["ordinal"], before["ordinal"])
        self.assertEqual(message["updated"], before["updated"])
        changes = c.store.changes(c.session, cursor)
        self.assertEqual(changes[-1]["data"]["attachments"], message["attachments"])
        c.provider_event(event)
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertEqual(c.store.db.execute("SELECT count(*) FROM chat_uploads").fetchone()[0], 1)
        self.assertEqual(self.host.store.max_transcript(), 0)
        self.host.backend.submit.assert_not_awaited()
        self.host.voice.say.assert_not_awaited()

    async def test_restart_recovers_existing_tool_picture_without_recapture(self):
        c = self.host.conversation
        wrapped = {"type": "text", "text": json.dumps({"content": [picture()]})}
        event = task_event("activity.completed", provider_item([wrapped]))
        c.store.put(
            "task:session:image-tool",
            c.session,
            kind="detail",
            text="调用工具\ncapture_image",
            status="complete",
        )
        old = c.store.get("task:session:image-tool")
        self.host.backend.restored_conversation_events = lambda: [event]
        await self.context.close()
        self.context = Owned()
        await c.start(self.context)
        await asyncio.wait_for(c.image_queue.join(), 2)
        message = c.store.get(old["id"])
        self.assertEqual(len(message["attachments"]), 1)
        self.assertEqual(message["ordinal"], old["ordinal"])
        self.assertEqual(message["updated"], old["updated"])
        self.host.backend.submit.assert_not_awaited()

    async def test_corrupt_image_is_visible_failure_and_does_not_kill_worker(self):
        c = self.host.conversation
        bad = picture() | {"data": "not base64"}
        c.provider_event(task_event("activity.completed", provider_item([bad], "bad")))
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertIn("未能显示", c.store.get("image-error:task:session:bad")["text"])
        c.provider_event(task_event("activity.completed", provider_item([picture(), bad], "mixed")))
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertEqual(len(c.store.get("task:session:mixed")["attachments"]), 1)
        self.assertTrue(c.store.get("image-error:task:session:mixed"))

    async def test_voice_only_mode_does_not_start_persisting_camera_images(self):
        c = self.host.conversation
        self.host.config = replace(self.host.config, web={})
        c.provider_event(task_event("activity.completed", provider_item([picture()])))
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertFalse(c.media.folder.exists())

    async def test_image_recovery_updates_previous_failure_notice(self):
        c = self.host.conversation
        bad = picture() | {"data": "not base64"}
        c.provider_event(task_event("activity.completed", provider_item([bad], "recover")))
        await asyncio.wait_for(c.image_queue.join(), 2)
        notice = "image-error:task:session:recover"
        self.assertIn("未能显示", c.store.get(notice)["text"])
        c.provider_event(task_event("activity.completed", provider_item([picture()], "recover")))
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertEqual(len(c.store.get("task:session:recover")["attachments"]), 1)
        self.assertIn("已恢复", c.store.get(notice)["text"])

    async def test_durable_reference_supersedes_pending_invalid_image(self):
        c = self.host.conversation
        raw = base64.b64decode(picture()["data"])
        reference = await c.media.add_image(raw, "retained.jpg", key="retained")
        bad = picture() | {"data": "truncated"}
        event = task_event("activity.completed", provider_item([bad], "late-ref"))
        c.provider_event(event)
        c.provider_event(event | {"attachments": [reference]})
        await asyncio.wait_for(c.image_queue.join(), 2)
        self.assertEqual(c.store.get("task:session:late-ref")["attachments"], [reference])
        self.assertIsNone(c.store.get("image-error:task:session:late-ref"))

    async def test_capture_reference_survives_truncated_provider_content_without_resaving_fragment(self):
        c = self.host.conversation
        data = io.BytesIO()
        Image.effect_noise((640, 480), 100).convert("RGB").save(data, format="JPEG", quality=92)
        frame = VideoFrame("camera", data.getvalue(), 640, 480, 100, 1)
        reply = await c.capture_reply(frame)
        self.assertGreater(len(reply["frame"]["data_base64"]), 65000)
        ref = reply["attachments"][0]
        metadata = {"frame": {"device_id": "camera"}, "attachments": [ref]}
        clipped = {"type": "image", "mimeType": "image/jpeg", "data": "<truncated>"}
        blocks = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "content": [
                            {"type": "text", "text": json.dumps(metadata)},
                            clipped,
                        ]
                    }
                ),
            }
        ]
        event = task_event("activity.completed", provider_item(blocks))
        self.assertEqual(event["attachments"], [ref])
        c.provider_event(event)
        await asyncio.wait_for(c.image_queue.join(), 2)
        message = c.store.get("task:session:image-tool")
        self.assertEqual(message["attachments"], [ref])
        self.assertIsNone(c.store.get("image-error:task:session:image-tool"))
        with Image.open(c.media.folder / ref["id"]) as image:
            self.assertEqual(image.size, (640, 480))
        self.assertEqual(c.store.db.execute("SELECT attached FROM chat_uploads").fetchone()[0], 1)

    async def test_forged_reference_cannot_serve_an_arbitrary_local_file(self):
        c = self.host.conversation
        event = task_event("activity.completed", provider_item([])) | {
            "attachments": [{"id": "../../config.toml"}]
        }
        c.provider_event(event)
        self.assertEqual(c.store.get("task:session:image-tool")["attachments"], [])

    async def test_cancelled_image_prepare_is_finished_before_cleanup_without_late_asset(self):
        c = self.host.conversation
        started, finish = asyncio.Event(), asyncio.Event()

        async def blocked(fn, *args, **kwargs):
            started.set()
            await finish.wait()
            return fn(*args, **kwargs)

        raw = base64.b64decode(picture()["data"])
        with patch("assistant_runtime.conversation.media.asyncio.to_thread", side_effect=blocked):
            task = asyncio.create_task(c.media.add_image(raw, "cancelled", key="cancel"))
            await started.wait()
            task.cancel()
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(list(c.media.folder.iterdir()))
        self.assertEqual(c.store.db.execute("SELECT count(*) FROM chat_uploads").fetchone()[0], 0)


class ReplyImageHttpTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_web.WebTest.asyncSetUp
    asyncTearDown = test_web.WebTest.asyncTearDown
    login = test_web.WebTest.login

    async def test_background_task_picture_crosses_mcp_and_survives_reload_without_recapture(self):
        await self.login()
        c = self.host.conversation
        ref = await c.media.add_image(base64.b64decode(picture()["data"]), "snapshot.jpg")
        owner = object.__new__(TasksPlugin)
        owner.host = self.host
        owner.repo = TaskRepository(self.host.store.db)
        from assistant_runtime.plugins.tasks.feedback import TaskFeedback

        owner.feedback = TaskFeedback(owner)
        ident = uuid.uuid4().hex
        spec = {
            "title": "requested snapshot",
            "prompt": "snapshot",
            "trigger": {"kind": "immediate"},
            "notify_completion": False,
        }
        self.host.store.db.execute(
            "INSERT INTO job_definitions(id,request_id,fingerprint,state,spec,created,updated) VALUES(?,?,?,'archived',?,?,?)",
            (ident, str(uuid.uuid4()), "snapshot", json.dumps(spec), 1, 1),
        )
        run = owner.repo.enqueue(
            owner.repo.definition(ident),
            "snapshot",
            "completed",
            {"attachments": [ref, {"id": "../../config.toml"}], "progress": "captured"},
        )
        # Exercise the actual task facade rather than inventing foreground image bytes.
        async with Client(create_server(SimpleNamespace(invoke=owner.request))) as mcp:
            response = await mcp.call_tool("get_task", {"task_id": ident})
        value = response.structured_content
        self.assertEqual(value["attachments"], [ref])
        self.assertEqual(value["attachment_run_id"], run["id"])
        event = provider_item([{"type": "text", "text": json.dumps(value)}], ident="get-snapshot")
        event["item"].update(server="tasks", tool="get_task")
        c.provider_event(task_event("activity.completed", event))
        message = c.store.get("task:session:get-snapshot")
        self.assertEqual(message["attachments"], [ref])
        response = await self.client.get(self.base + "/api/uploads/" + ref["id"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        response = await self.client.get(self.base + "/api/messages")
        saved = next(m for m in (await response.json())["messages"] if m["id"] == message["id"])
        self.assertEqual(saved["attachments"], [ref])
        before = (message["ordinal"], message["updated"])
        c.provider_event(task_event("activity.completed", event), restored=True)
        replay = c.store.get(message["id"])
        self.assertEqual((replay["ordinal"], replay["updated"]), before)
        self.assertEqual(self.host.store.db.execute("SELECT count(*) FROM chat_uploads").fetchone()[0], 1)
        self.assertEqual(self.host.store.db.execute("SELECT attached FROM chat_uploads").fetchone()[0], 1)
        newer = owner.repo.enqueue(owner.repo.definition(ident), "empty-later", "completed")
        empty = await owner.request(TaskRequest("get", {"task_id": ident}))
        self.assertEqual(empty["attachment_run_id"], newer["id"])
        self.assertEqual(empty["attachments"], [])

    async def test_reply_attachment_is_authorized_replays_after_reload_and_is_not_expired(self):
        await self.login()
        c = self.host.conversation
        c.provider_event(task_event("activity.completed", provider_item([picture()])))
        await asyncio.wait_for(c.image_queue.join(), 2)
        asset = c.store.get("task:session:image-tool")["attachments"][0]
        url = self.base + "/api/uploads/" + asset["id"]
        response = await self.client.get(url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        response = await self.client.get(self.base + "/api/messages")
        self.assertEqual((await response.json())["messages"][-1]["attachments"][0], asset)
        async with ClientSession() as other:
            response = await other.get(url)
            self.assertEqual(response.status, 401)
        c.store.db.execute("UPDATE chat_uploads SET created=0")
        sweep = asyncio.create_task(self.plugin.cleanup_uploads())
        await asyncio.sleep(0.02)
        sweep.cancel()
        await asyncio.gather(sweep, return_exceptions=True)
        self.assertTrue((c.media.folder / asset["id"]).exists())
