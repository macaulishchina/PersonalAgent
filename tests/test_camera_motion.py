"""Camera actuation: capability discovery, cancellation, bounds and no replay."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from aiohttp import web
import yaml

from assistant_runtime.app.config import Config
from assistant_runtime.core.devices.cameras import CameraControls
from assistant_runtime.core.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.core.devices.video import VideoInputs
from assistant_runtime.api.contracts import CameraMotion, DeviceQuery
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.plugins.go2rtc.camera import Go2RTCCamera, GatewayUnavailable
from assistant_runtime.plugins.go2rtc.plugin import Go2RTCPlugin
from assistant_runtime.plugins.go2rtc.ptz import Go2RTCPanTilt
from assistant_runtime.app.store import Store


class CameraMotionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source = "xiaomi://private-account:cn@camera?did=private-device"
        self.moves = []
        self.media_count = 0
        self.total_media = 0
        self.block = False
        self.fail_stop = False
        self.change_source = False
        self.move_started = asyncio.Event()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/api/streams", self.metadata)
        app.router.add_get("/api/config", self.configuration)
        app.router.add_get("/api/stream.mp4", self.media)
        app.router.add_post("/api/xiaomi/ptz", self.motion)
        self.runner = web.AppRunner(app, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        self.camera = Go2RTCCamera("camera", self.spec())
        await self.camera.start()
        self.controller = Go2RTCPanTilt(self.camera)

    def spec(self):
        return {
            "adapter": "go2rtc",
            "kind": "camera",
            "label": "云台",
            "connection": {
                "base_url": self.base,
                "stream": "camera",
                "pan_tilt": {"axes": ["pan", "tilt"], "max_duration_ms": 300},
            },
        }

    async def asyncTearDown(self):
        self.release.set()
        await self.controller.close()
        await self.camera.close()
        await self.runner.cleanup()

    async def metadata(self, request):
        return web.json_response({"producers": [{"format_name": "xiaomi/miss"}]})

    async def configuration(self, request):
        return web.Response(text=yaml.safe_dump({"streams": {"camera": self.source}}))

    async def media(self, request):
        self.media_count += 1
        self.total_media += 1
        if self.change_source:
            self.source += "-changed"
        response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
        try:
            await response.prepare(request)
            while True:
                await response.write(b"media")
                await asyncio.sleep(0.01)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.media_count -= 1
        return response

    async def motion(self, request):
        self.assertGreater(self.media_count, 0, "Motor commands require the same live media connection")
        direction = request.query["direction"]
        self.moves.append(direction)
        if direction != "stop":
            self.move_started.set()
            if self.block:
                await self.release.wait()
        if direction == "stop" and self.fail_stop:
            return web.Response(status=503, text=self.source)
        return web.json_response(
            {"src": "camera", "direction": direction, "duration": int(request.query["duration"])}
        )

    async def test_short_steps_keep_media_alive_and_always_stop_without_position_claims(self):
        for direction in ("left", "right", "up", "down"):
            result = await self.controller.move(CameraMotion("camera", direction))
            self.assertEqual(result["direction"], direction)
            self.assertTrue(result["command_sent"])
            self.assertTrue(result["stop_sent"])
            self.assertFalse(result["position_verified"])
            self.assertEqual(self.moves[-2:], [direction, "stop"])
        self.assertFalse(self.controller.operations)

    async def test_invalid_fixed_or_unsupported_requests_never_reach_hardware(self):
        for direction, duration in [("left", 0), ("left", 501), ("stop", 80), ("left", True), ("zoom", 80)]:
            with self.assertRaises(ValueError):
                CameraMotion("camera", direction, duration)
        self.controller.axes = ("pan",)
        for request in (CameraMotion("camera", "up"), CameraMotion("camera", "left", 301)):
            with self.assertRaises(ValueError):
                await self.controller.move(request)
        controls = CameraControls({"fixed": self.spec()})
        with self.assertRaisesRegex(ValueError, "does not support"):
            await controls.move(CameraMotion("fixed", "left"))
        self.assertEqual(self.moves, [])
        self.assertEqual(self.total_media, 0)

    async def test_concurrent_move_is_rejected_and_stop_preempts_with_no_late_cleanup(self):
        self.block = True
        moving = asyncio.create_task(self.controller.move(CameraMotion("camera", "left")))
        await asyncio.wait_for(self.move_started.wait(), 2)
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await self.controller.move(CameraMotion("camera", "right"))
        result = await self.controller.move(CameraMotion("camera", "stop", 0))
        self.assertTrue(result["stop_sent"])
        with self.assertRaises(asyncio.CancelledError):
            await moving
        self.assertEqual(self.moves, ["left", "stop"])
        self.block = False
        await self.controller.move(CameraMotion("camera", "right"))
        self.assertEqual(self.moves, ["left", "stop", "right", "stop"])

    async def test_timeout_and_close_stop_owned_work_without_replaying_movement(self):
        self.camera.timeout = 0.1
        self.block = True
        with self.assertRaises(GatewayUnavailable):
            await self.controller.move(CameraMotion("camera", "left"))
        self.assertEqual(self.moves, ["left", "stop"])
        self.assertFalse(self.controller.operations)
        self.assertIsNone(self.controller.active)
        self.camera.timeout = 8
        self.move_started.clear()
        moving = asyncio.create_task(self.controller.move(CameraMotion("camera", "up")))
        await asyncio.wait_for(self.move_started.wait(), 2)
        await self.controller.close()
        with self.assertRaises(asyncio.CancelledError):
            await moving
        self.assertEqual(self.moves, ["left", "stop", "up", "stop"])

    async def test_stop_failure_is_visible_and_never_turns_into_movement_retry(self):
        self.fail_stop = True
        with self.assertRaisesRegex(GatewayUnavailable, "stop unconfirmed") as failure:
            await self.controller.move(CameraMotion("camera", "left"))
        self.assertEqual(self.moves, ["left", "stop"])
        self.assertNotIn("private-account", str(failure.exception))
        self.assertIsNotNone(self.controller.state()["last_error"])

    async def test_source_change_before_actuation_is_rejected(self):
        self.change_source = True
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            await self.controller.move(CameraMotion("camera", "left"))
        self.assertEqual(self.moves, [])

    async def test_runtime_exposes_only_configured_motion_capability_and_queries_do_not_move(self):
        moving = self.spec()
        fixed = self.spec()
        del fixed["connection"]["pan_tilt"]
        specs = {"camera": moving, "fixed": fixed}

        directory, video, controls = DeviceDirectory(specs), VideoInputs(specs), CameraControls(specs)
        plugin = Go2RTCPlugin(Config(devices=specs, plugins=["go2rtc"]), directory, video, controls)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state.sqlite3")
            runtime = Runtime(store)
            runtime.add(plugin)
            runtime.add(DevicesPlugin(directory, video, controls))
            try:
                await runtime.start()
                listing = await runtime.invoke(DeviceQuery("list"))
                by_id = {d["id"]: d for d in listing["devices"]}
                self.assertIn("camera.pan_tilt", by_id["camera"]["capabilities"])
                self.assertNotIn("camera.pan_tilt", by_id["fixed"]["capabilities"])
                await runtime.invoke(DeviceQuery("inspect", "camera"))
                self.assertEqual(self.total_media, 0)
                result = await runtime.invoke(CameraMotion("camera", "left"))
                self.assertEqual(result["device_id"], "camera")
                with self.assertRaises(ValueError):
                    await runtime.invoke(CameraMotion("fixed", "left"))
            finally:
                await runtime.close()
                store.db.close()
        duplicate = self.spec()
        duplicate["physical_id"] = "same"
        other = self.spec()
        other["physical_id"] = "same"
        with self.assertRaisesRegex(ValueError, "physical camera"):
            Go2RTCPlugin(
                Config(devices={"a": duplicate, "b": other}, plugins=["go2rtc"]), directory, video, controls
            )
