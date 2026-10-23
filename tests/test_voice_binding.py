"""Voice transport binding is startup state, independent of device tool inventory."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.assistant.commands import parse_command
from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.devices.directory import DeviceDirectory
from assistant_runtime.kernel.contracts import Control, DeviceQuery, VoiceBinding, AudioVolume
from assistant_runtime.mcp.device import create_server
from assistant_runtime.plugins.pipewire.audio import AudioRoute
from assistant_runtime.plugins.pipewire.devices import PipeWireDevices
from test_behavior import device_config


class FixedVoiceTest(unittest.IsolatedAsyncioTestCase):
    def test_startup_ignores_persisted_selection_and_binding_is_read_only(self):
        config = device_config()
        store = SimpleNamespace(get=Mock(return_value="removed_old_speaker"), set=Mock())
        route = AudioRoute(config, store)
        self.assertEqual(route.input_device, "living_microphone")
        self.assertEqual(route.output_device, "living")
        store.get.assert_not_called()
        for attr in ("input_device", "output_device"):
            with self.assertRaises(AttributeError):
                setattr(route, attr, "study")
        with self.assertRaises(FrozenInstanceError):
            route._voice_io.output_device = "study"
        self.assertFalse(hasattr(route, "select"))
        store.set.assert_not_called()

    async def test_later_config_edits_and_extra_hardware_do_not_replace_voice(self):
        config = device_config(echo_cancel=False)
        route = AudioRoute(config, SimpleNamespace(set=Mock()))
        config.default_output = "study"
        config.input_device = "other_microphone"
        config.devices["living"]["connection"] = config.devices["study"]["connection"]
        config.devices["living_microphone"]["connection"]["match"] = "other_microphone"
        route.devices = AsyncMock(
            return_value=(
                [{"name": "alsa_input.usb_UGREEN_Camera_2K"}, {"name": "other_microphone"}],
                [{"name": "bluez_output.4C_C6_4C_97_78_4A.1"}, {"name": "bluez_output.8C_53_C3_7F_18_7C.1"}],
            )
        )
        with patch(
            "assistant_runtime.plugins.pipewire.audio.command", new_callable=AsyncMock, return_value="[]"
        ):
            await route.ensure(force=True)
        self.assertEqual(route.source, "alsa_input.usb_UGREEN_Camera_2K")
        self.assertEqual(route.physical_sink, "bluez_output.4C_C6_4C_97_78_4A.1")
        self.assertEqual((route.input_device, route.output_device), ("living_microphone", "living"))
        # Removal of the bound node fails even though another speaker is present.
        route.devices.return_value = (
            [{"name": "alsa_input.usb_UGREEN_Camera_2K"}],
            [{"name": "bluez_output.8C_53_C3_7F_18_7C.1"}],
        )
        with patch("assistant_runtime.plugins.pipewire.audio.command", new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, "living speaker unavailable"):
                await route.ensure()
        self.assertEqual(route.output_device, "living")
        self.assertFalse(route.ready.is_set())

    async def test_directory_exposes_binding_roles_and_extra_devices_independently(self):
        config = device_config()
        binding = VoiceBinding(config.input_device, config.default_output)
        directory = DeviceDirectory(config.devices, binding)
        route = SimpleNamespace(
            input_device=binding.input_device,
            output_device=binding.output_device,
            capture=None,
            ready=asyncio.Event(),
            devices=AsyncMock(return_value=([], [])),
        )
        PipeWireDevices(config, route, directory).register()
        devices = {d["id"]: d for d in (await directory.handle(DeviceQuery("list")))["devices"]}
        self.assertEqual(devices[binding.input_device]["voice_role"], "input")
        self.assertEqual(devices[binding.output_device]["voice_role"], "output")
        self.assertTrue(devices[binding.output_device]["voice_binding_locked"])
        self.assertIsNone(devices["study"]["voice_role"])
        self.assertFalse(devices["study"]["voice_binding_locked"])
        self.assertIn("audio.playback", devices["study"]["capabilities"])
        self.assertFalse(any("audio.select" in d["capabilities"] for d in devices.values()))
        # Registering another provider changes discovery, never the fixed binding.
        config.devices["new_video"] = {"adapter": "video", "kind": "camera", "label": "Extra camera"}
        directory.register("video", "new_video", ["video.snapshot"], AsyncMock())
        self.assertIsNone(directory.snapshot("new_video")["voice_role"])
        self.assertEqual(directory.voice_io, binding)
        self.assertIn('"voice_role": "output"', directory.context_text())

    async def test_legacy_control_is_rejected_without_touching_playback(self):
        service = AssistantService.__new__(AssistantService)
        service.last_activity = 123
        service.player = SimpleNamespace(stop=AsyncMock())
        service.route = SimpleNamespace(select=AsyncMock())
        service.runtime = SimpleNamespace(invoke=AsyncMock())
        for kind in ("room", "input", "select_audio_output", "select_audio_input"):
            with self.assertRaisesRegex(ValueError, "fixed"):
                await service.control(Control(kind, "study"))
        with self.assertRaises(ValueError):
            await service.tool_control({"action": "room", "room": "study"})
        self.assertEqual(service.last_activity, 123)
        service.player.stop.assert_not_awaited()
        service.route.select.assert_not_awaited()
        service.runtime.invoke.assert_not_awaited()

    async def test_mcp_does_not_expose_selection_but_keeps_interaction_controls(self):
        handler = SimpleNamespace(invoke=AsyncMock(return_value={"ok": True}))
        async with Client(create_server(handler)) as client:
            names = {t.name for t in await client.list_tools()}
            self.assertNotIn("select_audio_output", names)
            self.assertNotIn("select_audio_input", names)
            with self.assertRaises(ToolError):
                await client.call_tool("select_audio_output", {"device_id": "study"})
            handler.invoke.assert_not_awaited()
            await client.call_tool("set_audio_volume", {"device_id": "study", "percent": 40})
            handler.invoke.assert_awaited_once_with(AudioVolume("study", 40))

    def test_voice_phrase_never_changes_route_but_session_controls_remain(self):
        for text in ("切换到客厅音箱", "切换到书房", "使用HomePod播放", "在客厅回答"):
            self.assertIsNone(parse_command(text, ["嘟嘟"]))
        self.assertEqual(parse_command("嘟嘟，音量调到四十", ["嘟嘟"]), Control("volume", 40))
        self.assertEqual(parse_command("嘟嘟，停止播放", ["嘟嘟"]), Control("stop"))
