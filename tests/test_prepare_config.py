"""Candidate conversion preserves instance data paths and all unrelated values."""

from platform_support import private_mode

from pathlib import Path
import tempfile
import unittest

from assistant_runtime.app.config import Config, read_config
from assistant_runtime.app.prepare_config import (
    migrate_model_connections,
    migrate_plugins,
    prepare,
    set_field,
)


class PrepareConfigTest(unittest.TestCase):
    def test_split_config_preserves_values_root_and_original_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "live" / "config.toml"
            source.parent.mkdir()
            original = 'include = ["services.toml"]\nroot = ".."\nmain_model = "unconfigured"\n'
            source.write_text(original)
            services = source.parent / "services.toml"
            service_text = 'plugins = [\n "pi", # comment containing ]\n "web",\n]\n[plugin_settings.web]\nenable = false\n'
            services.write_text(service_text)
            result = prepare(source, root / "candidate")
            candidate = Path(result["config"])
            data = read_config(candidate)
            self.assertEqual(data["root"], str(root))
            self.assertTrue(
                {"pi", "web", "ai_usage", "workfiles", "execution", "conversation", "cli"}
                <= set(data["plugins"])
            )
            self.assertTrue({"assistant", "devices", "voice_disabled"}.isdisjoint(data["plugins"]))
            self.assertEqual(data["plugin_settings"]["web"], {"enable": False})
            self.assertEqual(source.read_text(), original)
            self.assertEqual(services.read_text(), service_text)
            self.assertEqual((root / "candidate/original/services.toml").read_text(), service_text)
            self.assertEqual(private_mode(candidate), 0o600)
            self.assertEqual(Config.load(candidate).root, root)
            self.assertFalse(result["applied"])
            with self.assertRaisesRegex(ValueError, "must not exist"):
                prepare(source, root / "candidate")

    def test_unknown_plugin_and_duplicate_fields_do_not_create_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "config.toml", Path(folder) / "out"
            source.write_text('plugins = ["nonexistent_package"]\n')
            with self.assertRaises(ValueError):
                prepare(source, output)
            self.assertFalse(output.exists())
            source.write_text('plugins = ["pi"]\nplugins = []\n')
            with self.assertRaises(ValueError):
                prepare(source, output)
            self.assertFalse(output.exists())

    def test_only_changes_top_level_field(self):
        original = '[plugin_settings.web]\nenable = false\nplugins = ["nested"]\n'
        updated = set_field(original, "plugins", ["pi"])
        self.assertEqual(updated, 'plugins = ["pi"]\n' + original)

    def test_model_connection_preview_preserves_routes_and_unrelated_credentials(self):
        from assistant_runtime.core.models.settings import ModelSettings

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "config.toml"
            source.write_text('include = ["models.toml"]\nplugins = []\nmain_model = "fast"\n')
            models = root / "models.toml"
            models.write_text("""
[model_api.providers.go-responses]
preset = "opencode_go_responses"
enabled = true
[model_api.providers.go-messages]
preset = "opencode_go_anthropic"
enabled = true
[model_api.providers.other-key]
preset = "opencode_go_gemini"
key_env = "SEPARATE_API_KEY"
enabled = true
[model_api.entries.fast]
provider = "go-responses"
model = "synthetic"
[model_api.entries.deep]
provider = "go-messages"
model = "other-synthetic"
""")
            old_text = models.read_bytes()
            old = ModelSettings.parse(read_config(source)["model_api"])
            report = migrate_model_connections(source, root / "candidate")
            converted = read_config(report["config"])
            self.assertEqual(report["retained"], ["other-key"])
            self.assertEqual(converted["main_model"], "fast")
            new = ModelSettings.parse(converted["model_api"])
            self.assertEqual(set(new.providers), {"opencode_go", "other-key"})
            for alias in old.entries:
                self.assertEqual(new.route(alias)["revision"], old.route(alias)["revision"])
                self.assertEqual(new.provider_for(new.entries[alias]), old.provider_for(old.entries[alias]))
                self.assertEqual(new.entries[alias]["capabilities"], old.entries[alias]["capabilities"])
            self.assertEqual(models.read_bytes(), old_text)

    def test_05_migration_preserves_plugin_selection_split_fields_and_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "live/config.toml"
            entry.parent.mkdir()
            source = (
                'root = ".."\ninclude = ["settings.toml"]\nplugins = []\n'
                "[web]\nenable = false # existing setting\n[ai_passport]\nvolume = 62\n"
            )
            fragment = '[web]\nport = 8030\n[ai_passport.devices.example.wifi_networks."1"]\nssid = "lab"\npassword = "fixture-only"\n'
            entry.write_text(source)
            # Device identities remain unchanged and must still satisfy the PP schema.
            ident = "a" * 32
            fragment = fragment.replace(".example.", "." + ident + ".")
            (entry.parent / "settings.toml").write_text(fragment)
            result = migrate_plugins(entry, root / "candidate")
            self.assertCountEqual(result["migrated_plugins"], ["web", "ai_passport"])
            config = Config.load(result["config"])
            self.assertEqual(config.plugins, [])
            self.assertEqual(config.root, root)
            self.assertEqual(config.plugin_settings["web"], {"enable": False, "port": 8030})
            self.assertEqual(
                config.plugin_settings["ai_passport"]["devices"][ident]["wifi_networks"]["1"]["password"],
                "fixture-only",
            )
            self.assertIn("# existing setting", Path(result["config"]).read_text())
            self.assertEqual(entry.read_text(), source)
            self.assertEqual((Path(result["original"]) / "settings.toml").read_text(), fragment)

    def test_05_migration_rejects_old_and_new_namespaces_across_includes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text('include = ["new.toml"]\n[web]\nenable = false\n')
            (root / "new.toml").write_text("[plugin_settings.web]\nport = 8030\n")
            with self.assertRaisesRegex(ValueError, "Both old and new settings exist for web"):
                migrate_plugins(entry, root / "candidate")
            self.assertFalse((root / "candidate").exists())
