"""Windows adapter contracts; native enumeration never records or plays media."""

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from assistant_runtime.plugins.hid_buttons.settings import parse
from assistant_runtime.plugins.hid_buttons.windows import open_device, read_reports
from assistant_runtime.plugins.pipewire.windows_audio import Endpoint, Transport, validate
from assistant_runtime.plugins.v4l2.windows_camera import Camera, devices


OUTPUT = "{0.0.0.00000000}.{00000000-0000-0000-0000-000000000001}"
INPUT = "{0.1.0.00000000}.{00000000-0000-0000-0000-000000000001}"
CAMERA = r"@device_pnp_\\?\usb#vid_1234&pid_5678#camera-serial"


def audio(kind="speaker", endpoint=OUTPUT):
    return {"adapter": "pipewire", "kind": kind, "label": "Test", "connection": {"endpoint_id": endpoint}}


def camera():
    return Camera(
        "test_camera", {"kind": "camera", "connection": {"device_path": CAMERA, "width": 32, "height": 24}}
    )


class WindowsContracts(unittest.IsolatedAsyncioTestCase):
    def test_exact_audio_identity_and_role(self):
        validate(audio())
        validate(audio("microphone", INPUT))
        for spec in (
            audio(endpoint="default"),
            audio(endpoint="0"),
            audio("microphone"),
            audio("speaker", INPUT),
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate(spec)
        changed = audio()
        changed["connection"]["fallback"] = True
        with self.assertRaises(ValueError):
            validate(changed)

    async def test_missing_endpoint_never_chooses_another(self):
        endpoint = Endpoint(audio())
        with patch(
            "assistant_runtime.plugins.pipewire.windows_audio.endpoints",
            return_value=[{"endpoint_id": INPUT}],
        ):
            with self.assertRaisesRegex(OSError, "no fallback"):
                await endpoint.ensure_ready()

    async def test_cancel_during_native_audio_open_closes_completed_stream(self):
        entered, release = threading.Event(), threading.Event()
        stream = SimpleNamespace(close=AsyncMock())

        def opening(*_):
            entered.set()
            release.wait(3)
            return stream

        endpoint = Endpoint(audio())
        with patch("assistant_runtime.plugins.pipewire.windows_audio.Transport", side_effect=opening):
            task = asyncio.create_task(endpoint.open(16000))
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        stream.close.assert_awaited_once()

    async def test_bounded_capture_overrun_does_not_deliver_stale_audio(self):
        stream = Transport.__new__(Transport)
        stream.capture, stream.rate, stream.maximum = True, 16000, 8
        stream.lock, stream.buffer = threading.Lock(), bytearray()
        stream.error, stream.closed, stream.device = None, False, SimpleNamespace(running=True)
        recorder = stream.record()
        next(recorder)
        recorder.send(b"12345678")
        self.assertEqual(await stream.read(4), b"1234")
        recorder.send(b"12345678")
        with self.assertRaisesRegex(OSError, "overrun"):
            await stream.read(4)
        self.assertFalse(stream.buffer)
        recorder.close()

    async def test_bounded_pcm_playback_and_close_discard(self):
        stream = Transport.__new__(Transport)
        stream.capture, stream.rate, stream.maximum = False, 16000, 32000
        stream.lock, stream.buffer = threading.Lock(), bytearray()
        stream.error, stream.closed, stream.device = None, False, SimpleNamespace(running=True, close=Mock())
        with self.assertRaises(ValueError):
            await stream.write(b"short")
        await stream.write(b"a" * 640)
        callback = stream.play()
        next(callback)
        self.assertEqual(callback.send(320), b"a" * 640)
        self.assertEqual(callback.send(320), bytes(640))
        await stream.write(b"b" * 640)
        await stream.close()
        self.assertFalse(stream.buffer)
        stream.device.close.assert_called_once()
        with self.assertRaises(OSError):
            await stream.write(b"c" * 640)
        callback.close()

    async def test_volume_is_confirmed_on_the_configured_endpoint(self):
        endpoint = Endpoint(audio(), minimum_volume=25)
        with patch(
            "assistant_runtime.plugins.pipewire.windows_audio.volume", return_value=(25, False)
        ) as operation:
            result = await endpoint.set_volume(10)
        operation.assert_called_once_with(OUTPUT, 25)
        self.assertEqual(result["scope"], "device")
        self.assertTrue(result["applied"])

    def test_directshow_uses_alternative_identity_not_friendly_name_or_audio(self):
        text = f'[dshow] "Camera" (video)\n[dshow] Alternative name "{CAMERA}"\n[dshow] "Microphone" (audio)\n[dshow] Alternative name "@device_pnp_audio"'
        self.assertEqual(devices(text), [CAMERA])
        for path in ("default", "0", "Friendly Camera", CAMERA + "\n"):
            with self.assertRaises(ValueError):
                Camera("camera", {"kind": "camera", "connection": {"device_path": path}})

    async def test_missing_camera_is_explicit_and_cannot_capture_other_device(self):
        provider = camera()
        provider.execute = AsyncMock(return_value=(1, b"", '[dshow] "Other" (video)'))
        with self.assertRaisesRegex(OSError, "no fallback"):
            await provider.capture()
        self.assertEqual(provider.capture_count, 0)
        provider.execute.assert_awaited_once()

    async def test_capture_validates_image_and_rejects_a_disconnected_device(self):
        from PIL import Image
        import io

        image = io.BytesIO()
        Image.new("RGB", (32, 24)).save(image, format="JPEG")
        description = f'[dshow] "Camera" (video)\n[dshow] Alternative name "{CAMERA}"'
        provider = camera()
        provider.execute = AsyncMock(
            side_effect=[(1, b"", description), (0, image.getvalue(), ""), (1, b"", "")]
        )
        with self.assertRaises(OSError):
            await provider.capture()
        self.assertEqual(provider.capture_count, 0)

    async def test_hid_exact_serial_ambiguity_and_read_only_reports(self):
        button = parse(
            {
                "wake": {
                    "vendor_id": "1234",
                    "product_id": "5678",
                    "serial": "fixed",
                    "report": "01fe",
                    "action": "wake",
                }
            }
        )[0]
        native = SimpleNamespace(
            enumerate=Mock(return_value=[{"path": b"fixed", "serial_number": "fixed"}]), device=Mock()
        )
        handle = native.device.return_value
        handle.get_serial_number_string.return_value = "fixed"
        handle.read.side_effect = [[1, 254], [], OSError("unplugged")]
        with patch.dict(sys.modules, {"hid": native}):
            received, ready = Mock(), Mock()
            with self.assertRaises(OSError):
                await read_reports(button, received, ready)
            received.assert_called_once_with(button, b"\x01\xfe")
            ready.assert_called_once()
            handle.close.assert_called_once()
            handle.write.assert_not_called()
            native.enumerate.return_value *= 2
            with self.assertRaisesRegex(OSError, "ambiguous"):
                open_device(button)


@unittest.skipUnless(sys.platform == "win32", "Windows native libraries and OS enumeration")
class NativeWindows(unittest.IsolatedAsyncioTestCase):
    async def test_airplay_native_encoder_and_owned_process_shutdown(self):
        from assistant_runtime.plugins.airplay.output import AirPlayPlayback
        from platform_support import process_alive

        encoded = asyncio.Event()
        received = bytearray()

        async def receive(reader, **_):
            while data := await reader.read(4096):
                received.extend(data)
                if len(received) > 256:
                    encoded.set()

        atv = SimpleNamespace(stream=SimpleNamespace(stream_file=receive), close=lambda: [])
        owner = SimpleNamespace(last_error=None)
        transport = await AirPlayPlayback.open(atv, 16000, owner)
        try:
            for _ in range(25):
                await transport.write(bytes(640))
            await asyncio.wait_for(encoded.wait(), 5)
            self.assertTrue(transport.healthy)
            self.assertGreater(transport.sent_bytes, 0)
        finally:
            await transport.close()
        self.assertFalse(process_alive(transport.process.pid))
        self.assertTrue(transport.encoder_task.done())
        self.assertTrue(transport.stream_task.done())

    async def test_observation_registry_native_private_write_and_revision(self):
        from assistant_runtime.plugins.observations.registry import Registry
        from platform_support import private_mode

        provider = SimpleNamespace(max_instances=1, validate=lambda value: value)
        definition = {"adapter": "fixture", "config": {"label": "Windows"}, "enabled": False}
        with tempfile.TemporaryDirectory() as folder:
            registry = Registry(Path(folder), {"fixture": provider})
            registry.update("configure", "source", 0, definition)
            self.assertEqual(private_mode(registry.path), 0o600)
            restored = Registry(Path(folder), {"fixture": provider})
            restored.load()
            self.assertEqual(restored.sources["source"], definition)
            with self.assertRaisesRegex(ValueError, "revision conflict"):
                restored.update("remove", "source", 0)

    async def test_media_library_native_ntfs_identity_and_safe_names(self):
        from assistant_runtime.plugins.media.local_library import LocalTree

        with tempfile.TemporaryDirectory(prefix="pa-windows-media-") as folder:
            root = Path(folder)
            library, stage = root / "library", root / "stage"
            library.mkdir()
            stage.mkdir()
            (library / "sample.txt").write_bytes(b"fixture")
            binding = {"mount_path": str(root.anchor), "path": str(library), "staging_path": str(stage)}
            tree = LocalTree(binding).open()
            try:
                before = tree.info("sample.txt")
                after = tree.change(
                    {"action": "rename", "path": "sample.txt", "destination": "renamed.txt", "before": before}
                )
                self.assertEqual(after["file_id"], before["file_id"])
                self.assertEqual((library / "renamed.txt").read_bytes(), b"fixture")
                self.assertFalse((library / "sample.txt").exists())
                with self.assertRaises(ValueError):
                    tree.change({"action": "mkdir", "destination": "CON"})
                with self.assertRaises(ValueError):
                    tree.info("../outside")
            finally:
                tree.close()

    async def test_placeholder_and_explicit_gesture_have_no_device_side_effects(self):
        from assistant_runtime.plugins.speech.placeholder import VoicePlaceholder
        from assistant_runtime.plugins.gesture import GesturePlugin

        context = SimpleNamespace(invoke=AsyncMock(return_value={"accepted": True}))
        placeholder = VoicePlaceholder()
        await placeholder.start(context)
        self.assertEqual(placeholder.diagnostics()["state"], "disabled")
        context.invoke.assert_not_awaited()
        gesture = GesturePlugin()
        await gesture.start(context)
        self.assertTrue((await gesture.simulate("wave"))["accepted"])
        with self.assertRaises(ValueError):
            await gesture.simulate("other")
        self.assertEqual(context.invoke.await_count, 1)

    async def test_keyword_native_libraries_and_missing_assets_are_explicit(self):
        import kaldi_native_fbank as knf
        import onnxruntime
        from assistant_runtime.plugins.keyword.recognition import Recognizer

        self.assertIn("CPUExecutionProvider", onnxruntime.get_available_providers())
        bank = knf.OnlineFbank(knf.FbankOptions())
        bank.accept_waveform(16000, [0.0] * 1600)
        self.assertGreater(bank.num_frames_ready, 0)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileNotFoundError):
                Recognizer(SimpleNamespace(models=Path(folder), wake_words=["test"]))

    async def test_native_hid_enumeration_and_missing_exact_identity(self):
        import hid

        self.assertIsInstance(await asyncio.to_thread(hid.enumerate), list)
        button = parse(
            {
                "missing": {
                    "vendor_id": "ffff",
                    "product_id": "ffff",
                    "serial": "pa-test-absent",
                    "report": "01",
                    "action": "wake",
                }
            }
        )[0]
        with self.assertRaisesRegex(OSError, "absent"):
            await asyncio.to_thread(open_device, button)

    async def test_native_wasapi_inventory_and_missing_endpoint(self):
        from assistant_runtime.plugins.pipewire.windows_audio import endpoints

        for kind in ("microphone", "speaker"):
            rows = await asyncio.to_thread(endpoints, kind)
            for row in rows:
                validate(audio(kind, row["endpoint_id"]))
        with self.assertRaisesRegex(OSError, "no fallback"):
            await Endpoint(audio()).ensure_ready()

    async def test_native_directshow_backend_and_unavailable_camera(self):
        provider = camera()
        try:
            code, listing, _ = await provider.execute(["-devices"], 65536)
            self.assertEqual(code, 0)
            self.assertRegex(listing.decode(), r"D\s+dshow\b")
            with self.assertRaisesRegex(OSError, "no fallback"):
                await provider.inspect()
        finally:
            await provider.close()
        self.assertFalse(provider.processes)
