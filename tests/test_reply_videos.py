"""Private recording attachments appear live and serve authenticated seek ranges."""

from platform_support import private_mode

from dataclasses import replace
import json
import time
import unittest
import uuid

from aiohttp import ClientSession

from assistant_runtime.api.contracts import VideoClip
from assistant_runtime.core.pi.conversation import task_event
from test_reply_images import provider_item
from test_video_recording import sample_video
import test_web


class ReplyVideoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.web = test_web.WebTest()
        await self.web.asyncSetUp()
        self.host = self.web.host
        self.host.config.devices["camera"] = {"label": "测试摄像头"}

    async def asyncTearDown(self):
        await self.web.asyncTearDown()

    def clip(self):
        path = self.host.config.state_dir / "video-recordings" / (uuid.uuid4().hex + ".mp4")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        sample_video(path)
        path.chmod(0o600)
        return VideoClip("camera", path, 160, 96, 2, path.stat().st_size, time.time(), 1)

    async def test_tool_reply_is_live_replayable_and_requires_login_for_playback_and_seek(self):
        c = self.host.conversation
        cursor = c.store.cursor()
        clip = self.clip()
        result = await c.recording_reply(clip)
        self.assertNotIn("path", result["video"])
        self.assertFalse(clip.path.exists())
        item = provider_item([{"type": "text", "text": json.dumps(result)}], ident="clip-tool")
        item["item"]["tool"] = "record_video"
        event = task_event("activity.completed", item)
        c.provider_event(event)
        message = c.store.get("task:session:clip-tool")
        attachment = message["attachments"][0]
        self.assertEqual(attachment["type"], "video/mp4")
        self.assertEqual(
            c.store.changes(c.session, cursor)[-1]["data"]["attachments"], message["attachments"]
        )
        c.provider_event(event)
        self.assertEqual(c.store.get(message["id"])["ordinal"], message["ordinal"])
        path = c.media.folder / attachment["id"]
        self.assertTrue(path.is_file())
        self.assertEqual(private_mode(path), 0o600)
        url = self.web.base + "/api/uploads/" + attachment["id"]
        async with ClientSession() as guest:
            response = await guest.get(url)
            self.assertEqual(response.status, 401)
        await self.web.login()
        response = await self.web.client.get(url, headers={"Range": "bytes=0-99"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.content_type, "video/mp4")
        self.assertEqual(len(await response.read()), 100)
        response = await self.web.client.head(url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_length, path.stat().st_size)
        self.assertIn("media-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(
            (await (await self.web.client.get(self.web.base + "/api/messages")).json())["messages"][-1][
                "attachments"
            ],
            message["attachments"],
        )
        self.host.backend.submit.assert_not_awaited()
        self.host.voice.say.assert_not_awaited()

    async def test_video_cannot_be_forged_from_an_arbitrary_file_or_sent_as_an_image(self):
        c = self.host.conversation
        clip = self.clip()
        outside = self.host.config.state_dir / (uuid.uuid4().hex + ".mp4")
        clip.path.replace(outside)
        with self.assertRaisesRegex(ValueError, "source"):
            await c.recording_reply(replace(clip, path=outside))
        self.assertTrue(outside.exists())
        clip = self.clip()
        result = await c.recording_reply(clip)
        ident = result["attachments"][0]["id"]
        with self.assertRaisesRegex(ValueError, "图片"):
            await c.submit(str(uuid.uuid4()), "重新发送视频", [ident])
        c.provider_event(
            {
                "kind": "message",
                "message_id": "forged",
                "turn_id": "run",
                "text": "fake",
                "detail": False,
                "status": "complete",
                "attachments": [{"id": "/etc/passwd", "type": "video/mp4"}],
            }
        )
        self.assertEqual(c.store.get("task:session:forged")["attachments"], [])
