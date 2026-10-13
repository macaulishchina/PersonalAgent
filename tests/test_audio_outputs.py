"""Output extension contracts: same route/player, no protocol-specific assistant."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from assistant_runtime.devices.outputs import AudioOutputs
from assistant_runtime.plugins.pipewire.audio import AudioRoute
from assistant_runtime.plugins.pipewire.playback import MirroredPlayback
from assistant_runtime.assistant.commands import parse_command
from test_behavior import device_config


class OutputRegistrationTest(unittest.IsolatedAsyncioTestCase):
    def config(self):
        c = device_config(echo_cancel=False)
        c.devices["remote"] = {"adapter": "remote", "kind": "speaker", "label": "Remote", "connection": {}}
        return c

    def test_owner_and_duplicate_outputs_rejected(self):
        c = self.config()
        outputs = AudioOutputs(c.devices)
        with self.assertRaises(ValueError):
            outputs.register("pipewire", "remote", object())
        outputs.register("remote", "remote", object())
        with self.assertRaises(ValueError):
            outputs.register("remote", "remote", object())
        with self.assertRaises(ValueError):
            outputs.get("missing")

    async def test_external_route_keeps_same_microphone_and_skips_unknown_volume(self):
        c = self.config()
        outputs = AudioOutputs(c.devices)
        transport = SimpleNamespace(healthy=True, write=AsyncMock(), close=AsyncMock())
        provider = SimpleNamespace(
            volume=None,
            ensure_ready=AsyncMock(),
            open=AsyncMock(return_value=transport),
            set_volume=AsyncMock(),
            close=AsyncMock(),
        )
        outputs.register("remote", "remote", provider)
        store = SimpleNamespace(get=lambda k, d: d, set=Mock())
        route = AudioRoute(c, store, outputs)
        route.devices = AsyncMock(return_value=([{"name": "alsa_input.usb_UGREEN_Camera_2K"}], []))
        await route.select("remote")
        self.assertTrue(route.ready.is_set())
        self.assertIsNone(route.volume)
        self.assertEqual(route.source, "alsa_input.usb_UGREEN_Camera_2K")
        provider.set_volume.assert_not_awaited()
        self.assertIs(await route.open_output(24000), transport)
        await route.set_volume(50)
        provider.set_volume.assert_awaited_once_with(80)
        provider.ensure_ready.side_effect = RuntimeError("receiver disconnected")
        with self.assertRaises(RuntimeError):
            await route.ensure()
        self.assertFalse(route.ready.is_set())
        await route.close()
        provider.close.assert_awaited_once()

    async def test_reference_and_remote_output_close_together(self):
        output = SimpleNamespace(healthy=True, write=AsyncMock(), close=AsyncMock())
        reference = SimpleNamespace(healthy=True, write=AsyncMock(), close=AsyncMock())
        m = MirroredPlayback(output, reference)
        await m.write(b"audio")
        await m.close()
        output.write.assert_awaited_once_with(b"audio")
        reference.write.assert_awaited_once_with(b"audio")
        output.close.assert_awaited_once()
        reference.close.assert_awaited_once()

    def test_specific_living_speaker_label_beats_shared_room_alias(self):
        aliases = {"living": ["客厅音箱", "客厅"], "homepod": ["HomePod", "客厅"]}
        self.assertEqual(parse_command("切换到客厅音箱", aliases).value, "living")
        self.assertEqual(parse_command("切换到HomePod", aliases).value, "homepod")
        self.assertIsNone(parse_command("切换到客厅", aliases))


class AirPlayBoundTest(unittest.IsolatedAsyncioTestCase):
    def test_fixed_port_and_identity_validation(self):
        try:
            from assistant_runtime.plugins.airplay.output import validate_connection
        except ImportError:
            self.skipTest("optional AirPlay extra is absent")
        c = {"host": "127.0.0.1", "identifier": "test-id", "timing_port": 49160, "control_port": 49161}
        validate_connection(c)
        for change in ({"identifier": ""}, {"control_port": 49160}, {"timing_port": 1}):
            with self.assertRaises(ValueError):
                validate_connection({**c, **change})

    async def test_encoder_queue_bounded_and_stop_drops_old_pcm(self):
        try:
            from assistant_runtime.plugins.airplay.output import AirPlayPlayback
        except ImportError:
            self.skipTest("optional AirPlay extra is absent")

        async def stream(*args, **kwargs):
            await asyncio.sleep(60)

        atv = SimpleNamespace(stream=SimpleNamespace(stream_file=stream), close=lambda: [])
        process = SimpleNamespace(
            returncode=None,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(feed_eof=Mock()),
            kill=Mock(),
            wait=AsyncMock(),
        )
        owner = SimpleNamespace(last_error=None)
        p = AirPlayPlayback(atv, process, 24000, owner)
        for _ in range(75):
            await p.write(bytes(960))
        with self.assertRaisesRegex(RuntimeError, "1.5"):
            await p.write(bytes(960))
        await p.close()
        self.assertFalse(p.healthy)
        self.assertTrue(p.queue.empty())
        self.assertTrue(p.encoder_task.done() and p.stream_task.done())
        with self.assertRaises(RuntimeError):
            await p.write(bytes(960))
