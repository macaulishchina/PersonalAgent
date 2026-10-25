"""Opt-in hardware levels, physical routing and failures before PCM delivery."""

import asyncio
import copy
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from assistant_runtime.plugins.pipewire.audio import AudioRoute
from assistant_runtime.plugins.pipewire.devices import validate_device
from assistant_runtime.plugins.pipewire.io import PipeWireEndpoint
from assistant_runtime.plugins.pipewire.stream_audio import StreamPlayer
from assistant_runtime.plugins.pipewire.volume import apply_hardware_volume
from test_behavior import device_config


def spec(kind="speaker", percent=100):
    return {
        "adapter": "pipewire",
        "kind": kind,
        "connection": {
            ("sink" if kind == "speaker" else "match"): "configured-device",
            "hardware_volume": {
                "control": "PCM Playback Volume" if kind == "speaker" else "Mic Capture Volume",
                "index": 1 if kind == "speaker" else 0,
                "percent": percent,
            },
        },
    }


def control(values="44", *, kind="INTEGER", access="rw---R--"):
    return (
        "numid=5,iface=MIXER,name='PCM Playback Volume',index=1\n"
        f"  ; type={kind},access={access},values={len(values.split(','))},min=0,max=151,step=0\n"
        f"  : values={values}\n"
    )


NODE = {"name": "configured-device", "properties": {"alsa.id": "test-usb-card", "alsa.card": "7"}}


