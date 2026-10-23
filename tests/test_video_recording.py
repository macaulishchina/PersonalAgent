"""Real MP4 finalization with a fake gateway: bounds, source changes and cleanup."""

import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import imageio_ffmpeg
import yaml

from assistant_runtime.api.contracts import VideoRecord
from assistant_runtime.plugins.go2rtc.camera import Go2RTCCamera
from assistant_runtime.plugins.go2rtc.recording import inspect_clip
from assistant_runtime.app.host import AssistantService
from assistant_runtime.mcp.client import LocalRuntimeClient


def sample_video(path):
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x96:rate=10",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "frag_keyframe+empty_moov",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.TemporaryDirectory()
        cls.sample = sample_video(Path(cls.samples.name) / "sample.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.samples.cleanup()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "video-recordings"
        self.source = "rtsp://private-password@camera/live"
        self.data = self.sample
        self.aliases = set()
        self.deleted = []
        self.block = False
        self.change = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/api/streams", self.metadata)
        app.router.add_delete("/api/streams", self.delete)
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/stream.mp4", self.video)
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.camera = Go2RTCCamera(
            "camera", {"connection": {"base_url": base, "stream": "camera"}}, self.folder
        )
        await self.camera.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.camera.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def metadata(self, request):
        return web.json_response({"producers": [{"url": self.source}]})

    async def config(self, request):
        return web.Response(text=yaml.safe_dump({"streams": {"camera": self.source}}))

    async def delete(self, request):
        name = request.query["src"]
        self.deleted.append(name)
        self.aliases.discard(name)
        return web.Response()

    async def video(self, request):
        self.assertEqual(request.query["src"], "ffmpeg:camera#video=h264#width=1280#height=720")
        self.assertEqual(request.query["video"], "h264")
        self.aliases.add(request.query["name"])
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        await response.prepare(request)
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.change:
            self.source += "-changed"
        try:
            await response.write(self.data)
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    async def test_completed_clip_is_playable_and_temporary_stream_is_removed(self):
        clip = await self.camera.record(2)
        self.assertEqual(clip.device_id, "camera")
        self.assertEqual((clip.width, clip.height), (160, 96))
        self.assertAlmostEqual(clip.duration_seconds, 2, delta=0.2)
        self.assertEqual(await asyncio.to_thread(inspect_clip, clip.path), (160, 96, clip.duration_seconds))
        self.assertEqual(clip.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.aliases)
        self.assertEqual(len(self.deleted), 1)
        self.assertFalse(self.camera.lock.locked())
        self.assertFalse(self.camera.operations)
        self.assertEqual(list(self.folder.iterdir()), [clip.path])

    async def test_cancellation_releases_acquisition_and_never_publishes_partial_video(self):
        self.block = True
        task = asyncio.create_task(self.camera.record(2))
        await asyncio.wait_for(self.started.wait(), 2)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.camera.capture()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.aliases)
        self.assertFalse(list(self.folder.iterdir()))
        self.assertFalse(self.camera.operations)

    async def test_invalid_or_oversized_or_changed_source_never_returns_a_clip(self):
        self.data = b"broken mp4"
        with self.assertRaises(ValueError):
            await self.camera.record(2)
        self.data = self.sample
        with patch("assistant_runtime.plugins.go2rtc.recording.MAX_VIDEO_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "64 MiB"):
                await self.camera.record(2)
        self.change = True
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            await self.camera.record(2)
        self.assertFalse(self.aliases)
        self.assertFalse(list(self.folder.iterdir()))

    def test_duration_contract_rejects_implicit_or_unbounded_recordings(self):
        for duration in (0, 61, True, 1.5, "10"):
            with self.assertRaises(ValueError):
                VideoRecord("camera", duration)
        self.assertEqual(VideoRecord("camera").duration_seconds, 10)

    async def test_canceling_real_ipc_request_cancels_capture_before_attachment_publication(self):
        service = object.__new__(AssistantService)
        from assistant_runtime.api.lifecycle import WorkIntake

        service.intake = WorkIntake()
        canceled, started = asyncio.Event(), asyncio.Event()

        async def capture(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        service.runtime = SimpleNamespace(invoke=capture)
        service.conversation = SimpleNamespace(recording_reply=AsyncMock())
        socket = Path(self.temp.name) / "recording.sock"
        server = await asyncio.start_unix_server(service._connection, path=socket)
        try:
            task = asyncio.create_task(LocalRuntimeClient(socket).invoke(VideoRecord("camera", 2)))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(canceled.wait(), 2)
            service.conversation.recording_reply.assert_not_awaited()
        finally:
            server.close()
            await server.wait_closed()
