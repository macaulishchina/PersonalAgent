"""Version enforcement precedes imports; upgrade inspection never runs candidate code."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
import asyncio

from assistant_runtime.api import Manifest, PluginDefinition
from assistant_runtime.app.versions import (
    CORE_VERSION,
    CompatibilityError,
    descriptor,
    check_dependencies,
    framework,
)
from assistant_runtime.app.extensions import discover
from assistant_runtime.app.config import Config
from assistant_runtime.app.release_package import preflight, read_wheel
from assistant_runtime.app.release_inventory import inspect
from assistant_runtime.app import release_state
from release_fixture import release_data

SOURCE = Path(__file__).resolve().parents[1]


def wheel(root, target="0.3.1", *, sample=None):
    package = "personal_agent_sample" if sample else "personal_agent"
    v = sample["version"] if sample else target
    path = root / f"{package}-{v}-py3-none-any.whl"
    dist = f"{package}-{v}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            dist + "/METADATA",
            f"Metadata-Version: 2.1\nName: {package.replace('_', '-')}\nVersion: {v}\nRequires-Python: >=3.12\n",
        )
        if sample:
            archive.writestr(
                dist + "/entry_points.txt", "[personal_agent.plugins]\nsample = sample:definition\n"
            )
            archive.writestr("sample/plugin.json", json.dumps(sample))
            archive.writestr("sample/__init__.py", "raise AssertionError('candidate executed')")
        else:
            base = SOURCE / "src/assistant_runtime"
            for name in ("release.json", "owners.json"):
                archive.write(base / name, "assistant_runtime/" + name)
            archive.writestr("assistant_runtime/VERSION", target)
            for path_in_source in base.rglob("*.json"):
                if path_in_source.name == "plugin.json" or path_in_source.name.endswith(".plugin.json"):
                    archive.write(
                        path_in_source, "assistant_runtime/" + path_in_source.relative_to(base).as_posix()
                    )
    return path


class VersionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def entry(self, data, definition=None):
        path = self.root / "sample.plugin.json"
        path.write_text(json.dumps(data))
        return SimpleNamespace(
            name="sample",
            value="sample:definition",
            dist=SimpleNamespace(
                files=["sample.plugin.json"],
                locate_file=lambda name: path,
                version=data.get("version", "1.0.0"),
            ),
            load=Mock(return_value=definition or PluginDefinition(Manifest("sample"), create=lambda c: None)),
        )

    def test_invalid_contracts_cannot_default_to_current_compatibility(self):
        for changes in (
            {"requires_core": ""},
            {"requires_core": ">=0.3.0"},
            {"requires_core": ">=secret"},
            {"api_version": True},
            {"version": "1.0"},
            {"version": "1.0.0rc1"},
            {"requires": {"sample": ">=1.0.0"}},
            {"optional": {"a": ">=1.0.0"}, "requires": {"a": ">=1.0.0"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                descriptor(release_data(**changes))
        data = release_data()
        del data["api_version"]
        with self.assertRaises(ValueError):
            descriptor(data)

    def test_numeric_ranges_and_optional_dependencies_share_rules(self):
        records = {
            "a": descriptor(release_data("a", version="1.10.0")),
            "b": descriptor(release_data("b", optional={"a": ">=1.9.0,<2.0.0"})),
        }
        check_dependencies(records, ["b"])
        check_dependencies(records, ["a", "b"])
        records["a"]["version"] = "2.0.0"
        with self.assertRaisesRegex(CompatibilityError, "b requires a"):
            check_dependencies(records, ["a", "b"])
        records["b"]["requires"] = {"missing": ">=1.0.0"}
        with self.assertRaisesRegex(CompatibilityError, "missing"):
            check_dependencies(records, ["b"])

    def test_core_and_api_incompatibility_rejected_before_import_and_config_save(self):
        for changes in ({"requires_core": ">=0.4.0,<0.5.0"}, {"api_version": 2}):
            entry = self.entry(release_data(**changes))
            with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
                with self.assertRaises(CompatibilityError):
                    Config(root=self.root, plugins=["sample"])
                with self.assertRaises(CompatibilityError):
                    discover(["sample"])
            entry.load.assert_not_called()

    def test_runtime_distribution_and_static_versions_must_agree(self):
        entry = self.entry(release_data(version="1.1.0"))
        with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(CompatibilityError, "declarations differ"):
                discover(["sample"])
            entry.dist.version = "1.2.0"
            entry.load.reset_mock()
            with self.assertRaisesRegex(CompatibilityError, "invalid release"):
                discover(["sample"])
            entry.load.assert_not_called()

    def test_disabled_incompatible_plugin_is_visible_and_removable(self):
        records = {"sample": descriptor(release_data(requires_core=">=0.4.0,<0.5.0"))}
        self.assertFalse(inspect({"plugins": ["sample"]}, records)["compatible"])
        report = inspect({"plugins": []}, records)
        self.assertTrue(report["compatible"])
        self.assertFalse(report["plugins"][0]["compatible"])

    def test_static_inventory_retains_selected_backend_and_dependency_ranges(self):
        from assistant_runtime.api.system import system

        info = descriptor(
            release_data(
                backends={"linux": ["first", "second"], "windows": ["first", "second"]},
                default_backends={"linux": "first", "windows": "first"},
                optional={"unused": ">=1.0.0,<2.0.0"},
            )
        )
        with patch(
            "assistant_runtime.core.plugin_catalog.declaration",
            side_effect=AssertionError("inventory imported implementation"),
        ):
            result = inspect(
                {"plugins": ["sample"], "plugin_backends": {"sample": {system.name: "second"}}},
                {"sample": info},
            )
        self.assertTrue(result["compatible"])
        self.assertEqual(result["plugins"][0]["backend"], "second")
        self.assertEqual(result["plugins"][0]["optional"], {"unused": ">=1.0.0,<2.0.0"})
        self.assertTrue(result["plugins"][0]["platform_supported"])

    def test_incompatible_dependency_marks_its_dependent(self):
        records = {
            "a": descriptor(release_data("a", api_version=2)),
            "b": descriptor(release_data("b", requires={"a": ">=1.0.0,<2.0.0"})),
        }
        result = inspect({"plugins": ["a", "b"]}, records)
        self.assertEqual(len(result["blockers"]), 2)

    def test_preflight_uses_target_versions_without_import_or_install(self):
        entry = self.root / "config.toml"
        entry.write_text(
            'plugins = ["web", "conversation", "execution", "workfiles", "pi", "models", "ai_usage"]'
        )
        with patch("assistant_runtime.app.release_inventory.releases", return_value=({}, {})):
            result = preflight(entry, wheel(self.root))
        self.assertTrue(result["compatible"], result)
        self.assertEqual(result["core"]["version"], "0.3.1")
        self.assertFalse(result["installation_performed"])
        self.assertFalse((self.root / "state/release-state.json").exists())
        self.assertEqual(len(result["artifacts"][0]["sha256"]), 64)
        with patch("assistant_runtime.app.release_inventory.releases", return_value=({}, {})):
            result = preflight(entry, wheel(self.root, "0.4.0"))
        self.assertFalse(result["compatible"])
        self.assertTrue(any(b["plugin"] == "web" for b in result["blockers"]))

    def test_candidate_extension_can_resolve_conflict_without_running_its_code(self):
        entry = self.root / "config.toml"
        entry.write_text('plugins = ["sample"]')
        old = descriptor(release_data(requires_core=">=0.2.0,<0.3.0"))
        target = wheel(self.root)
        replacement = wheel(self.root, sample=release_data(version="1.1.0"))
        with patch("assistant_runtime.app.release_inventory.releases", return_value=({"sample": old}, {})):
            self.assertFalse(preflight(entry, target)["compatible"])
            result = preflight(entry, target, [replacement])
            self.assertTrue(result["compatible"], result)
            self.assertEqual(next(p["version"] for p in result["plugins"] if p["name"] == "sample"), "1.1.0")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                preflight(entry, target, [replacement, replacement])

    def test_wheel_version_mismatch_is_rejected(self):
        path = wheel(self.root)
        changed = path.with_name("personal_agent-9.0.0-py3-none-any.whl")
        path.rename(changed)
        with self.assertRaisesRegex(ValueError, "differ"):
            read_wheel(changed, framework_package=True)

    def test_replacement_distribution_cannot_retain_removed_plugin(self):
        entry = self.root / "config.toml"
        entry.write_text('plugins = ["removed"]')
        installed = SimpleNamespace(
            name="removed", dist=SimpleNamespace(metadata={"Name": "personal-agent-sample"})
        )
        with (
            patch(
                "assistant_runtime.app.release_inventory.releases",
                return_value=({"removed": descriptor(release_data("removed"))}, {}),
            ),
            patch("assistant_runtime.app.extensions.entry_points", return_value=[installed]),
        ):
            result = preflight(
                entry, wheel(self.root), [wheel(self.root, sample=release_data(version="1.1.0"))]
            )
        self.assertFalse(result["compatible"])
        self.assertEqual(result["blockers"][0]["plugin"], "removed")
        self.assertIn({"plugin": "removed", "from": "1.0.0", "to": None}, result["changes"])

    def test_candidate_cannot_shadow_another_installed_distribution(self):
        entry = self.root / "config.toml"
        entry.write_text('plugins = ["sample"]')
        installed = SimpleNamespace(name="sample", dist=SimpleNamespace(metadata={"Name": "another-sample"}))
        with (
            patch(
                "assistant_runtime.app.release_inventory.releases",
                return_value=({"sample": descriptor(release_data())}, {}),
            ),
            patch("assistant_runtime.app.extensions.entry_points", return_value=[installed]),
        ):
            target = wheel(self.root)
            replacement = wheel(self.root, sample=release_data(version="1.1.0"))
            result = preflight(entry, target, [replacement])
            self.assertFalse(result["compatible"])
            self.assertIn("conflicts", result["blockers"][0]["reason"])
            installed.dist.metadata["Name"] = "personal-agent-sample"
            self.assertTrue(preflight(entry, target, [replacement])["compatible"])

    def test_corrupt_wheel_has_a_controlled_diagnostic(self):
        path = self.root / "personal_agent-0.3.0-py3-none-any.whl"
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "Invalid candidate wheel"):
            read_wheel(path, framework_package=True)

    def test_duplicate_descriptor_fields_fail_before_import(self):
        from assistant_runtime.api.releases import decode_json

        with self.assertRaisesRegex(ValueError, "Duplicate"):
            decode_json('{"version":"1.0.0","version":"2.0.0"}')

    def test_start_attempt_preserves_last_success_and_blocks_unsafe_downgrade(self):
        (self.root / "state").mkdir()
        release_state.attempted(self.root, {"web": "1.0.0"})
        release_state.successful(self.root, {"web": "1.0.0"})
        previous = release_state.read(self.root)["last_successful"]
        newer = {**framework(), "version": "0.3.1"}
        with patch("assistant_runtime.app.release_state.framework", return_value=newer):
            release_state.attempted(self.root)
        self.assertEqual(release_state.read(self.root)["last_successful"], previous)
        with self.assertRaisesRegex(CompatibilityError, "downgrade"):
            release_state.check(self.root)
        with self.assertRaisesRegex(CompatibilityError, "migration"):
            release_state.check(self.root, {**newer, "data_format": 2})

    def test_corrupted_guard_blocks_before_database_open(self):
        from assistant_runtime.app.host import AssistantService

        config = Config(root=self.root, plugins=[])
        (self.root / "state").mkdir()
        path = self.root / "state/release-state.json"
        path.write_text('{"schema_version":99}')
        with patch("assistant_runtime.app.host.Store") as store:
            with self.assertRaises(CompatibilityError):
                AssistantService(config)
            store.assert_not_called()

    def test_extension_downgrade_remains_blocked_after_disable(self):
        (self.root / "state").mkdir()
        release_state.attempted(self.root, {"sample": "1.2.0"})
        release_state.attempted(self.root, {})
        with self.assertRaisesRegex(CompatibilityError, "sample.*downgrade"):
            release_state.check(self.root, plugins={"sample": "1.1.0"})
        release_state.check(self.root, plugins={"sample": "1.2.1"})

    def test_source_release_gate(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("release_check", SOURCE / "scripts/check_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.validate(SOURCE)["core_version"], CORE_VERSION)


class VersionConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_presets_use_declared_runtime_versions_and_preserve_restart_guard(self):
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.core.plugin_catalog import release

        with tempfile.TemporaryDirectory() as folder:
            for preset in ("slim", "standard"):
                entry = initialize(Path(folder) / preset, preset=preset)
                config = Config.load(entry)
                for _ in range(2):
                    host = AssistantService(config)
                    try:
                        versions = {
                            name: plugin.manifest.version for name, plugin in host.runtime.plugins.items()
                        }
                        self.assertEqual(
                            versions, {name: release(name)["version"] for name in config.enabled_plugins}
                        )
                        release_state.successful(config.root, versions)
                    finally:
                        await host.runtime.close()
                        host.store.db.close()
                        host.lock_file.close()

    async def test_incompatible_existing_plugin_can_be_inspected_and_removed(self):
        from aiohttp.test_utils import TestClient, TestServer
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings
        from assistant_runtime.core.configuration.console import Console, web
        from assistant_runtime.core.plugin_catalog import release

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text('plugins = ["web"]\n')
            business = BusinessProcess(ManagementSettings(root, entry))
            console = Console(business)
            app = web.Application(middlewares=[console.guard])
            app.add_routes(
                [
                    web.post("/api/login", console.login),
                    web.get("/api/catalog", console.describe),
                    web.get("/api/configuration", console.read),
                    web.post("/api/configuration", console.save),
                ]
            )

            def candidate(name):
                result = release(name)
                if name == "web":
                    result["requires_core"] = ">=0.4.0,<0.5.0"
                return result

            with patch("assistant_runtime.core.plugin_catalog.release", side_effect=candidate):
                async with TestClient(TestServer(app)) as client:
                    origin = {"Origin": str(client.make_url("/")).rstrip("/")}
                    response = await client.post(
                        "/api/login", json={"token": console.access.path.read_text().strip()}, headers=origin
                    )
                    self.assertEqual(response.status, 200)
                    catalog = await (await client.get("/api/catalog")).json()
                    row = next(p for p in catalog["plugins"] if p["id"] == "web")
                    self.assertFalse(row["version_compatible"])
                    self.assertIn("0.4.0", row["version_error"])
                    snapshot = await (await client.get("/api/configuration")).json()
                    original = entry.read_bytes()
                    for enabled, status in ((["web"], 400), ([], 200)):
                        response = await client.post(
                            "/api/configuration",
                            headers=origin,
                            json={
                                "version": snapshot["version"],
                                "patches": [{"path": ["plugins"], "op": "set", "value": enabled}],
                            },
                        )
                        self.assertEqual(response.status, status, await response.text())
                        if status == 400:
                            self.assertEqual((await response.json())["code"], "version_incompatible")
                            self.assertEqual(entry.read_bytes(), original)
                    self.assertEqual(Config.load(entry).plugins, [])
            if business.apply_task:
                await asyncio.wait_for(business.apply_task, 10)
