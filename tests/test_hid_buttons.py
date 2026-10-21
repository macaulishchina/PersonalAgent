"""Synthetic HID reports, device identity and lifecycle; no physical wake calls."""

import asyncio
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from assistant_runtime.kernel.contracts import Trigger
from assistant_runtime.kernel.runtime import Context
from assistant_runtime.plugins.hid_buttons.plugin import HidButtonsPlugin, discover
from assistant_runtime.plugins.hid_buttons.settings import parse


SETTINGS = {
    "speakerphone": {
        "vendor_id": "17ef",
        "product_id": "a03b",
        "serial": "test-device",
        "report": "01fe",
        "action": "wake",
        "debounce_ms": 500,
    }
}


class SettingsTest(unittest.TestCase):
    def test_validation_and_no_implicit_mappings(self):
        self.assertEqual(parse({}), [])
        self.assertEqual(parse(SETTINGS)[0].report, b"\x01\xfe")
        for change in (
            {"action": "stop"},
            {"report": "01f"},
            {"report": ""},
            {"serial": ""},
            {"vendor_id": 6127},
            {"debounce_ms": True},
            {"debounce_ms": 0},
            {"command": "x"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse({"speakerphone": {**SETTINGS["speakerphone"], **change}})
        with self.assertRaises(ValueError):
            parse({**SETTINGS, "duplicate": SETTINGS["speakerphone"]})

    def test_discovery_follows_identity_not_hidraw_number_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def device(name, serial="test-device"):
                target = root / name / "device"
                target.mkdir(parents=True)
                (target / "uevent").write_text(f"HID_ID=0003:000017EF:0000A03B\nHID_UNIQ={serial}\n")

            device("hidraw9", "other-device")
            button = parse(SETTINGS)[0]
            with self.assertRaises(OSError):
                discover(button, root)
            device("hidraw3")
            self.assertEqual(discover(button, root), Path("/dev/hidraw3"))
            device("hidraw4")
            with self.assertRaises(OSError):
                discover(button, root)


class ButtonsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = SimpleNamespace(
            invoke=AsyncMock(return_value={"accepted": True}), ready=asyncio.Event()
        )
        self.runtime.ready.set()
        self.context = Context(self.runtime, "hid_buttons")
        self.plugin = HidButtonsPlugin(SimpleNamespace(hid_buttons=SETTINGS))
        self.plugin.context = self.context
        self.button = self.plugin.buttons[0]

    async def asyncTearDown(self):
        await self.context.close()

    async def test_exact_reports_debounce_and_no_wake_storm_while_pending(self):
        gate = asyncio.Event()

        async def invoke(request):
            await gate.wait()
            return {"accepted": True}

        self.runtime.invoke.side_effect = invoke
        for packet in (b"\x02\x01", b"\x01\xff", b"\x01\xfe\x00"):
            self.plugin.report(self.button, packet, 1)
        self.runtime.invoke.assert_not_awaited()
        self.plugin.report(self.button, b"\x01\xfe", 2)
        await asyncio.sleep(0)
        for now in (2.01, 2.02, 10):
            self.plugin.report(self.button, b"\x01\xfe", now)
        self.runtime.invoke.assert_awaited_once_with(Trigger("hid_buttons:speakerphone", "wake"))
        self.assertEqual(len(self.context.tasks), 1)
        gate.set()
        await asyncio.gather(*self.context.tasks)
        self.runtime.invoke.side_effect = None
        self.plugin.report(self.button, b"\x01\xfe", 11)
        await asyncio.gather(*self.context.tasks)
        self.assertEqual(self.runtime.invoke.await_count, 2)
        await self.context.close()
        self.plugin.report(self.button, b"\x01\xfe", 20)
        self.assertFalse(self.context.tasks)

    async def test_declined_or_failed_wake_never_retries(self):
        self.runtime.invoke.return_value = {"accepted": False}
        await self.plugin.wake(self.button)
        self.runtime.invoke.assert_awaited_once()
        self.runtime.invoke.reset_mock()
        self.runtime.invoke.side_effect = TimeoutError()
        with self.assertLogs("assistant_runtime.plugins.hid_buttons.plugin", level="WARNING"):
            await self.plugin.wake(self.button)
        self.runtime.invoke.assert_awaited_once()

    async def test_read_only_event_path_disconnect_and_reader_cleanup(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        left.setblocking(False)
        task = asyncio.create_task(self.plugin.read(self.button, left.fileno()))
        try:
            await asyncio.sleep(0)
            right.send(b"\x01\xfe")
            for _ in range(20):
                if self.runtime.invoke.await_count:
                    break
                await asyncio.sleep(0.01)
            self.runtime.invoke.assert_awaited_once()
            self.assertEqual(self.context.state, "ready")
            right.close()
            with self.assertRaises(OSError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            left.close()
            right.close()

    async def test_missing_hardware_degrades_without_blocking_startup_and_retries(self):
        self.runtime.ready.clear()
        with patch(
            "assistant_runtime.plugins.hid_buttons.plugin.discover", side_effect=PermissionError()
        ) as lookup:
            await self.plugin.start(self.context)
            await asyncio.sleep(0)
            lookup.assert_not_called()
            self.runtime.ready.set()
            await asyncio.sleep(0.01)
            self.assertEqual(self.context.state, "degraded")
            self.assertIn("permission_denied", self.context.error)
            self.assertEqual(len(self.context.tasks), 1)
            await self.context.close()
            self.assertFalse(self.context.tasks)
            self.assertEqual(self.context.state, "stopped")

    async def test_disconnect_reconnect_and_descriptor_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device"
            path.touch()
            connected = asyncio.Event()
            descriptors = []

            async def read(button, fd):
                descriptors.append(fd)
                if len(descriptors) == 1:
                    raise OSError("unplugged")
                self.plugin.health(button.name, "ready")
                connected.set()
                await asyncio.Event().wait()

            sleep = asyncio.sleep

            async def fast_retry(seconds):
                await sleep(0)

            with (
                patch("assistant_runtime.plugins.hid_buttons.plugin.discover", return_value=path),
                patch.object(self.plugin, "read", side_effect=read),
                patch("assistant_runtime.plugins.hid_buttons.plugin.asyncio.sleep", side_effect=fast_retry),
            ):
                await self.plugin.start(self.context)
                await asyncio.wait_for(connected.wait(), 1)
                self.assertEqual(self.context.state, "ready")
                self.assertIsNone(self.context.error)
                await self.context.close()
            import os

            self.assertEqual(len(descriptors), 2)
            for fd in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    async def test_replaced_node_with_reused_device_number_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device"
            path.touch()
            rejected = asyncio.Event()

            async def retry(seconds):
                rejected.set()
                await asyncio.Event().wait()

            def replacement(fd):
                real = path.stat()
                return SimpleNamespace(st_dev=real.st_dev, st_ino=real.st_ino + 1, st_rdev=real.st_rdev)

            with (
                patch("assistant_runtime.plugins.hid_buttons.plugin.discover", return_value=path),
                patch("assistant_runtime.plugins.hid_buttons.plugin.os.fstat", side_effect=replacement),
                patch("assistant_runtime.plugins.hid_buttons.plugin.asyncio.sleep", side_effect=retry),
                patch.object(self.plugin, "read", new_callable=AsyncMock) as read,
            ):
                await self.plugin.start(self.context)
                await asyncio.wait_for(rejected.wait(), 1)
                read.assert_not_awaited()
                self.assertIn("disconnected", self.context.error)
                await self.context.close()
