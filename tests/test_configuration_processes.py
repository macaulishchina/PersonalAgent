"""Owned descendant cleanup, manager resilience and installation entry points."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.app.management_bootstrap import ensure_management
from assistant_runtime.core.configuration.settings import ManagementSettings


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        sys.platform == "linux", "Linux guardian-specific contract; Windows uses Job Object tests"
    )
    async def test_guardian_pipe_loss_cleans_separate_session_and_marks_unclean(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pidfile = root / "child.pid"
            script = root / "worker.py"
            script.write_text(
                'import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"],start_new_session=True)\nPath(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(60)\n'
            )
            guardian = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "assistant_runtime.platforms.linux_guardian",
                "0.15",
                sys.executable,
                str(script),
                str(pidfile),
                stdin=-1,
                env={**os.environ, "PERSONAL_AGENT_INSTANCE_ROOT": str(root)},
            )
            try:
                async with asyncio.timeout(5):
                    while not pidfile.exists():
                        await asyncio.sleep(0.02)
                child = int(pidfile.read_text())
                guardian.stdin.close()  # Same EOF produced when the owning manager is SIGKILLed.
                await asyncio.wait_for(guardian.wait(), 5)
                self.assertFalse(Path(f"/proc/{child}").exists())
                self.assertEqual(
                    json.loads((root / "state/configuration/unclean.json").read_text())["reason"],
                    "manager_lost",
                )
            finally:
                if guardian.returncode is None:
                    guardian.terminate()
                    await guardian.wait()

    async def test_default_run_starts_admin_even_when_business_config_is_broken(self):
        from aiohttp import ClientSession

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text("broken=[\n")
            bootstrap = ensure_management(root, entry)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            bootstrap.write_text(bootstrap.read_text().replace("port = 8047", f"port = {port}"))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "assistant_runtime.app.cli",
                "--config",
                str(entry),
                "run",
                stdout=-1,
                stderr=-1,
            )
            try:
                async with ClientSession() as client, asyncio.timeout(10):
                    while True:
                        try:
                            async with client.get(f"http://127.0.0.1:{port}/api/status") as reply:
                                self.assertEqual(reply.status, 401)
                                break
                        except OSError:
                            self.assertIsNone(process.returncode)
                            await asyncio.sleep(0.05)
                    token = (root / "state/configuration/admin-token").read_text().strip()
                    async with client.post(
                        f"http://127.0.0.1:{port}/api/login",
                        json={"token": token},
                        headers={"Origin": f"http://127.0.0.1:{port}"},
                    ) as reply:
                        self.assertEqual(reply.status, 200)
                self.assertIsNone(process.returncode)
            finally:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 10)

    async def test_interrupted_apply_receipt_survives_and_external_steps_require_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text(f'root="{root.as_posix()}"\nplugins=[]\n')
            state = root / "state/configuration"
            state.mkdir(parents=True)
            (state / "application.json").write_text(
                json.dumps(
                    {
                        "phase": "restarting",
                        "version": "version",
                        "required_steps": [{"component": "model_gateway", "message": "operator deployment"}],
                    }
                )
            )
            business = BusinessProcess(ManagementSettings(root, entry))
            self.assertEqual(business.application["phase"], "failed")
            self.assertTrue(business.application["interrupted"])
            business.loaded = "version"
            with self.assertRaises(ValueError):
                business.complete_step("version", "model_gateway", True)
            business.application["phase"] = "partially_applied"
            with self.assertRaises(ValueError):
                business.complete_step("version", "model_gateway", False)
            business.complete_step("version", "model_gateway", True)
            restarted = BusinessProcess(ManagementSettings(root, entry))
            self.assertEqual(restarted.application["phase"], "applied")
            self.assertFalse(restarted.application["required_steps"])


class ManagedMcpTests(unittest.TestCase):
    def test_saved_registry_is_not_used_by_new_execution_before_restart(self):
        from types import SimpleNamespace
        from assistant_runtime.core.integrations.registry import McpRegistry
        from assistant_runtime.core.pi.integrations import PiIntegrations

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = McpRegistry(root, {})
            registry.configure("before", {"transport": {"type": "stdio", "command": "echo"}}, 0)
            frozen = root / "frozen.json"
            frozen.write_text(json.dumps(registry.snapshot()))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({str(registry.path): str(frozen)}))
            backend = SimpleNamespace(
                config=SimpleNamespace(socket=root / "control.sock", enabled_plugins=[])
            )
            with patch.dict(os.environ, {"PERSONAL_AGENT_RESOURCE_MANIFEST": str(manifest)}):
                adapter = PiIntegrations(backend, registry)
                registry.configure("after", {"transport": {"type": "stdio", "command": "echo"}}, 1)
                ordinary = adapter.configuration("background")
                self.assertIn("external_before", ordinary)
                self.assertNotIn("external_after", ordinary)
                probe = adapter.configuration("foreground", probe="after")
                self.assertIn("external_after", probe)
                self.assertEqual(adapter.status()["background_policy"], "restart_required")
