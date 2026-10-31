"""Platform policy contracts; simulations are distinct from native startup acceptance."""

from release_fixture import release_data

import json
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from assistant_runtime.api import Manifest, PluginDefinition
from assistant_runtime.api.plugins import plugin_platforms, PlatformCertificationError
from assistant_runtime.api.system import system
from assistant_runtime.app.config import Config
from assistant_runtime.app.extensions import discover
from assistant_runtime.app.initialize import initialize
from assistant_runtime.core.plugin_catalog import OWNERS, declaration, platforms
from assistant_runtime.core.configuration.catalog import catalog


def uncertified(name):
    return ("linux",) if name in {"pipewire", "v4l2"} else platforms(name)


class CertificationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def entry(self, certified=("linux",), runtime=None, *, module=False):
        resource = "sample.plugin.json" if module else "sample/plugin.json"
        metadata = self.root / resource
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(json.dumps(release_data(supported_platforms=list(certified))), encoding="utf-8")
        return SimpleNamespace(
            name="sample",
            value="sample:definition",
            dist=SimpleNamespace(files=[resource], locate_file=lambda name: self.root / str(name)),
            load=Mock(
                return_value=PluginDefinition(
                    Manifest("sample"),
                    create=lambda context: None,
                    supported_platforms=tuple(runtime or certified),
                )
            ),
        )

    def test_bundled_platform_matrix_and_core_scope_is_unchanged(self):
        for name, module in OWNERS.items():
            expected = ("linux", "windows")
            with self.subTest(plugin=name):
                self.assertEqual(platforms(name), expected)
                self.assertEqual(declaration(name).supported_platforms, expected)
        for current in ("linux", "windows"):
            with patch.object(system, "name", current):
                rows = {p["id"]: p for p in catalog()["plugins"]}
                for name in ("web", "voice_disabled", "gesture"):
                    self.assertEqual(rows[name]["kind"], "extension")
                    self.assertEqual(rows[name]["platform_supported"], current in platforms(name))
                self.assertTrue(rows["pi"]["platform_supported"])

    def test_bad_or_missing_certification_never_defaults_to_all_platforms(self):
        path = self.root / "plugin.json"
        for content in (
            None,
            "not-json",
            "{}",
            "[]",
            '{"supported_platforms": []}',
            '{"supported_platforms": ["windows_server"]}',
            '{"supported_platforms": ["linux", "linux"]}',
            '{"supported_platforms": [{}]}',
        ):
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content)
                with self.assertRaises(ValueError):
                    plugin_platforms(path)

    def test_external_gate_precedes_import_and_metadata_is_visible_without_import(self):
        entry = self.entry()
        with (
            patch.object(system, "name", "windows"),
            patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]),
            patch("assistant_runtime.core.configuration.catalog.entry_points", return_value=[entry]),
        ):
            row = next(p for p in catalog()["plugins"] if p["id"] == "sample")
            self.assertFalse(row["platform_supported"])
            with self.assertRaisesRegex(PlatformCertificationError, "sample.*windows.*linux"):
                discover(["sample"])
            entry.load.assert_not_called()
            entry.dist.files = []
            row = next(p for p in catalog()["plugins"] if p["id"] == "sample")
            self.assertEqual(row["supported_platforms"], [])
            self.assertIn("platform_error", row)
            with self.assertRaisesRegex(ValueError, "certification"):
                discover(["sample"])
            entry.load.assert_not_called()

    def test_external_certification_matches_runtime_for_packages_and_modules(self):
        for module in (False, True):
            entry = self.entry(("linux", "windows"), module=module)
            with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
                self.assertEqual(discover(["sample"])["sample"].manifest.name, "sample")
            entry = self.entry(runtime=("linux", "windows"), module=module)
            with (
                patch.object(system, "name", "linux"),
                patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]),
            ):
                with self.assertRaisesRegex(ValueError, "certifications differ"):
                    discover(["sample"])

    def test_windows_configuration_and_mutated_composition_cannot_bypass_gate(self):
        from assistant_runtime.app.bootstrap import compose

        with (
            patch.object(system, "name", "windows"),
            patch("assistant_runtime.core.plugin_catalog.platforms", side_effect=uncertified),
        ):
            for name in ("pipewire", "v4l2"):
                with (
                    self.subTest(plugin=name),
                    patch(
                        "assistant_runtime.core.plugin_catalog.declaration",
                        side_effect=AssertionError("import"),
                    ),
                ):
                    with self.assertRaisesRegex(PlatformCertificationError, name):
                        Config(root=self.root, plugins=[name])
            config = Config(root=self.root, plugins=[])
            config.plugins = ["pipewire"]
            with self.assertRaisesRegex(PlatformCertificationError, "pipewire"):
                compose(SimpleNamespace(config=config, store=None))

    def test_disabling_external_plugin_retains_stored_settings_without_loading_it(self):
        entry = self.entry()
        resource = "sample/configuration.json"
        (self.root / resource).write_text(
            json.dumps({"version": 1, "namespace": "ignored", "schema": {"type": "object"}})
        )
        entry.dist.files.append(resource)
        with (
            patch.object(system, "name", "windows"),
            patch("assistant_runtime.core.configuration.catalog.entry_points", return_value=[entry]),
            patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]),
        ):
            config = Config(root=self.root, plugins=[], plugin_settings={"sample": {"option": "retained"}})
            self.assertEqual(config.plugin_settings["sample"], {"option": "retained"})
            entry.load.assert_not_called()
            with self.assertRaisesRegex(PlatformCertificationError, "sample.*windows"):
                Config(root=self.root, plugins=["sample"], plugin_settings=config.plugin_settings)
            entry.load.assert_not_called()

    def test_initialization_defaults_and_refusal_are_explicit_and_leave_no_files(self):
        with patch.object(system, "name", "windows"):
            config = Config.load(initialize(self.root / "windows"))
            self.assertNotIn("web", config.plugins)
            self.assertEqual(config.plugins, Config(root=self.root).plugins)
            self.assertIn("web", Config.load(initialize(self.root / "standard", preset="standard")).plugins)
        with patch.object(system, "name", "linux"):
            config = Config.load(initialize(self.root / "linux"))
            self.assertIn("web", config.plugins)
            self.assertIn("voice_disabled", config.plugins)

    def test_native_cli_uses_certification_for_initialization_and_hand_edited_config(self):
        root = self.root / "cli"
        result = subprocess.run(
            [sys.executable, "-m", "assistant_runtime.app.cli", "init", str(root)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        entry = root / "config/config.toml"
        config = Config.load(entry)
        self.assertEqual("web" in config.plugins, system.name == "linux")
        if system.name == "windows":
            entry.write_text(
                'root = ".."\nplugins = ["pipewire"]\n[plugin_backends.pipewire]\nwindows = "removed"\n'
            )
            result = subprocess.run(
                [sys.executable, "-m", "assistant_runtime.app.cli", "--config", str(entry), "plugins"],
                capture_output=True,
                timeout=20,
            )
            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertFalse(report["compatible"])
            self.assertIn("implementation selection", report["blockers"][0]["reason"])


class ConsoleCertificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_business_start_rejects_incompatible_config_without_spawning_worker(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(system, "name", "windows"),
            patch("assistant_runtime.core.plugin_catalog.platforms", side_effect=uncertified),
            patch("assistant_runtime.core.configuration.catalog.platforms", side_effect=uncertified),
        ):
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text('plugins = ["pipewire"]\n')
            business = BusinessProcess(ManagementSettings(root, entry))
            with patch.object(system, "spawn", side_effect=AssertionError("worker created")):
                await business.start()
            self.assertIsNone(business.process)
            self.assertEqual(business.outcome, "failed")

    async def test_incompatible_existing_config_can_be_read_and_repaired_but_not_enabled(self):
        from aiohttp.test_utils import TestClient, TestServer
        from assistant_runtime.core.configuration.console import Console, web
        from assistant_runtime.core.configuration.files import ConfigurationFiles
        from assistant_runtime.core.configuration.validation import validate_candidate

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(system, "name", "windows"),
            patch("assistant_runtime.core.plugin_catalog.platforms", side_effect=uncertified),
            patch("assistant_runtime.core.configuration.catalog.platforms", side_effect=uncertified),
        ):
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text('plugins = ["pipewire"]\n')
            files = ConfigurationFiles(
                entry, root / "private", lambda contents: validate_candidate(entry.name, contents, root=root)
            )
            business = SimpleNamespace(
                settings=SimpleNamespace(root=root),
                state=root / "private",
                files=files,
                application={},
                loaded_snapshot=None,
                loaded=None,
                process=None,
                update=lambda version: {"phase": "pending_start", "version": version},
            )
            console = Console(business)
            self.addAsyncCleanup(console.close)
            app = web.Application(middlewares=[console.guard])
            app.add_routes(
                [
                    web.post("/api/login", console.login),
                    web.get("/api/configuration", console.read),
                    web.post("/api/configuration", console.save),
                    web.get("/api/catalog", console.describe),
                ]
            )
            async with TestClient(TestServer(app)) as client:
                origin = {"Origin": str(client.make_url("/")).rstrip("/")}
                response = await client.post(
                    "/api/login", json={"token": console.access.path.read_text().strip()}, headers=origin
                )
                self.assertEqual(response.status, 200)
                response = await client.get("/api/catalog")
                rows = {p["id"]: p for p in (await response.json())["plugins"]}
                self.assertFalse(rows["pipewire"]["platform_supported"])
                response = await client.get("/api/configuration")
                self.assertEqual(response.status, 200)
                version = (await response.json())["version"]
                original = entry.read_bytes()
                for selected, status in ((["pipewire"], 400), (["v4l2"], 400), ([], 200)):
                    response = await client.post(
                        "/api/configuration",
                        headers=origin,
                        json={
                            "version": version,
                            "patches": [{"path": ["plugins"], "op": "set", "value": selected}],
                        },
                    )
                    self.assertEqual(response.status, status, await response.text())
                    if status == 400:
                        self.assertEqual((await response.json())["code"], "platform_not_certified")
                        self.assertEqual(entry.read_bytes(), original)
                self.assertEqual(Config.load(entry).plugins, [])
