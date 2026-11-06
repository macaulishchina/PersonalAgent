"""Save/apply interaction against a real local worker; no paid model calls."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.files import Conflict
from assistant_runtime.core.configuration.save_flow import SaveFlow
from assistant_runtime.core.configuration.settings import ManagementSettings


def patch(path, value):
    return {"path": path, "op": "set", "value": value}


class SaveFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        entry = self.root / "config.toml"
        entry.write_text("root=" + json.dumps(str(self.root)) + '\nplugins=["ai_usage"]\n', encoding="utf-8")
        self.business = BusinessProcess(
            ManagementSettings(self.root, entry, startup_seconds=15, shutdown_seconds=10)
        )
        await self.business.start()
        self.assertEqual(self.business.outcome, "running", self.business.error)
        self.flow = SaveFlow(self.business)

    async def asyncTearDown(self):
        if self.business.apply_task:
            await asyncio.gather(self.business.apply_task, return_exceptions=True)
        await self.business.stop()
        self.temp.cleanup()

    async def settle(self):
        if self.business.apply_task:
            await asyncio.wait_for(self.business.apply_task, 15)
        self.assertIn(
            self.business.application["phase"], {"applied", "partially_applied"}, self.business.application
        )

    async def test_mixed_save_decline_reload_discard_and_confirm(self):
        pid = self.business.process.pid
        first = self.business.files.snapshot()
        edits = [patch(["ai_usage", "max_requests"], 37), patch(["plugins"], [])]
        saved = await self.flow.save(first.version, edits, [])
        await self.settle()
        self.assertEqual(self.business.process.pid, pid)
        config = self.business.files.fields()
        self.assertEqual(next(f["value"] for f in config["fields"] if f["path"] == ["plugins"]), ["ai_usage"])
        self.assertEqual(self.business.loaded_snapshot.version, saved["version"])
        self.assertEqual(saved["pending_restart"]["count"], 1)
        self.assertEqual(saved["pending_restart"]["patches"], [edits[1]])
        reloaded = SaveFlow(self.business)
        self.assertEqual(reloaded.public(), saved["pending_restart"])
        with self.assertRaises(Conflict):
            await reloaded.restart(first.version, reloaded.public()["id"])
        reloaded.discard(reloaded.public()["id"])
        self.assertIsNone(reloaded.public())
        self.assertEqual(self.business.files.snapshot().version, saved["version"])
        cold = await reloaded.save(saved["version"], [edits[1]], [])
        self.assertEqual(self.business.process.pid, pid)
        restarted = await reloaded.restart(cold["version"], cold["pending_restart"]["id"])
        await self.settle()
        self.assertNotEqual(self.business.process.pid, pid)
        self.assertEqual(self.business.loaded, restarted["version"])
        self.assertIsNone(reloaded.public())

    async def test_partial_dependency_failure_defers_all_without_writes(self):
        first = self.business.files.snapshot()
        original = self.flow.files.patch

        def validate(version, patches, **kwargs):
            if kwargs.get("_preview") and patches == [patch(["ai_usage", "max_requests"], 37)]:
                raise ValueError("dependent partial candidate")
            return original(version, patches, **kwargs)

        self.flow.files.patch = validate
        saved = await self.flow.save(
            first.version, [patch(["ai_usage", "max_requests"], 37), patch(["plugins"], [])], []
        )
        self.assertFalse(saved["changed"])
        self.assertEqual(saved["pending_restart"]["count"], 2)
        self.assertEqual(self.business.files.snapshot().version, first.version)

    async def test_no_change_explicit_save_retries_failed_hot_update(self):
        first = self.business.files.snapshot()
        saved = self.business.files.patch(first.version, [patch(["ai_usage", "max_requests"], 37)])
        self.business.application = {"phase": "failed", "version": saved["version"]}
        result = await self.flow.save(saved["version"], [], [])
        await self.settle()
        self.assertFalse(result["changed"])
        self.assertEqual(self.business.loaded, result["version"])

    async def test_pending_cannot_be_overwritten_by_another_editor_and_secrets_stay_private(self):
        first = self.business.files.snapshot()
        self.flow.persist(
            first.version,
            [patch(["plugins"], [])],
            [{"id": "credentials", "key": "PRIVATE_KEY", "op": "replace_secret", "value": "not-for-browser"}],
        )
        public = self.flow.public()
        self.assertNotIn("not-for-browser", json.dumps(public))
        self.assertEqual(public["opaque_count"], 1)
        with self.assertRaises(Conflict):
            await self.flow.save(first.version, [], [])
        with self.assertRaises(Conflict):
            self.flow.discard("stale-id")

    async def test_failed_hot_update_retains_explicit_retry_and_restart_draft(self):
        first = self.business.files.snapshot()
        real_request = self.business.request

        async def request(payload):
            if payload.get("command") == "configuration-update" and not payload.get("preview"):
                return {
                    "phase": "failed",
                    "issues": [{"path": ["ai_usage"], "code": "update_prepare", "message": "failed safely"}],
                }
            return await real_request(payload)

        self.business.request = AsyncMock(side_effect=request)
        saved = await self.flow.save(
            first.version, [patch(["ai_usage", "max_requests"], 37), patch(["plugins"], [])], []
        )
        await asyncio.wait_for(self.business.apply_task, 10)
        self.assertEqual(self.business.application["phase"], "failed")
        self.assertEqual(self.business.loaded, first.version)
        self.assertIsNotNone(self.flow.public())
        self.business.request = real_request
        retry = await self.flow.save(
            saved["version"], self.flow.public()["patches"], [], self.flow.public()["id"]
        )
        await self.settle()
        self.assertEqual(self.business.loaded, retry["version"])
        self.assertEqual(self.flow.public()["count"], 1)

    async def test_real_pi_and_model_helpers_switch_configuration_without_restart(self):
        import tomlkit

        await self.business.stop()
        config = {
            "root": str(self.root),
            "plugins": [
                "ai_usage",
                "workfiles",
                "models",
                "pi",
                "execution",
                "integrations",
                "conversation",
                "cli",
            ],
            "main_model": "one",
            "model_api": {
                "enabled": True,
                "providers": {
                    "local": {
                        "protocol": "openai_responses",
                        "base_url": "http://127.0.0.1:9/v1",
                        "key_env": "TEST_KEY",
                        "enabled": True,
                    }
                },
                "entries": {
                    alias: {
                        "provider": "local",
                        "model": alias,
                        "reasoning_efforts": [],
                        "default_reasoning_effort": "auto",
                    }
                    for alias in ("one", "two")
                },
            },
        }
        (self.root / ".env").write_text("TEST_KEY=local-synthetic-key\n")
        self.business.settings.entry.write_text(tomlkit.dumps(config), encoding="utf-8")
        await self.business.start(self.business.files.snapshot())
        self.assertEqual(self.business.outcome, "running", self.business.error)
        pid = self.business.process.pid
        first = self.business.files.snapshot()
        saved = await self.flow.save(
            first.version, [patch(["main_model"], "two"), patch(["execution", "concurrency"], 2)], []
        )
        await self.settle()
        self.assertEqual(self.business.process.pid, pid)
        self.assertEqual(self.business.loaded, saved["version"])
        self.assertIsNone(self.flow.public())
        status = await self.business.request({"command": "management-status"})
        self.assertEqual(status["configuration_version"], saved["version"])
