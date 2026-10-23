"""Candidate conversion preserves instance data paths and all unrelated values."""

from pathlib import Path
import tempfile
import unittest

from assistant_runtime.app.config import Config, read_config
from assistant_runtime.app.prepare_config import prepare, set_field


class PrepareConfigTest(unittest.TestCase):
    def test_split_config_preserves_values_root_and_original_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "live" / "config.toml"
            source.parent.mkdir()
            original = 'include = ["services.toml"]\nroot = ".."\nmain_model = "unconfigured"\n'
            source.write_text(original)
            services = source.parent / "services.toml"
            service_text = 'plugins = [\n "pi", # comment containing ]\n "web",\n]\n[web]\nenable = false\n'
            services.write_text(service_text)
            result = prepare(source, root / "candidate")
            candidate = Path(result["config"])
            data = read_config(candidate)
            self.assertEqual(data["root"], str(root))
            self.assertTrue(
                {"ai_usage", "workfiles", "execution", "conversation", "assistant", "devices"}
                <= set(data["plugins"])
            )
            self.assertEqual(data["web"], {"enable": False})
            self.assertEqual(source.read_text(), original)
            self.assertEqual(services.read_text(), service_text)
            self.assertEqual((root / "candidate/original/services.toml").read_text(), service_text)
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
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
        original = '[web]\nenable = false\nplugins = ["nested"]\n'
        updated = set_field(original, "plugins", ["pi"])
        self.assertEqual(updated, 'plugins = ["pi"]\n' + original)
