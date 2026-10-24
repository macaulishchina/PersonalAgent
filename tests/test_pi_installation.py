"""Per-user Pi discovery keeps default deployments independent of a shell PATH."""

import json
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.api.system import system
from assistant_runtime.app.install_pi import setup
from assistant_runtime.platforms.executables import pi_command, pi_node
from assistant_runtime.platforms.pi_installation import data_directory, default_directory, installed, remember


class PiInstallationTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="pi install ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(
            patch.dict(
                os.environ, LOCALAPPDATA=str(self.root / "local"), XDG_DATA_HOME=str(self.root / "data")
            )
        )

    def runtime(self, folder):
        binary = folder / "node_modules/.bin" / ("pi.cmd" if system.name == "windows" else "pi")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("synthetic executable")
        binary.chmod(0o700)
        node = self.root / "node.exe"
        node.touch()
        return binary, node

    def test_platform_default_and_xdg_fallback(self):
        with patch.object(system, "name", "windows"):
            self.assertEqual(default_directory(), self.root / "local/personal-agent/pi")
            with patch.dict(os.environ, LOCALAPPDATA=""), patch.object(Path, "home", return_value=self.root):
                self.assertEqual(default_directory(), self.root / "AppData/Local/personal-agent/pi")
        with patch.object(system, "name", "linux"):
            self.assertEqual(default_directory(), self.root / "data/personal-agent/pi")
            for value in ("", "relative"):
                with (
                    patch.dict(os.environ, XDG_DATA_HOME=value),
                    patch.object(Path, "home", return_value=self.root),
                ):
                    self.assertEqual(default_directory(), self.root / ".local/share/personal-agent/pi")

    def test_record_and_default_installation_are_found_without_path(self):
        self.assertIsNone(installed())
        binary, node = self.runtime(default_directory())
        self.assertEqual(installed(), (binary, None))
        remember(binary, node)
        self.assertEqual(installed(), (binary.resolve(), node.resolve()))

    def test_corrupt_or_stale_record_does_not_fall_back(self):
        binary, node = self.runtime(default_directory())
        remember(binary, node)
        node.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            installed()
        record = data_directory() / "pi-installation.json"
        for value in ("not-json", "[]", "{}", '{"binary":"relative","node":"relative"}'):
            record.write_text(value)
            with self.assertRaisesRegex(RuntimeError, "record"):
                pi_command("pi")

    def test_registered_installation_precedes_path_and_explicit_path_overrides_it(self):
        binary, node = self.runtime(self.root / "custom pi")
        remember(binary, node)
        alternate = self.root / "explicit-pi"
        alternate.touch()
        with patch("assistant_runtime.platforms.executables.shutil.which", side_effect=lambda value: value):
            if system.name == "windows":
                package = binary.parent.parent / "@earendil-works/pi-coding-agent"
                package.mkdir(parents=True)
                (package / "package.json").write_text(json.dumps({"bin": {"pi": "cli.js"}}))
                (package / "cli.js").touch()
                self.assertEqual(pi_command("pi"), [str(node.resolve()), str(package / "cli.js")])
            else:
                self.assertEqual(pi_command("pi"), [str(binary.resolve())])
            self.assertEqual(pi_command(str(alternate)), [str(alternate.resolve())])

    def test_unregistered_path_installation_remains_available(self):
        binary = self.root / "external-pi"
        binary.touch()
        with patch("assistant_runtime.platforms.executables.shutil.which", return_value=str(binary)):
            self.assertEqual(pi_command("pi"), [str(binary.resolve())])

    def test_native_accounts_discover_the_same_registered_runtime_without_path(self):
        from assistant_runtime.core.pi.accounts import installation

        binary, node = self.runtime(self.root / "custom sdk")
        package = binary.parent.parent / "@earendil-works/pi-coding-agent"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {"name": "@earendil-works/pi-coding-agent", "version": "1.4.0", "bin": {"pi": "cli.js"}}
            )
        )
        (package / "cli.js").touch()
        if system.name != "windows":
            # npm's POSIX launcher is a symlink to the package entry.
            binary.unlink()
            binary.symlink_to(package / "cli.js")
            (package / "cli.js").chmod(0o700)
        remember(binary, node)
        with patch.dict(os.environ, PATH=""):
            self.assertEqual(asyncio.run(installation("pi")), package)
            self.assertEqual(pi_node("pi"), str(node.resolve()))

    def run_setup(self, destination=None, failure=None, produce_binary=True):
        node, npm = self.root / "node.exe", self.root / "npm"
        node.touch()
        npm.touch()
        npm_cli = self.root / "node_modules/npm/bin/npm-cli.js"
        npm_cli.parent.mkdir(parents=True, exist_ok=True)
        npm_cli.touch()

        def install(command, **kwargs):
            if failure:
                raise failure
            if produce_binary:
                self.runtime(Path(command[command.index("--prefix") + 1]))

        with (
            patch(
                "assistant_runtime.app.install_pi.shutil.which",
                side_effect=lambda value: str(node if value == "node" else npm),
            ),
            patch("assistant_runtime.app.install_pi.subprocess.run", side_effect=install) as run,
        ):
            result = setup(destination)
        self.assertIn("ci", run.call_args.args[0])
        self.assertEqual(installed(), (result.resolve(), node.resolve()))
        self.assertTrue((result.parents[2] / "package-lock.json").is_file())
        return result

    def test_default_and_custom_setup_record_only_successful_installation(self):
        binary = self.run_setup()
        self.assertTrue(binary.is_relative_to(default_directory()))
        custom = self.run_setup(self.root / "custom 中文 pi")
        self.assertTrue(custom.is_relative_to(self.root / "custom 中文 pi"))
        record = (data_directory() / "pi-installation.json").read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_setup(self.root / "failed", subprocess.CalledProcessError(1, "npm"))
        self.assertEqual((data_directory() / "pi-installation.json").read_bytes(), record)

    def test_missing_executable_after_npm_success_is_not_registered(self):
        with self.assertRaisesRegex(RuntimeError, "did not produce"):
            self.run_setup(produce_binary=False)
        self.assertFalse((data_directory() / "pi-installation.json").exists())
