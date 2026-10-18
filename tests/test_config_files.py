"""Configuration composition preserves one instance and rejects ambiguous input."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.config import Config, read_config


class ConfigFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "config"
        self.folder.mkdir()
        self.entry = self.folder / "config.toml"

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text)
        return path

    def test_split_config_equals_single_file_and_preserves_root_from_other_cwd(self):
        self.write("voice.toml", 'wake_words = ["你好"]\n')
        self.write(
            "models.toml",
            '[model_api]\nenabled=false\n[model_api.providers.test]\npreset="deepseek"\nenabled=false\n',
        )
        self.write("memory.toml", '[model_api]\nmemory=""\n')
        self.write(
            "config.toml",
            'root=".."\nprofile="headless"\nworkspace="/tmp"\ninclude=["voice.toml", "models.toml", "memory.toml"]\n',
        )
        # A file not named by include must never be loaded.
        self.write("backup.toml", 'profile="voice"\n')
        merged = read_config(self.entry)
        self.assertNotIn("include", merged)
        expected = Config(root=self.root, **{k: v for k, v in merged.items() if k != "root"})
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            actual = Config.load(self.entry)
        finally:
            os.chdir(previous)
        self.assertEqual(actual, expected)
        self.assertEqual(actual.state_dir, self.root / "state")
        with patch.dict(os.environ, {"PERSONAL_AGENT_CONFIG": str(self.entry)}):
            self.assertEqual(Config.load(), expected)

    def test_conflicts_report_key_and_both_sources_without_values(self):
        self.write("a.toml", '[model_api.providers.test]\nkey_env="SECRET_ONE"\n')
        self.write("b.toml", '[model_api.providers.test]\nkey_env="SECRET_TWO"\n')
        self.write("config.toml", 'include=["a.toml", "b.toml"]\n')
        with self.assertRaises(ValueError) as error:
            read_config(self.entry)
        message = str(error.exception)
        for value in ("model_api.providers.test.key_env", "a.toml", "b.toml"):
            self.assertIn(value, message)
        self.assertNotIn("SECRET_", message)
        for entry, fragment in (
            ("value=1", "[value]\nx=1"),
            ("[value]\nx=1", "value=1"),
            ("value=[1]", "value=[1]"),
        ):
            self.write("config.toml", 'include=["a.toml"]\n' + entry)
            self.write("a.toml", fragment)
            with self.assertRaisesRegex(ValueError, "Duplicate configuration value"):
                read_config(self.entry)

    def test_invalid_include_lists_and_duplicate_paths(self):
        self.write("a.toml", 'profile="headless"\n')
        for value in (
            '"a.toml"',
            "[1]",
            '[""]',
            '["a.toml", "./a.toml"]',
            '["config.toml"]',
            '["/tmp/external.toml"]',
            '["../outside.toml"]',
            str(["a.toml"] * 33).replace("'", '"'),
        ):
            with self.subTest(value=value):
                self.write("config.toml", "include=" + value)
                with self.assertRaises(ValueError):
                    read_config(self.entry)

    def test_nested_include_and_escaping_symlink_rejected(self):
        self.write("config.toml", 'include=["a.toml"]')
        self.write("a.toml", "include=[]")
        with self.assertRaisesRegex(ValueError, "Only the entry"):
            read_config(self.entry)
        (self.folder / "a.toml").unlink()
        outside = self.root / "outside.toml"
        outside.write_text('profile="headless"')
        (self.folder / "a.toml").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "must stay inside"):
            read_config(self.entry)

    def test_missing_or_invalid_files_and_unknown_fields_fail(self):
        with self.assertRaises(FileNotFoundError):
            Config.load(self.entry)
        self.write("config.toml", 'profile="headless"\ninclude=["missing.toml"]')
        with self.assertRaises(FileNotFoundError):
            Config.load(self.entry)
        self.write("missing.toml", 'secret = "unterminated')
        with self.assertRaisesRegex(ValueError, "Invalid TOML configuration.*missing.toml"):
            Config.load(self.entry)
        self.write("missing.toml", "typo=true")
        with self.assertRaisesRegex(ValueError, "Unknown config keys.*typo"):
            Config.load(self.entry)

    def test_candidate_validation_reads_includes_without_writing_entry(self):
        self.write("config.toml", 'profile="headless"\ninclude=["models.toml"]')
        self.write("models.toml", 'codex_model="selected"')
        before = self.entry.read_text()
        merged = read_config(self.entry, text=before + "\n[media]\nenabled=false\n")
        self.assertEqual(merged["codex_model"], "selected")
        self.assertFalse(merged["media"]["enabled"])
        self.assertEqual(self.entry.read_text(), before)

    def test_missing_implicit_xdg_config_remains_headless(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=True):
            self.assertEqual(Config.load().profile, "headless")

    def test_checked_in_split_example_loads(self):
        entry = Path(__file__).resolve().parents[1] / "config.example/config.toml"
        self.assertEqual(Config.load(entry).profile, "headless")
