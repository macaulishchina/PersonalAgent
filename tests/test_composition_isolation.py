"""Configuration and shared storage must not activate unrelated plugin owners."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class CompositionIsolationTest(unittest.TestCase):
    def test_both_presets_ship_core_and_enable_integrations(self):
        import tempfile
        from importlib.resources import files
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.core.plugin_catalog import OWNERS

        for owner in OWNERS.values():
            if owner.startswith("assistant_runtime.core."):
                relative = owner.removeprefix("assistant_runtime.").replace(".", "/")
                self.assertTrue((files("assistant_runtime") / relative / "declaration.py").is_file(), owner)
        with tempfile.TemporaryDirectory() as directory:
            for preset in ("slim", "standard"):
                config = Config.load(initialize(Path(directory) / preset, preset=preset))
                self.assertIn("integrations", config.plugins)
                self.assertTrue({"memory", "events"}.isdisjoint(config.plugins))
                self.assertFalse(config.external_mcp.get("inherit_user_servers", False))

    def test_slim_initializer_and_host_do_not_import_optional_owners(self):
        source = """
import asyncio, sys, tempfile, uuid
from pathlib import Path
from assistant_runtime.app.config import Config
from assistant_runtime.app.initialize import initialize
from assistant_runtime.app.host import AssistantService
from assistant_runtime.app.cli import request
async def main():
    with tempfile.TemporaryDirectory() as directory:
        config = Config.load(initialize(Path(directory) / "instance", preset="slim"))
        host = AssistantService(config)
        work = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 10)
            status = await request(config, {"command": "status"})
            assert status["configuration_required"]
            assert set(status["plugins"]) == set(config.plugins)
            assert all(row["state"] == "ready" for row in status["plugins"].values())
            assert host.assistant is None
            assert host.backend.execution_session_id is None
            try:
                await request(config, {"command": "executions", "action": "submit",
                                      "arguments": {"request_id": str(uuid.uuid4()), "text": "never sent"}})
            except RuntimeError as error:
                assert "configuration required" in str(error), error
            else:
                raise AssertionError("Unconfigured inference accepted")
            forbidden = ("assistant_runtime.core.conversation", "assistant_runtime.core.memory",
                         "assistant_runtime.core.speech", "assistant_runtime.core.devices",
                         "assistant_runtime.core.tasks", "assistant_runtime.core.notifications",
                         "assistant_runtime.plugins.", "apscheduler", "numpy", "miniaudio")
            loaded = [name for name in sys.modules if name.startswith(forbidden)]
            assert not loaded, loaded
        finally:
            host.stop_event.set()
            await asyncio.wait_for(work, 10)
asyncio.run(main())
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_execution_configuration_and_storage_do_not_load_assistant_owners(self):
        source = """
import asyncio, sys, tempfile
from pathlib import Path
from assistant_runtime.app.config import Config
from assistant_runtime.app.store import Store
from assistant_runtime.app.bootstrap import assistant_skill_paths
with tempfile.TemporaryDirectory() as directory:
    config = Config(root=Path(directory), plugins=["ai_usage", "models", "pi"])
    store = Store(config.state_dir / "test.sqlite3")
    store.set("example", {"value": 7})
    assert store.get("example") == {"value": 7}
    assert assistant_skill_paths(config) == ()
    forbidden = ("assistant_runtime.core.conversation", "assistant_runtime.core.memory",
                 "assistant_runtime.core.speech", "assistant_runtime.core.devices",
                 "assistant_runtime.plugins.")
    assert not [name for name in sys.modules if name.startswith(forbidden)]
    tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"settings"}, tables
    from assistant_runtime.core.pi.plugin import PiPlugin
    plugin = PiPlugin(config, store, foreground=False)
    backend = plugin.backend
    async def lifecycle():
        await backend.start()
        assert backend.session_id is None
        assert backend.execution_session_id is None
        await backend.close()
    asyncio.run(lifecycle())
    assert not [name for name in sys.modules if name.startswith(forbidden)]
    assert "executions" not in store.__dict__
    store.executions.create_task("one", "synthetic")
    assert store.executions.recent()[0]["id"] == "one"
    assert not [name for name in sys.modules if name.startswith(forbidden)]
    store.db.close()
    reopened = Store(config.state_dir / "test.sqlite3")
    assert reopened.executions.recent()[0]["id"] == "one"
    assert reopened.get("example") == {"value": 7}
    reopened.db.close()
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
