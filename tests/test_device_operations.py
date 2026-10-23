"""Device-level ownership, concurrency, lifecycle and MCP are independent of voice."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastmcp import Client

from assistant_runtime.core.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.core.devices.cameras import CameraControls
from assistant_runtime.core.devices.inputs import AudioInputs
from assistant_runtime.core.devices.outputs import AudioOutputs
from assistant_runtime.core.devices.operations import DeviceOperations
from assistant_runtime.core.devices.resources import DeviceResources, DeviceBusy
from assistant_runtime.core.devices.video import VideoInputs
from assistant_runtime.api.contracts import (
    AudioVolume,
    CameraMotion,
    DeviceOperationQuery,
    DeviceOperationCancel,
    VideoCapture,
    VideoFrame,
    VoiceBinding,
)
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.core.devices.tools import create_server
from assistant_runtime.app.store import Store


class OperationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        specs = {
            ident: {"adapter": "fake", "kind": kind, "label": ident}
            for ident, kind in [
                ("main", "speaker"),
                ("extra", "speaker"),
                ("mic", "microphone"),
                ("cam", "camera"),
                ("cam2", "camera"),
            ]
        }
        self.binding = VoiceBinding("mic", "main")
        self.directory = DeviceDirectory(specs, self.binding)
        self.resources = DeviceResources()
        self.operations = DeviceOperations(self.directory, self.resources, history_limit=3)
        self.video = VideoInputs(specs)
        self.controls = CameraControls(specs)
        self.outputs = AudioOutputs(specs, self.resources)
        self.inputs = AudioInputs(specs, self.resources)
        outer = self

        class Adapter:
            manifest = Manifest("fake")

            async def start(self, context):
                for ident, spec in specs.items():
                    caps = (
                        ["video.snapshot", "camera.pan_tilt"]
                        if spec["kind"] == "camera"
                        else ["audio.volume", "audio.playback"]
                        if spec["kind"] == "speaker"
                        else ["audio.capture"]
                    )
                    outer.directory.register("fake", ident, caps, AsyncMock())
                    if spec["kind"] == "camera":
                        provider = SimpleNamespace(
                            capture=AsyncMock(return_value=VideoFrame(ident, b"jpeg", 1, 1, time.time(), 1))
                        )
                        outer.video.register("fake", ident, provider)
                        outer.controls.register(
                            "fake", ident, SimpleNamespace(move=AsyncMock(return_value={"device_id": ident}))
                        )
                    elif spec["kind"] == "speaker":
                        provider = SimpleNamespace(
                            volume=20,
                            set_volume=AsyncMock(
                                return_value={"volume_percent": 30, "applied": True, "scope": "device"}
                            ),
                            open=AsyncMock(),
                        )
                        outer.outputs.register("fake", ident, provider)

        self.runtime = Runtime(self.store)
        self.runtime.add(Adapter())
        self.runtime.add(
            DevicesPlugin(
                self.directory,
                self.video,
                self.controls,
                audio_inputs=self.inputs,
                audio_outputs=self.outputs,
            )
        )
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_independent_devices_and_targeted_cancel_keep_other_operations(self):
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        for index, ident in enumerate(("cam", "cam2")):

            async def capture(index=index):
                started[index].set()
                await release.wait()

            self.video.providers[ident].capture = capture
        first, second = VideoCapture("cam"), VideoCapture("cam2")
        one = asyncio.create_task(self.runtime.invoke(first))
        two = asyncio.create_task(self.runtime.invoke(second))
        await asyncio.gather(*(e.wait() for e in started))
        with self.assertRaises(DeviceBusy):
            await self.runtime.invoke(VideoCapture("cam"))
        state = await self.runtime.invoke(DeviceOperationQuery("cam", first.operation_id))
        self.assertEqual(state["state"], "running")
        canceled = await self.runtime.invoke(DeviceOperationCancel("cam", first.operation_id))
        self.assertEqual(canceled["state"], "canceled")
        with self.assertRaises(asyncio.CancelledError):
            await one
        self.assertFalse(two.done())
        self.assertTrue(self.resources.snapshot("cam2"))
        self.assertFalse(self.resources.snapshot("cam"))
        two.cancel()
        await asyncio.gather(two, return_exceptions=True)

    async def test_cancel_is_idempotent_and_lease_waits_for_cleanup(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def capture():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        self.video.providers["cam"].capture = capture
        request = VideoCapture("cam")
        running = asyncio.create_task(self.runtime.invoke(request))
        await started.wait()
        first = asyncio.create_task(self.runtime.invoke(DeviceOperationCancel("cam", request.operation_id)))
        await cleaning.wait()
        second = asyncio.create_task(self.runtime.invoke(DeviceOperationCancel("cam", request.operation_id)))
        await asyncio.sleep(0)
        self.assertEqual(self.operations.running[request.operation_id].cancelling(), 1)
        with self.assertRaises(DeviceBusy):
            await self.runtime.invoke(VideoCapture("cam"))
        release.set()
        await asyncio.gather(first, second)
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertFalse(self.resources.held)

    async def test_canceled_before_worker_started_releases_resource(self):
        original = self.operations.context.spawn

        def canceled(coroutine, label):
            task = original(coroutine, label)
            task.cancel()
            return task

        self.operations.context.spawn = canceled
        request = VideoCapture("cam")
        with self.assertRaises(asyncio.CancelledError):
            await self.operations.execute("video.snapshot", request)
        self.assertFalse(self.resources.held)
        self.assertEqual(self.operations.snapshot("cam", request.operation_id)["state"], "canceled")
        self.video.providers["cam"].capture.assert_not_awaited()

    async def test_failure_timeout_and_duplicate_identity_never_replay(self):
        provider = self.video.providers["cam"]
        request = VideoCapture("cam")
        provider.capture.side_effect = RuntimeError("private connection information")
        with self.assertRaises(RuntimeError):
            await self.runtime.invoke(request)
        self.assertEqual(self.operations.snapshot("cam", request.operation_id)["error"], "RuntimeError")
        with self.assertRaisesRegex(ValueError, "already used"):
            await self.runtime.invoke(request)
        provider.capture.assert_awaited_once()
        self.assertFalse(self.resources.held)

        async def blocked():
            await asyncio.Event().wait()

        provider.capture.side_effect = blocked
        timed = VideoCapture("cam")
        with self.assertRaises(TimeoutError):
            await self.runtime.invoke(timed, timeout=0.02)
        self.assertEqual(self.operations.snapshot("cam", timed.operation_id)["state"], "canceled")
        self.assertFalse(self.resources.held)
        self.assertEqual(self.runtime.contexts["devices"].state, "ready")

    async def test_bounded_metadata_and_no_raw_media_in_operation_history(self):
        for _ in range(10):
            await self.runtime.invoke(VideoCapture("cam"))
        snapshot = self.operations.snapshot("cam")
        self.assertEqual(len(snapshot["operations"]), 3)
        self.assertEqual(len(self.operations.records), 3)
        self.assertNotIn("result", snapshot["operations"][0])
        self.assertNotIn("jpeg", str(snapshot))
        self.assertGreaterEqual(snapshot["operations"][0]["duration_seconds"], 0)
        self.assertEqual(
            self.directory.snapshot("cam")["tool_operations"], ["camera.pan_tilt", "video.snapshot"]
        )
        self.assertEqual(self.directory.snapshot("main")["tool_operations"], ["audio.volume"])

    async def test_voice_resource_is_visible_but_not_a_cancelable_tool_operation(self):
        lease = self.resources.acquire("main", "audio.output", "voice")
        state = self.operations.snapshot("main")
        self.assertEqual(state["resources"][0]["owners"][0]["owner"], "voice")
        with self.assertRaises(ValueError):
            await self.operations.cancel("main", "f" * 32)
        self.assertTrue(self.resources.snapshot("main"))
        lease.release()

    async def test_mcp_volume_targets_main_or_extra_without_voice_controls(self):
        async with Client(create_server(SimpleNamespace(invoke=self.runtime.invoke))) as client:
            for ident in ("extra", "main"):
                result = (
                    await client.call_tool("set_audio_volume", {"device_id": ident, "percent": 30})
                ).structured_content
                self.assertEqual(result["device_id"], ident)
                self.assertTrue(result["applied"])
                state = (
                    await client.call_tool(
                        "get_device_operation", {"device_id": ident, "operation_id": result["operation_id"]}
                    )
                ).structured_content
                self.assertEqual(state["state"], "completed")
            self.assertEqual(self.directory.voice_io, self.binding)
            self.outputs.get("main").set_volume.assert_awaited_once_with(30)
            self.outputs.get("extra").set_volume.assert_awaited_once_with(30)
            self.assertEqual(self.directory.snapshot("main")["voice_role"], "output")

    async def test_registration_and_operation_identity_checks_precede_driver_calls(self):
        with self.assertRaises(ValueError):
            self.operations.register("wrong", "cam", "video.snapshot", AsyncMock())
        with self.assertRaises(ValueError):
            self.operations.register("fake", "cam", "audio.playback", AsyncMock())
        with self.assertRaises(ValueError):
            self.operations.register("fake", "cam", "video.snapshot", AsyncMock())
        with self.assertRaises(ValueError):
            await self.runtime.invoke(VideoCapture("main"))
        self.video.providers["cam"].capture.assert_not_awaited()
        request = AudioVolume("main", 10)
        await self.runtime.invoke(request)
        with self.assertRaises(ValueError):
            await self.runtime.invoke(replace(request, device_id="extra"))

    async def test_durable_identity_survives_cache_eviction_and_restart_without_replay(self):
        request = VideoCapture("cam")
        await self.runtime.invoke(request)
        for _ in range(5):
            await self.runtime.invoke(VideoCapture("cam"))
        self.assertNotIn(request.operation_id, self.operations.records)
        with self.assertRaisesRegex(ValueError, "already used"):
            await self.runtime.invoke(request)
        previous = self.operations.snapshot("cam", request.operation_id)
        # Simulate a crash after the durable start receipt, before the final receipt.
        previous.update(state="running", finished_at=None, duration_seconds=None)
        with self.store.db:
            self.store.db.execute(
                "UPDATE device_operations SET state='running',metadata=? WHERE id=?",
                (json.dumps(previous), request.operation_id),
            )
        restarted = DeviceOperations(self.directory, self.resources)
        restarted.register("fake", "cam", "video.snapshot", self.video.capture, resource="video.input")
        restarted.bind(self.runtime.contexts["devices"])
        state = restarted.snapshot("cam", request.operation_id)
        self.assertEqual(state["state"], "unknown")
        self.assertIsNone(state["duration_seconds"])
        with self.assertRaisesRegex(ValueError, "already used"):
            await restarted.execute("video.snapshot", request)
        self.assertEqual(self.video.providers["cam"].capture.await_count, 6)

    async def test_mutating_failure_does_not_claim_no_effect(self):
        self.outputs.get("main").set_volume.side_effect = OSError("response lost after delivery")
        request = AudioVolume("main", 30)
        with self.assertRaises(OSError):
            await self.runtime.invoke(request)
        state = self.operations.snapshot("main", request.operation_id)
        self.assertEqual(state["state"], "unknown")
        self.assertTrue(state["effects_may_have_occurred"])
        self.assertFalse(self.resources.held)

    async def test_camera_stop_bypasses_motion_busy_without_preempting_other_resources(self):
        started = asyncio.Event()

        async def move(request):
            if request.direction != "stop":
                started.set()
                await asyncio.Event().wait()
            return {"device_id": request.device_id}

        self.controls.providers["cam"].move.side_effect = move
        request = CameraMotion("cam", "left", 80)
        running = asyncio.create_task(self.runtime.invoke(request))
        await started.wait()
        with self.assertRaises(DeviceBusy):
            await self.runtime.invoke(CameraMotion("cam", "right", 80))
        stopped = await self.runtime.invoke(CameraMotion("cam", "stop", 0))
        self.assertEqual(stopped["device_id"], "cam")
        self.assertTrue(self.resources.snapshot("cam"))
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    async def test_shutdown_does_not_cancel_an_already_running_cleanup_again(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def capture():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        self.video.providers["cam"].capture = capture
        request = VideoCapture("cam")
        running = asyncio.create_task(self.runtime.invoke(request))
        await started.wait()
        running.cancel()
        await cleaning.wait()
        worker = self.operations.running[request.operation_id]
        shutdown = asyncio.create_task(self.runtime.close())
        await asyncio.sleep(0)
        self.assertEqual(worker.cancelling(), 1)
        self.assertTrue(self.resources.held)
        release.set()
        await shutdown
        await asyncio.gather(running, return_exceptions=True)
        self.assertFalse(self.resources.held)

    async def test_repeated_caller_cancel_does_not_interrupt_owned_cleanup(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def capture():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

        self.video.providers["cam"].capture = capture
        request = VideoCapture("cam")
        caller = asyncio.create_task(self.operations.execute("video.snapshot", request))
        await started.wait()
        caller.cancel()
        await cleaning.wait()
        worker = self.operations.running[request.operation_id]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(worker.cancelling(), 1)
        self.assertFalse(worker.done())
        with self.assertRaises(DeviceBusy):
            await self.runtime.invoke(VideoCapture("cam"))
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(self.resources.held)
        self.assertEqual(self.operations.snapshot("cam", request.operation_id)["state"], "canceled")


class StreamsTest(unittest.IsolatedAsyncioTestCase):
    def setup_registry(self, shared=False, inputs=False):
        kind = "microphone" if inputs else "speaker"
        specs = {i: {"adapter": "fake", "kind": kind} for i in ("main", "extra")}
        resources = DeviceResources()
        registry = (AudioInputs if inputs else AudioOutputs)(specs, resources)
        for ident in specs:
            registry.register("fake", ident, SimpleNamespace(open=AsyncMock()), shared=shared)
        return registry, resources

    def transport(self):
        return SimpleNamespace(
            healthy=True, write=AsyncMock(), read=AsyncMock(return_value=bytes(640)), close=AsyncMock()
        )

    async def test_main_device_can_mix_tool_stream_without_rebinding_or_stopping_voice(self):
        registry, resources = self.setup_registry(shared=True)
        voice, tool = self.transport(), self.transport()
        registry.get("main").open.side_effect = [voice, tool]
        first = await registry.open("main", 24000, owner="voice")
        second = await registry.open("main", 24000, owner="tool-operation")
        await first.write(bytes(960))
        await second.write(bytes(960))
        await second.close()
        voice.close.assert_not_awaited()
        tool.close.assert_awaited_once()
        self.assertEqual(resources.snapshot("main")[0]["owners"], [{"owner": "voice", "shared": True}])
        await registry.close()
        self.assertFalse(resources.held)

    async def test_exclusive_busy_never_closes_existing_and_other_device_is_independent(self):
        registry, resources = self.setup_registry()
        for ident in ("main", "extra"):
            registry.get(ident).open.return_value = self.transport()
        main = await registry.open("main", 24000, owner="voice")
        extra = await registry.open("extra", 24000, owner="tool")
        with self.assertRaises(DeviceBusy):
            await registry.open("main", 24000, owner="tool")
        main.transport.close.assert_not_awaited()
        registry.get("main").open.assert_awaited_once()
        await extra.close()
        self.assertTrue(main.healthy)
        await registry.close()
        self.assertFalse(resources.held)

    async def test_capture_is_independent_data_and_close_only_releases_its_own_stream(self):
        registry, resources = self.setup_registry(shared=True, inputs=True)
        voice, tool = self.transport(), self.transport()
        registry.get("main").open.side_effect = [voice, tool]
        first = await registry.open("main", 16000, owner="voice")
        second = await registry.open("main", 16000, owner="tool")
        self.assertEqual(await second.read(640), bytes(640))
        await second.close()
        voice.close.assert_not_awaited()
        self.assertEqual(await first.read(640), bytes(640))
        await registry.close()
        self.assertFalse(resources.held)

    async def test_caller_cancellation_during_close_keeps_cleanup_owned_and_lease_held(self):
        registry, resources = self.setup_registry()
        started, release = asyncio.Event(), asyncio.Event()
        transport = self.transport()

        async def close():
            started.set()
            await release.wait()

        transport.close.side_effect = close
        registry.get("main").open.return_value = transport
        stream = await registry.open("main", 24000, owner="tool")
        closing = asyncio.create_task(stream.close())
        await started.wait()
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        with self.assertRaises(DeviceBusy):
            await registry.open("main", 24000, owner="voice")
        self.assertTrue(registry.cleanups)
        release.set()
        await stream.close()
        await registry.close()
        self.assertFalse(resources.held)

    async def test_failed_cleanup_keeps_lease_until_successful_retry(self):
        registry, resources = self.setup_registry()
        transport = self.transport()
        transport.close.side_effect = [RuntimeError("not closed"), None]
        registry.get("main").open.return_value = transport
        stream = await registry.open("main", 24000, owner="tool")
        with self.assertRaises(RuntimeError):
            await stream.close()
        self.assertTrue(resources.held)
        with self.assertRaises(DeviceBusy):
            await registry.open("main", 24000, owner="voice")
        await stream.close()
        await registry.close()
        self.assertFalse(resources.held)

    async def test_shutdown_cancels_opening_without_leaking_lease(self):
        registry, resources = self.setup_registry()
        started = asyncio.Event()

        async def opening(rate):
            started.set()
            await asyncio.Event().wait()

        registry.get("main").open.side_effect = opening
        pending = asyncio.create_task(registry.open("main", 24000, owner="tool"))
        await started.wait()
        await registry.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(resources.held)

    async def test_airplay_open_on_live_connection_preserves_existing_owner(self):
        from assistant_runtime.plugins.airplay.output import AirPlayOutput

        output = AirPlayOutput.__new__(AirPlayOutput)
        output.open_lock = asyncio.Lock()
        output.connection = SimpleNamespace(healthy=True)
        output.close = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "busy"):
            await output.open(24000)
        output.close.assert_not_awaited()

    async def test_unhealthy_voice_transport_releases_lease_before_reconnect(self):
        from assistant_runtime.plugins.pipewire.stream_audio import StreamPlayer

        registry, resources = self.setup_registry()
        old, new = self.transport(), self.transport()
        registry.get("main").open.side_effect = [old, new]
        route = SimpleNamespace(
            hardware_volume_configured=False,
            ensure=AsyncMock(),
            open_output=lambda rate: registry.open("main", rate, owner="voice"),
        )
        player = StreamPlayer(route)
        player.transport = await route.open_output(24000)
        old.healthy = False
        written = asyncio.Event()
        new.write.side_effect = lambda pcm: written.set()
        worker = asyncio.create_task(player.run())
        try:
            player.feed(b"\x00\x01" * 480)
            await asyncio.wait_for(written.wait(), 1)
            old.close.assert_awaited_once()
            self.assertEqual(len(resources.snapshot("main")[0]["owners"]), 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await registry.close()
        self.assertFalse(resources.held)
