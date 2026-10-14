"""Camera integration contracts: explicit acquisition, image delivery and cleanup."""

import asyncio
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from assistant_runtime.config import Config
from assistant_runtime.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.devices.video import VideoInputs
from assistant_runtime.kernel.contracts import VideoCapture, VideoFrame, DeviceQuery
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.v4l2.camera import V4L2Camera, validate_connection
from assistant_runtime.plugins.v4l2.plugin import V4L2Plugin
from assistant_runtime.store import Store


def camera_spec():
    return {
        "adapter": "v4l2",
        "kind": "camera",
        "label": "测试摄像头",
        "location": "客厅",
        "connection": {
            "path": "/dev/v4l/by-id/usb-example-serial-video-index0",
            "width": 640,
            "height": 480,
        },
    }


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (640, 480), "red").save(stream, format="JPEG")
    return stream.getvalue()


def frame(ident="camera"):
    return VideoFrame(ident, jpeg(), 640, 480, time.time(), 1)


class VideoRegistrationTest(unittest.IsolatedAsyncioTestCase):
    def test_stable_device_identity_and_bounded_connection(self):
        for values in (
            {"path": "/dev/video0"},
            {"path": "/dev/v4l/by-id/../../video0"},
            {"path": "/dev/v4l/by-id/usb-camera-video-index1"},
            {"width": True},
            {"width": 2560},
            {"height": 1440},
            {"framerate": 60},
            {"input_format": "file"},
            {"timeout_seconds": 0},
            {"unknown": True},
        ):
            spec = camera_spec()
            spec["connection"].update(values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_connection(spec)
        validate_connection(camera_spec())

    async def test_owner_role_disabled_unknown_and_frame_identity(self):
        devices = {"camera": camera_spec()}
        registry = VideoInputs(devices)
        provider = AsyncMock()
        provider.capture.return_value = frame()
        registry.register("v4l2", "camera", provider)
        for owner in ("other", "v4l2"):
            with self.assertRaises(ValueError):
                registry.register(owner, "camera", provider)
        result = await registry.capture(VideoCapture("camera"))
        self.assertEqual(result.device_id, "camera")
        with self.assertRaises(ValueError):
            await registry.capture(VideoCapture("unknown"))
        provider.capture.return_value = frame("other")
        with self.assertRaisesRegex(RuntimeError, "identity"):
            await registry.capture(VideoCapture("camera"))
        for change in ({"kind": "microphone"}, {"enabled": False}):
            changed = copy.deepcopy(devices)
            changed["camera"].update(change)
            with self.assertRaises(ValueError):
                VideoInputs(changed).register("v4l2", "camera", provider)

    async def test_query_and_observation_do_not_capture_or_publish_images(self):
        config = Config(profile="headless", plugins=["v4l2"], devices={"camera": camera_spec()})
        directory, video = DeviceDirectory(config.devices), VideoInputs(config.devices)
        plugin = V4L2Plugin(config, directory, video)
        camera = plugin.providers["camera"]
        camera.inspect = AsyncMock(return_value={"capturing": False, "capture_mode": "on_request"})
        camera.capture = AsyncMock(return_value=frame())
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            runtime = Runtime(store)
            runtime.add(plugin)
            runtime.add(DevicesPlugin(directory, video))
            events = runtime.subscribe("test")
            await runtime.start()
            await runtime.invoke(DeviceQuery("inspect", "camera"))
            self.assertEqual(directory.snapshot("camera")["state"], "available")
            camera.capture.assert_not_called()
            result = await runtime.invoke(VideoCapture("camera"))
            self.assertEqual(result.data, jpeg())
            self.assertNotIn("connection", directory.snapshot("camera"))
            while not events.empty():
                event = events.get_nowait()
                self.assertNotIn("data_base64", event.data)
                self.assertNotIn("data", event.data)
                self.assertNotEqual(event.kind, "assistant.triggered")
                json.dumps(event.data)
            camera.inspect.side_effect = FileNotFoundError("camera unplugged")
            result = await runtime.invoke(DeviceQuery("inspect", "camera"))
            self.assertEqual(result["device"]["state"], "unavailable")
            camera.inspect.side_effect = None
            result = await runtime.invoke(DeviceQuery("inspect", "camera"))
            self.assertEqual(result["device"]["state"], "available")
            await runtime.close()
            store.db.close()


class CameraCaptureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.camera = V4L2Camera("camera", camera_spec())
        self.camera.node_identity = lambda: (1, 2, 3)
        self.camera.execute = AsyncMock(return_value=(0, jpeg(), ""))

    async def test_validated_frame_is_fresh_and_never_retained(self):
        before = time.time()
        result = await self.camera.capture()
        self.assertEqual((result.width, result.height), (640, 480))
        self.assertGreaterEqual(result.captured_at, before)
        self.assertEqual(self.camera.capture_count, 1)
        self.assertFalse(self.camera.lock.locked())
        self.assertFalse(any(isinstance(v, bytes) for v in vars(self.camera).values()))
        args = self.camera.execute.call_args.args[0]
        self.assertIn("/dev/v4l/by-id/usb-example-serial-video-index0", args)
        self.assertEqual(args[args.index("-frames:v") + 1], "1")

    async def test_failed_or_malformed_frame_never_reuses_last_success(self):
        await self.camera.capture()
        for code, data, error in ((1, b"", "Permission denied"), (0, b"invalid", "")):
            self.camera.execute.return_value = (code, data, error)
            with self.assertRaises((RuntimeError, OSError)):
                await self.camera.capture()
            self.assertEqual(self.camera.capture_count, 1)
            self.assertIsNotNone(self.camera.last_error)
        self.camera.execute.return_value = (0, jpeg(), "")
        await self.camera.capture()
        self.assertIsNone(self.camera.last_error)

    async def test_concurrent_acquisition_rejected_and_stop_invalidates_old_frame(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            started.set()
            await release.wait()
            return 0, jpeg(), ""

        self.camera.execute.side_effect = blocked
        task = asyncio.create_task(self.camera.capture())
        await started.wait()
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.camera.capture()
        await self.camera.close()
        release.set()
        with self.assertRaisesRegex(RuntimeError, "generation"):
            await task
        self.assertEqual(self.camera.capture_count, 0)

    async def test_wrong_dimensions_and_changed_physical_node_are_rejected(self):
        self.camera.width = 1280
        with self.assertRaisesRegex(ValueError, "dimensions"):
            await self.camera.capture()
        self.camera.width = 640
        identities = iter(((1, 2, 3), (1, 4, 5)))
        self.camera.node_identity = lambda: next(identities)
        with self.assertRaisesRegex(RuntimeError, "identity"):
            await self.camera.capture()

    async def test_probe_checks_requested_format_and_size_on_same_line(self):
        self.camera.execute.return_value = (1, b"", "Compressed: mjpeg 1280x720\nRaw: yuyv422 640x480")
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            await self.camera.inspect()
        self.camera.execute.return_value = (1, b"", "Compressed: mjpeg 1280x720 640x480")
        state = await self.camera.inspect()
        self.assertTrue(state["formats_checked"])
        self.assertFalse(state["capturing"])
        self.assertEqual(self.camera.capture_count, 0)


class CameraProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_overflow_and_cancel_reap_processes(self):
        for mode in ("timeout", "overflow", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                executable = Path(folder) / "ffmpeg"
                executable.write_text(
                    "#!/usr/bin/env python3\nimport sys,time\n"
                    + (
                        "sys.stdout.buffer.write(b'x'*10000);sys.stdout.flush()\n"
                        if mode == "overflow"
                        else ""
                    )
                    + "time.sleep(60)\n"
                )
                executable.chmod(0o700)
                spec = camera_spec()
                spec["connection"]["timeout_seconds"] = 0.15
                camera = V4L2Camera("camera", spec)
                with patch(
                    "assistant_runtime.plugins.v4l2.camera.get_ffmpeg_exe", return_value=str(executable)
                ):
                    task = asyncio.create_task(camera.execute([], 100))
                    if mode == "cancel":
                        while not camera.processes:
                            await asyncio.sleep(0.001)
                        task.cancel()
                    with self.assertRaises((TimeoutError, ValueError, asyncio.CancelledError)):
                        await task
                self.assertFalse(camera.processes)


class CameraMcpTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_is_real_mcp_content_not_embedded_json(self):
        from fastmcp import Client
        from assistant_runtime.mcp.device import create_server

        payload = {
            "frame": {
                "device_id": "camera",
                "width": 640,
                "height": 480,
                "mime_type": "image/jpeg",
                "captured_at": time.time(),
                "generation": 1,
                "data_base64": base64.b64encode(jpeg()).decode(),
            }
        }
        boundary = AsyncMock()
        boundary.invoke.return_value = payload
        async with Client(create_server(boundary)) as client:
            result = await client.call_tool("capture_image", {"device_id": "camera"})
        boundary.invoke.assert_awaited_once_with(VideoCapture("camera"))
        images = [block for block in result.content if block.type == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0].data), jpeg())
        self.assertNotIn("data_base64", result.structured_content["frame"])


if __name__ == "__main__":
    unittest.main()
