"""Camera integration contracts: explicit acquisition, image delivery and cleanup."""

import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from PIL import Image

from assistant_runtime.config import Config
from assistant_runtime.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.devices.video import VideoInputs
from assistant_runtime.kernel.contracts import VideoCapture, VideoFrame, DeviceQuery
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.go2rtc.camera import validate_connection
from assistant_runtime.plugins.go2rtc.plugin import Go2RTCPlugin
from assistant_runtime.store import Store


def camera_spec():
    return {
        "adapter": "go2rtc",
        "kind": "camera",
        "label": "测试摄像头",
        "location": "客厅",
        "connection": {
            "base_url": "http://127.0.0.1:8553",
            "stream": "camera",
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
            {"base_url": "file:///dev/video0"},
            {"base_url": "http://admin:password@localhost:8553"},
            {"base_url": "http://localhost:8553/api"},
            {"base_url": "http://localhost:8553?src=other"},
            {"base_url": "http://localhost:8553#fragment"},
            {"stream": "rtsp://camera/live"},
            {"stream": "../camera"},
            {"timeout_seconds": True},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": 0},
            {"username": "admin"},
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
        registry.register("go2rtc", "camera", provider)
        for owner in ("other", "go2rtc"):
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
                VideoInputs(changed).register("go2rtc", "camera", provider)

    async def test_query_and_observation_do_not_capture_or_publish_images(self):
        config = Config(profile="headless", plugins=["go2rtc"], devices={"camera": camera_spec()})
        directory, video = DeviceDirectory(config.devices), VideoInputs(config.devices)
        plugin = Go2RTCPlugin(config, directory, video)
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
