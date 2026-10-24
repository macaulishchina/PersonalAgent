"""Short command defaults and explicit instance ownership without real user settings."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from assistant_runtime.app import cli
from assistant_runtime.app.initialize import initialize
from assistant_runtime.app.instance_selection import entry_path, resolve, select
from assistant_runtime.platforms.user_paths import configuration_directory


class CliEntryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        self.env = patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root / "user"),
                "XDG_CONFIG_HOME": str(self.root / "user"),
                "PERSONAL_AGENT_CONFIG": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.a = initialize(self.root / "a", preset="slim")
        self.b = initialize(self.root / "b", preset="slim")

    def invoke(self, *args):
        with patch("sys.argv", ["pa", *map(str, args)]), patch("sys.stdout", new_callable=io.StringIO) as out:
            cli.main()
            return out.getvalue()

    def test_use_persists_only_selection_and_default_command_chats(self):
        original = self.a.read_bytes()
        output = self.invoke("use", self.a.parent.parent)
        self.assertIn("Run pa", output)
        self.assertEqual(self.a.read_bytes(), original)
        self.assertEqual(resolve(), self.a.resolve())
        with patch("assistant_runtime.app.chat.chat", new_callable=AsyncMock) as chat:
            self.invoke()
            chat.assert_awaited_once()
            self.assertEqual(chat.await_args.args[1:], (str(self.a.resolve()), None))
        self.assertFalse((self.a.parent.parent / "state/configuration/detached-manager.json").exists())

    def test_explicit_selection_is_temporary_and_environment_wins_over_local(self):
        select(self.a)
        self.assertEqual(resolve(self.b), self.b.resolve())
        self.assertEqual(resolve(), self.a.resolve())
        os.chdir(self.b.parent.parent)
        self.assertEqual(resolve(), self.a.resolve())
        with patch.dict(os.environ, {"PERSONAL_AGENT_CONFIG": str(self.a)}):
            self.assertEqual(resolve(), self.a.resolve())
            self.assertEqual(resolve(self.b), self.b.resolve())
        (configuration_directory() / "cli.json").unlink()
        self.assertEqual(resolve(), self.b.resolve())

    def test_deleted_saved_instance_does_not_fall_back_or_initialize(self):
        select(self.a)
        self.a.unlink()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            resolve()
        self.assertFalse(self.a.exists())

    def test_missing_and_corrupt_selection_provide_actionable_errors(self):
        with self.assertRaisesRegex(ValueError, "pa use"):
            resolve()
        self.assertIsNone(resolve(required=False))
        select(self.a)
        record = configuration_directory() / "cli.json"
        record.write_text(json.dumps({"config": "relative.toml"}))
        with self.assertRaisesRegex(ValueError, "pa use"):
            resolve()
        self.assertEqual(resolve(self.b), self.b.resolve())

    def test_ambiguous_directory_requires_an_explicit_entry(self):
        folder = self.root / "ambiguous"
        (folder / "config").mkdir(parents=True)
        (folder / "config.toml").write_text("")
        (folder / "config/config.toml").write_text("")
        with self.assertRaisesRegex(ValueError, "one entry"):
            entry_path(folder)
        self.assertEqual(entry_path(folder / "config.toml"), folder / "config.toml")

    def test_status_has_no_chat_arguments_and_stop_manages_background(self):
        select(self.a)
        with patch.object(cli, "request", new_callable=AsyncMock, return_value={}) as request:
            self.invoke("status")
            self.assertEqual(request.await_args.args[1], {"command": "status"})
            with patch("assistant_runtime.platforms.services.control") as control:
                self.invoke("stop")
                self.assertEqual(control.call_args.args[0], "stop")
                request.assert_awaited_once()

    def test_session_selection_and_explicit_config_without_chat_subcommand(self):
        with patch("assistant_runtime.app.chat.chat", new_callable=AsyncMock) as chat:
            self.invoke("--config", self.a)
            self.assertEqual(chat.await_args.args[1:], (str(self.a.resolve()), None))
            self.invoke("--config", self.b, "chat", "--session", "chosen")
            self.assertEqual(chat.await_args.args[1:], (str(self.b.resolve()), "chosen"))

    def test_doctor_can_serialize_the_remembered_config_path(self):
        select(self.a)
        data = json.loads(self.invoke("doctor"))
        self.assertEqual(data["config_source"], str(self.a.resolve()))

    def test_help_is_available_without_an_instance(self):
        with patch("sys.argv", ["pa", "--help"]), patch("sys.stdout", new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit) as exit:
                cli.main()
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("usage: pa", out.getvalue())

    def test_existing_legacy_config_remains_selectable(self):
        old = Path(os.environ["XDG_CONFIG_HOME"]) / "personal-agent/config.toml"
        old.parent.mkdir(parents=True)
        old.write_text("plugins=[]\n")
        self.assertEqual(resolve(), old.resolve())
