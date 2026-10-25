"""Platform identity and declarative backend selection, before device construction."""

from release_fixture import release_data

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from assistant_runtime.api import BackendSpec, Manifest, PlatformInfo, PluginDefinition
from assistant_runtime.api.backends import plugin_backends, select_backend
from assistant_runtime.api.system import system
from assistant_runtime.app.config import Config
from assistant_runtime.app.extensions import InstalledPlugin, ToolRegistry, discover
from assistant_runtime.kernel.runtime import Runtime


class SelectionTest(unittest.TestCase):
    def test_multiple_implementations_use_configured_platform_default(self):
        spec = BackendSpec({"linux": ("native",), "windows": ("first", "second")}, {"windows": "first"})
        self.assertEqual(spec.select("linux"), "native")
        self.assertEqual(spec.select("windows"), "first")
        self.assertEqual(spec.select("windows", {"linux": "unavailable", "windows": "second"}), "second")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            spec.select("windows", {"windows": "removed"})
        with self.assertRaisesRegex(ValueError, "No implementation"):
            spec.select("darwin")
        with self.assertRaisesRegex(ValueError, "configure a default"):
            BackendSpec({"windows": ("first", "second")}).select("windows")
        with self.assertRaisesRegex(ValueError, "does not declare"):
            select_backend("simple", None, "windows", {"windows": "first"})
        self.assertIsNone(select_backend("simple", None, "linux", {"windows": "first"}))
        with self.assertRaises(TypeError):
            spec.defaults["windows"] = "second"
        with self.assertRaises(FrozenInstanceError):
            PlatformInfo("windows").name = "linux"

    def test_invalid_metadata_is_rejected_without_loading_implementation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plugin.json"
            for data in (
                [],
                {"backends": {}},
                {"default_backends": {}},
                {"backends": {"windows": ["same", "same"]}},
                {"backends": {"windows": ["one"]}, "default_backends": {"windows": "absent"}},
            ):
                path.write_text(json.dumps(data))
                with self.subTest(data=data), self.assertRaises(ValueError):
                    plugin_backends(path)

    def test_external_static_and_runtime_backend_declarations_must_match(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample/plugin.json"
            path.parent.mkdir()
            path.write_text(json.dumps({**release_data(), "backends": {"windows": ["one"]}}))
            entry = SimpleNamespace(
                name="sample",
                value="sample:definition",
                dist=SimpleNamespace(
                    files=["sample/plugin.json"], locate_file=lambda name: Path(folder) / str(name)
                ),
                load=Mock(return_value=PluginDefinition(Manifest("sample"), create=lambda api: None)),
            )
            with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
                with self.assertRaisesRegex(ValueError, "backend declarations differ"):
                    discover(["sample"])
                path.write_text('{**release_data(), "backends": []}')
                entry.load.reset_mock()
                with self.assertRaises(ValueError):
                    discover(["sample"])
                entry.load.assert_not_called()

    def test_config_retains_dormant_defaults_but_enabled_invalid_choice_fails_before_factory(self):
        with patch.object(system, "name", "linux"):
            config = Config(plugins=[], plugin_backends={"pipewire": {"linux": "removed"}})
            self.assertEqual(config.plugin_backends["pipewire"]["linux"], "removed")
            with patch(
                "assistant_runtime.core.plugin_catalog.declaration",
                side_effect=AssertionError("factory import"),
            ):
                with self.assertRaisesRegex(ValueError, "pipewire.*unavailable"):
                    Config(plugins=["pipewire"], plugin_backends=config.plugin_backends)
            config = Config(plugins=["pipewire"], plugin_backends={"pipewire": {"windows": "removed"}})
            self.assertIn("windows", config.plugin_backends["pipewire"])

    def test_manually_mutated_config_is_rechecked_before_resource_construction(self):
        from assistant_runtime.app.bootstrap import compose

        config = Config(plugins=[])
        config.plugins = ["devices", "pipewire"]
        config.plugin_backends = {"pipewire": {system.name: "removed"}}
        with patch(
            "assistant_runtime.core.devices.plugin.DeviceServices",
            side_effect=AssertionError("resource construction"),
        ):
            with self.assertRaisesRegex(ValueError, "pipewire.*unavailable"):
                compose(SimpleNamespace(config=config, store=None))

    def test_external_selection_is_identical_in_config_and_configuration_service(self):
        from assistant_runtime.core.configuration.validation import validate_candidate

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "sample/plugin.json"
            path.parent.mkdir()
            path.write_text(
                json.dumps(
                    {
                        **release_data(),
                        "backends": {system.name: ["one", "two"]},
                    }
                )
            )
            definition = PluginDefinition(
                Manifest("sample"),
                create=Mock(),
                backends=BackendSpec({system.name: ("one", "two")}),
            )
            entry = SimpleNamespace(
                name="sample",
                value="sample:definition",
                dist=SimpleNamespace(files=["sample/plugin.json"], locate_file=lambda name: root / str(name)),
                load=Mock(return_value=definition),
            )
            with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
                for choice in (None, "removed", "two"):
                    defaults = {"sample": {system.name: choice}} if choice else {}
                    text = 'plugins = ["sample"]\n'
                    if choice:
                        text += f'[plugin_backends.sample]\n{system.name} = "{choice}"\n'
                    for read in (
                        lambda: Config(root=root, plugins=["sample"], plugin_backends=defaults),
                        lambda: validate_candidate("config.toml", {"config.toml": text.encode()}, root=root),
                    ):
                        with self.subTest(choice=choice, reader=read):
                            entry.load.reset_mock()
                            if choice == "two":
                                self.assertEqual(read().plugin_backends, defaults)
                            else:
                                with self.assertRaisesRegex(
                                    ValueError, "sample.*(configure a default|unavailable)"
                                ):
                                    read()
                                entry.load.assert_not_called()
                definition.create.assert_not_called()

    def test_disabling_external_plugin_can_remove_its_required_backend_preference(self):
        from assistant_runtime.app.configuration_updates import ConfigurationRuntime

        original = Config(plugins=[])
        original.plugins = ["sample"]
        original.plugin_backends = {"sample": {"windows": "one"}}
        host = SimpleNamespace(
            config=original,
            runtime=SimpleNamespace(
                platform=PlatformInfo("windows"),
                contexts={},
                plugins={
                    "sample": SimpleNamespace(
                        definition=PluginDefinition(
                            Manifest("sample"),
                            create=lambda api: None,
                            backends=BackendSpec({"windows": ("one", "two")}),
                        )
                    )
                },
            ),
        )
        candidate = deepcopy(original)
        candidate.plugins = []
        candidate.plugin_backends = {}
        plan = ConfigurationRuntime(host).plan(candidate)
        self.assertIn("plugins", plan["restart"])
        self.assertNotIn("sample", plan["owners"])

    def test_update_plan_compares_effective_backend_and_preserves_voice_boundary(self):
        from dataclasses import replace
        from assistant_runtime.app.configuration_updates import ConfigurationRuntime
        from assistant_runtime.core.plugin_catalog import declaration

        policy = BackendSpec({"linux": ("native",), "windows": ("first", "second")}, {"windows": "first"})
        original = Config(plugins=[])
        original.plugins = ["pipewire"]
        host = SimpleNamespace(
            config=original,
            runtime=SimpleNamespace(
                plugins={"pipewire": SimpleNamespace(prepare_configuration=Mock())},
                contexts={},
                platform=PlatformInfo("windows"),
            ),
        )
        update = ConfigurationRuntime(host)
        spec = replace(declaration("pipewire"), backends=policy)
        with (
            patch.object(system, "name", "windows"),
            patch(
                "assistant_runtime.app.configuration_updates.declaration",
                side_effect=lambda name: spec if name == "pipewire" else declaration(name),
            ),
        ):
            for configured in ({"windows": "first"}, {"linux": "removed"}):
                candidate = deepcopy(original)
                candidate.plugin_backends = {"pipewire": configured}
                plan = update.plan(candidate)
                self.assertEqual(plan["owners"], [])
                self.assertEqual(plan["restart"], [])
            candidate = deepcopy(original)
            candidate.plugin_backends = {"pipewire": {"windows": "second"}}
            self.assertEqual(update.plan(candidate)["owners"], ["pipewire"])
            original.plugins.append("assistant")
            candidate.plugins.append("assistant")
            self.assertEqual(update.plan(candidate)["restart"], ["plugin_backends"])


class ContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_portable_bundled_plugin_uses_runtime_identity_without_backend_policy(self):
        from assistant_runtime.app.bundled import create

        with tempfile.TemporaryDirectory() as folder, patch.object(system, "name", "windows"):
            runtime = Runtime(None, platform=PlatformInfo("linux"))
            host = SimpleNamespace(config=Config(root=Path(folder), plugins=[]), runtime=runtime)
            observed = []

            def factory(api):
                observed.append(api)
                return SimpleNamespace(manifest=Manifest("gesture"), start=lambda context: asyncio.sleep(0))

            plugin = create(host, "gesture", factory)
            self.assertIs(observed[0].platform, runtime.platform)
            self.assertEqual(observed[0].platform.name, "linux")
            self.assertIsNone(observed[0].backend)
            runtime.add(plugin)
            try:
                await runtime.start()
                self.assertIs(runtime.contexts["gesture"].platform, observed[0].platform)
                with self.assertRaises(AttributeError):
                    runtime.platform = PlatformInfo("windows")
            finally:
                await runtime.close()

    async def test_public_plugin_gets_same_platform_and_selected_backend_as_runtime(self):
        observed = []

        def create(api):
            observed.append((api.platform, api.backend))
            return SimpleNamespace(start=lambda context: asyncio.sleep(0))

        with tempfile.TemporaryDirectory() as folder:
            runtime = Runtime(None, platform=PlatformInfo("windows"))
            runtime.add(
                InstalledPlugin(
                    PluginDefinition(
                        Manifest("sample"),
                        create=create,
                        backends=BackendSpec({"windows": ("one", "two")}, {"windows": "one"}),
                    ),
                    {},
                    Path(folder),
                    lambda key: None,
                    (),
                    ToolRegistry(),
                    backend_defaults={"windows": "two"},
                )
            )
            try:
                await runtime.start()
                self.assertEqual(observed, [(runtime.platform, "two")])
                self.assertIs(runtime.contexts["sample"].platform, runtime.platform)
                with self.assertRaises(AttributeError):
                    runtime.plugins["sample"].public.platform = PlatformInfo("linux")
            finally:
                await runtime.close()

    async def test_windows_factory_uses_stable_device_owner_and_cleans_registrations(self):
        from assistant_runtime.app.bundled import create
        from assistant_runtime.core.devices.plugin import DeviceServices

        devices = {
            "microphone": {
                "adapter": "pipewire",
                "kind": "microphone",
                "label": "Test mic",
                "connection": {"endpoint_id": "{0.1.0.00000000}.{00000000-0000-0000-0000-000000000001}"},
            },
            "camera": {
                "adapter": "v4l2",
                "kind": "camera",
                "label": "Test camera",
                "connection": {"device_path": r"@device_pnp_\\?\usb#vid_1234&pid_5678#absent"},
            },
        }
        with tempfile.TemporaryDirectory() as folder, patch.object(system, "name", "windows"):
            # Factory protocol test is independent of certification/native device acceptance.
            config = Config(root=Path(folder), plugins=[], devices=devices)
            config.plugins = ["devices", "pipewire", "v4l2"]
            runtime = Runtime(None, platform=PlatformInfo("windows"))
            host = SimpleNamespace(
                config=config,
                runtime=runtime,
                store=SimpleNamespace(get=lambda *a: None, set=lambda *a: None),
                voice_io=None,
            )
            services = DeviceServices(config)
            services.bind(host)
            for name in ("pipewire", "v4l2"):
                runtime.add(create(host, name))
            with (
                patch(
                    "assistant_runtime.plugins.pipewire.windows_audio.Endpoint.inspect",
                    side_effect=OSError("absent"),
                ),
                patch(
                    "assistant_runtime.plugins.v4l2.windows_camera.Camera.inspect",
                    side_effect=OSError("absent"),
                ),
            ):
                try:
                    await runtime.start()
                    await asyncio.sleep(0)
                    self.assertEqual(set(host.devices.entries), set(devices))
                    self.assertEqual(set(host.audio_inputs.providers), {"microphone"})
                    self.assertEqual(set(host.video_inputs.providers), {"camera"})
                    for ident in devices:
                        await host.devices.probes[ident]()
                        self.assertEqual(host.devices.observations[ident][0]["state"], "unavailable")
                    self.assertEqual(runtime.plugins["pipewire"].diagnostics()["backend"], "wasapi")
                finally:
                    await runtime.close()
            self.assertFalse(host.devices.entries)
            self.assertFalse(host.audio_inputs.providers)
            self.assertFalse(host.video_inputs.providers)
