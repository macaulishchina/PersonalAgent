"""Hardware onboarding: identity, ownership, state truth and shared query routing."""

import asyncio
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.app.config import Config
from assistant_runtime.core.devices.directory import DeviceDirectory, DevicesPlugin
from assistant_runtime.api.contracts import DeviceObservation, DeviceQuery
from assistant_runtime.kernel.runtime import Runtime, Manifest
from assistant_runtime.plugins.pipewire.devices import PipeWireDevices, resolve
from assistant_runtime.app.store import Store
from test_behavior import device_config


class DirectoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = device_config()
        self.directory = DeviceDirectory(self.config.devices)
        self.probe = AsyncMock()
        for ident in self.config.devices:
            self.directory.register("pipewire", ident, ["audio.capture"], self.probe)

    def test_registration_checks_ownership_duplicates_and_disabled(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.directory.register("pipewire", "living", ["audio.playback"], self.probe)
        with self.assertRaises(ValueError):
            self.directory.observe("other", "living", DeviceObservation("available"))
        with self.assertRaises(ValueError):
            DeviceDirectory(self.config.devices).register("other", "living", ["read"], self.probe)
        disabled = copy.deepcopy(self.config.devices)
        disabled["living"]["enabled"] = False
        with self.assertRaises(ValueError):
            DeviceDirectory(disabled).register("pipewire", "living", ["read"], self.probe)

    def test_snapshot_is_freshness_aware_private_and_detached(self):
        self.directory.observe("pipewire", "living", DeviceObservation("available", {"volume_percent": 80}))
        snap = self.directory.snapshot("living")
        self.assertTrue(snap["fresh"])
        self.assertNotIn("connection", snap)
        snap["data"]["volume_percent"] = 0
        snap["capabilities"].clear()
        self.assertEqual(self.directory.snapshot("living")["data"]["volume_percent"], 80)
        _, received = self.directory.observations["living"]
        self.directory.observations["living"] = self.directory.observations["living"][0], received - 20
        self.assertFalse(self.directory.snapshot("living")["fresh"])
        self.assertFalse(self.directory.snapshot("study")["fresh"])

    async def test_failed_probe_never_returns_old_available_state(self):
        self.directory.observe("pipewire", "living", DeviceObservation("available"))
        self.probe.side_effect = RuntimeError("transport offline")
        result = await self.directory.handle(DeviceQuery("inspect", "living"))
        self.assertEqual(result["device"]["state"], "error")
        self.assertIn("offline", result["device"]["error"])

    async def test_unknown_query_and_device_fail_closed(self):
        for request in (DeviceQuery("inspect", "missing"), DeviceQuery("execute", "living")):
            with self.assertRaises(ValueError):
                await self.directory.handle(request)
        self.probe.assert_not_awaited()

    async def test_timeout_is_bounded_by_existing_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.db")
            runtime = Runtime(store)

            class Owner:
                manifest = Manifest("pipewire")

                async def start(self, context):
                    pass

            runtime.add(Owner())
            runtime.add(DevicesPlugin(self.directory))
            await runtime.start()

            async def blocked():
                await asyncio.sleep(60)

            self.directory.probes["living"] = blocked
            with self.assertRaises(TimeoutError):
                await runtime.invoke(DeviceQuery("inspect", "living"), timeout=0.01)
            self.assertEqual(self.directory.snapshot("living")["state"], "error")
            await runtime.close()
            store.db.close()

    async def test_missing_registration_fails_onboarding(self):
        plugin = DevicesPlugin(DeviceDirectory(self.config.devices))
        with self.assertRaisesRegex(ValueError, "not registered"):
            await plugin.start(None)


class PipeWireIdentityTest(unittest.IsolatedAsyncioTestCase):
    def test_stable_identity_excludes_monitors_and_transient_indexes(self):
        config = device_config()
        nodes = [
            {"index": 900, "name": "alsa_input.usb_UGREEN_Camera_2K"},
            {"index": 1, "name": "alsa_input.usb_UGREEN_Camera_2K.monitor"},
        ]
        self.assertEqual(resolve(config.devices["living_microphone"], nodes, [])["index"], 900)
        nodes[0]["index"] = 2000
        self.assertEqual(resolve(config.devices["living_microphone"], nodes, [])["index"], 2000)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            resolve(config.devices["living_microphone"], [nodes[0], {"name": "another_UGREEN_Camera_2K"}], [])

    async def test_missing_output_never_becomes_other_room(self):
        config = device_config()
        directory = DeviceDirectory(config.devices)
        from types import SimpleNamespace

        route = SimpleNamespace(
            input_device="living_microphone",
            output_device="living",
            capture=None,
            ready=asyncio.Event(),
            devices=AsyncMock(
                return_value=(
                    [{"name": "alsa_input.usb_UGREEN_Camera_2K"}],
                    [{"name": "bluez_output.8C_53_C3_7F_18_7C.1", "volume": {"mono": {"value": 50000}}}],
                )
            ),
        )
        adapter = PipeWireDevices(config, route, directory)
        adapter.register()
        await adapter.refresh()
        self.assertEqual(directory.snapshot("living")["state"], "unavailable")
        self.assertEqual(directory.snapshot("study")["state"], "available")
        self.assertFalse(directory.snapshot("study")["data"]["voice_bound"])
        self.assertEqual(directory.snapshot("living_microphone")["state"], "available")
        self.assertFalse(directory.snapshot("living_microphone")["data"]["capturing"])

    def test_protocol_connection_validation_is_in_adapter(self):
        config = device_config()
        for bad in ({"address": "bad-address"}, {"address": "4C:C6:4C:97:78:4A", "sink": "other"}):
            config.devices["living"]["connection"] = bad
            from types import SimpleNamespace

            with self.assertRaises(ValueError):
                PipeWireDevices(config, SimpleNamespace(), DeviceDirectory(config.devices))


class DeviceConfigTest(unittest.TestCase):
    def test_schema_and_voice_selectors(self):
        cfg = device_config()
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "config.toml"
            p.write_text('profile="voice"\ninput_device="unknown"\ndefault_output="unknown"\n')
            with self.assertRaises(ValueError):
                Config.load(p)
        for key, value in [
            ("label", ""),
            ("enabled", "yes"),
            ("max_age_seconds", 0),
            ("adapter", "not-enabled"),
        ]:
            values = copy.deepcopy(cfg.devices)
            values["living"][key] = value
            with self.assertRaises(ValueError):
                Config(devices=values)

    def test_context_describes_capabilities_without_connection_details(self):
        directory = DeviceDirectory(device_config().devices)
        directory.register("pipewire", "living", ["audio.playback"], AsyncMock())
        self.assertIn("audio.playback", directory.context_text())
        self.assertNotIn("4C:C6", directory.context_text())


class DeviceNameTest(unittest.TestCase):
    def test_device_names_do_not_activate_voice_route_switching(self):
        from assistant_runtime.core.speech.commands import parse_command

        for text in ("切换到客厅音箱", "切换到客厅小爱音箱", "切换到客厅HomePod"):
            self.assertIsNone(parse_command(text))