class HardwareVolumeTest(unittest.IsolatedAsyncioTestCase):
    def test_optional_adapter_schema_rejects_invalid_levels_and_mixer_selectors(self):
        validate_device(spec())
        validate_device(spec("microphone", 0))
        for value in (
            None,
            False,
            {},
            {"control": "PCM", "percent": True},
            {"control": "PCM", "percent": -1},
            {"control": "PCM", "percent": 101},
            {"control": "PCM", "percent": 100, "index": True},
            {"control": "PCM", "percent": 100, "index": 256},
            {"control": "PCM',numid=1", "percent": 100},
            {"control": "PCM", "percent": 100, "card": "unrelated"},
        ):
            invalid = spec()
            invalid["connection"]["hardware_volume"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_device(invalid)

    async def test_unconfigured_devices_do_not_touch_hardware(self):
        value = spec()
        del value["connection"]["hardware_volume"]
        validate_device(value)
        with patch("assistant_runtime.plugins.pipewire.audio.command", new=AsyncMock()) as command:
            await apply_hardware_volume(value, {})
        command.assert_not_awaited()

    async def test_controls_follow_resolved_card_and_verify_all_channels(self):
        with patch(
            "assistant_runtime.plugins.pipewire.audio.command",
            new=AsyncMock(side_effect=[control("44,44"), control("151,151")]),
        ) as command:
            await apply_hardware_volume(spec(), NODE)
        read, write = command.await_args_list
        self.assertEqual(read.args[:4], ("amixer", "-c", "test-usb-card", "cget"))
        self.assertEqual(write.args[-1], "100%")
        self.assertIn("index=1", read.args[-1])
        self.assertNotIn("numid", read.args[-1])
        self.assertEqual(read.kwargs["timeout"], 1)

    async def test_missing_card_unreadable_controls_and_failed_writes_do_not_claim_success(self):
        for output in (control(kind="BOOLEAN"), control(access="r----R--")):
            with patch(
                "assistant_runtime.plugins.pipewire.audio.command", new=AsyncMock(return_value=output)
            ) as command:
                with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                    await apply_hardware_volume(spec(), NODE)
                self.assertEqual(command.await_count, 1)
        with patch(
            "assistant_runtime.plugins.pipewire.audio.command",
            new=AsyncMock(side_effect=[control(), control()]),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not apply"):
                await apply_hardware_volume(spec(), NODE)
        with patch("assistant_runtime.plugins.pipewire.audio.command", new=AsyncMock()) as command:
            with self.assertRaisesRegex(RuntimeError, "ALSA-backed"):
                await apply_hardware_volume(spec(), {"name": "network-device"})
            command.assert_not_awaited()

    async def test_output_open_failure_or_cancellation_closes_only_its_transport(self):
        transport = SimpleNamespace(healthy=True, close=AsyncMock())
        for failure in (RuntimeError("mixer unavailable"), asyncio.CancelledError()):
            with (
                patch(
                    "assistant_runtime.plugins.pipewire.io.PipeWirePlayback.open",
                    new=AsyncMock(return_value=transport),
                ),
                patch(
                    "assistant_runtime.plugins.pipewire.io.apply_hardware_volume",
                    new=AsyncMock(side_effect=failure),
                ),
            ):
                provider = PipeWireEndpoint(spec())
                provider.node = AsyncMock(return_value=NODE)
                with self.assertRaises(type(failure)):
                    await provider.open(24000)
        self.assertEqual(transport.close.await_count, 2)

    async def test_capture_policy_applies_after_open_and_before_pcm_delivery(self):
        order = []
        process = SimpleNamespace(returncode=None, terminate=Mock(), wait=AsyncMock())

        async def opened(*args, **kwargs):
            order.append("opened")
            return process

        async def volume(value, node):
            order.append("volume")

        with (
            patch("assistant_runtime.plugins.pipewire.io.system.spawn", side_effect=opened),
            patch("assistant_runtime.plugins.pipewire.io.audio_env", return_value={}),
            patch("assistant_runtime.plugins.pipewire.io.apply_hardware_volume", side_effect=volume),
        ):
            provider = PipeWireEndpoint(spec("microphone"))
            provider.node = AsyncMock(return_value=NODE)
            stream = await provider.open(16000)
            self.assertEqual(order, ["opened", "volume"])
            await stream.close()
            process.terminate.assert_called_once()

    async def test_aec_route_initializes_physical_sink_with_frozen_settings(self):
        config = device_config()
        config.devices[config.default_output]["connection"]["hardware_volume"] = spec()["connection"][
            "hardware_volume"
        ]
        route = AudioRoute(config, SimpleNamespace())
        expected = copy.deepcopy(route._output_spec)
        config.devices[config.default_output]["connection"]["hardware_volume"]["percent"] = 10
        route.sink, route.physical_sink = "private-echo-reference", "configured-device"
        node = {**NODE, "volume": {"mono": {"value": 65536}}}
        with (
            patch(
                "assistant_runtime.plugins.pipewire.audio.command",
                new=AsyncMock(return_value=json.dumps([node])),
            ),
            patch("assistant_runtime.plugins.pipewire.audio.apply_hardware_volume", new=AsyncMock()) as apply,
        ):
            await route.ensure_minimum_volume()
            await route.ensure_minimum_volume()
        self.assertTrue(route.hardware_volume_configured)
        self.assertEqual(apply.await_count, 2)
        apply.assert_awaited_with(expected, node)

    async def test_existing_voice_transport_resets_before_new_speech_without_reopening(self):
        for configured in (False, True):
            route = SimpleNamespace(
                ensure=AsyncMock(), hardware_volume_configured=configured, open_output=AsyncMock()
            )
            player = StreamPlayer(route)
            player.transport = SimpleNamespace(healthy=True, write=AsyncMock(), close=AsyncMock())
            player.last_voice_write = time.monotonic() - 1
            task = asyncio.create_task(player.run())
            try:
                for is_new_segment in (True, False, True):
                    if is_new_segment:
                        player.last_voice_write = time.monotonic() - 1
                    player.feed((200).to_bytes(2, "little", signed=True) * 480)
                    await asyncio.wait_for(player.queue.join(), 1)
                self.assertEqual(route.ensure.await_count, 2 if configured else 0)
                route.open_output.assert_not_awaited()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
