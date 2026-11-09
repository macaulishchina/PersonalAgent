"""Installation choices survive updates without modifying running application environments."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "pa_install_script", Path(__file__).resolve().parents[1] / "scripts/install.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pa install ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "pyproject.toml").write_text(
            "[project.optional-dependencies]\nslim=[]\nstandard=[]\nweb=[]\ndev=[]\nairplay=[]\n"
        )

    def test_platform_defaults_and_custom_environment_are_independent_of_cwd(self):
        args = installer.arguments([])
        target, options, commands = installer.plan(
            self.root, args, {"UV_PROJECT_ENVIRONMENT": "custom env"}, "win32"
        )
        self.assertEqual(target, self.root / "custom env")
        self.assertEqual(options["preset"], "slim")
        self.assertEqual(commands[0][1:], ["scripts/build_plugin_pages.py"])
        self.assertEqual(len(commands), 2)
        self.assertIn("--inexact", commands[-1])
        _, options, commands = installer.plan(self.root, args, {}, "linux")
        self.assertEqual(options["preset"], "standard")
        self.assertEqual([c[0] for c in commands[1:]], ["npm", "npm", "uv"])

    def test_preview_does_not_execute_or_create_environment(self):
        run = Mock(side_effect=AssertionError("preview performed installation"))
        installer.install(
            self.root, installer.arguments(["--dry-run"]), environment={}, platform="win32", run=run
        )
        run.assert_not_called()
        self.assertFalse((self.root / ".venv").exists())

    def test_success_remembers_preset_and_extras_and_retains_installed_plugins(self):
        run = Mock()
        with patch.object(installer.shutil, "which", side_effect=lambda name, **kw: name):
            installer.install(
                self.root,
                installer.arguments(["--preset", "standard", "--extra", "dev"]),
                environment={},
                platform="win32",
                run=run,
            )
        receipt = json.loads((self.root / ".venv/pa-install.json").read_text())
        self.assertEqual(receipt["extras"], ["dev"])
        _, choices, commands = installer.plan(
            self.root, installer.arguments(["--extra", "airplay"]), {}, "win32"
        )
        self.assertEqual(choices["preset"], "standard")
        self.assertEqual(choices["extras"], ["airplay", "dev"])
        self.assertIn("--inexact", commands[-1])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], self.root)
            self.assertEqual(call.kwargs["env"]["UV_PROJECT_ENVIRONMENT"], str(self.root / ".venv"))
            self.assertTrue(call.kwargs["check"])
        _, choices, _ = installer.plan(
            self.root, installer.arguments(["--preset", "slim", "--without-extra", "dev"]), {}, "win32"
        )
        self.assertEqual(choices, {"schema_version": 1, "preset": "slim", "extras": []})

    def test_failure_does_not_record_candidate_or_run_later_steps(self):
        environment = self.root / ".venv"
        environment.mkdir()
        receipt = environment / "pa-install.json"
        previous = '{"schema_version":1,"preset":"slim","extras":[]}'
        receipt.write_text(previous)
        for index in range(4):
            with self.subTest(index=index):
                run = Mock(side_effect=[None] * index + [subprocess.CalledProcessError(7, "failed")])
                with patch.object(installer.shutil, "which", return_value="tool"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        installer.install(
                            self.root,
                            installer.arguments(["--preset", "standard"]),
                            environment={},
                            platform="win32",
                            run=run,
                        )
                self.assertEqual(receipt.read_text(), previous)
                self.assertEqual(run.call_count, index + 1)

    def test_invalid_options_or_missing_tools_fail_before_mutation(self):
        run = Mock()
        with self.assertRaises(ValueError):
            installer.plan(self.root, installer.arguments(["--extra", "missing"]), {}, "win32")
        with patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Required executable"):
                installer.install(
                    self.root, installer.arguments([]), environment={}, platform="win32", run=run
                )
        run.assert_not_called()
        self.assertFalse((self.root / ".venv").exists())
        (self.root / ".venv").mkdir()
        (self.root / ".venv/pa-install.json").write_text(
            '{"schema_version":1,"preset":"unknown","extras":[]}'
        )
        with self.assertRaisesRegex(ValueError, "Invalid saved"):
            installer.plan(self.root, installer.arguments([]), {}, "win32")

    def test_explicit_web_extra_builds_frontend_even_with_slim(self):
        _, _, commands = installer.plan(
            self.root, installer.arguments(["--preset", "slim", "--extra", "web"]), {}, "linux"
        )
        self.assertEqual([c[0] for c in commands[1:]], ["npm", "npm", "uv"])
