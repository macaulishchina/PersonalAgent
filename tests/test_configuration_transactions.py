"""Bounded update recovery and background intent ownership; no supplier calls."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.api.configuration import ConfigurationUpdate, ConfigurationError
from assistant_runtime.app.configuration_updates import ConfigurationRuntime
from assistant_runtime.core.configuration.updates import UpdateCoordinator


class TransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_commit_timeout_rolls_back_and_a_later_update_can_succeed(self):
        stages, value = [], ["old"]
        coordinator = UpdateCoordinator(stage_timeout=0.03, on_stage=stages.append)

        async def prepare():
            async def commit():
                value[0] = "candidate"
                await asyncio.Event().wait()

            async def rollback():
                value[0] = "old"

            return ConfigurationUpdate(commit, rollback)

        with self.assertRaises(TimeoutError):
            await coordinator.apply([("model", prepare)])
        self.assertEqual(value, ["old"])
        self.assertFalse(coordinator.lock.locked())
        self.assertFalse(coordinator.failed_owners)
        self.assertEqual([s["stage"] for s in stages], ["prepare", "commit", "rollback"])

        async def next_prepare():
            async def commit():
                value[0] = "valid"

            return ConfigurationUpdate(commit, AsyncMock())

        await coordinator.apply([("model", next_prepare)])
        self.assertEqual(value, ["valid"])

    async def test_cleanup_timeout_is_explicit_and_releases_transaction_lock(self):
        coordinator = UpdateCoordinator(stage_timeout=0.03)

        async def prepare():
            async def release(committed):
                self.assertTrue(committed)
                await asyncio.Event().wait()

            return ConfigurationUpdate(AsyncMock(), AsyncMock(), release)

        await coordinator.apply([("models", prepare)])
        self.assertEqual(coordinator.cleanup_failures, {"models"})
        self.assertFalse(coordinator.lock.locked())
        self.assertFalse(coordinator.authorized_tasks)

    async def test_prepare_timeout_never_commits_and_rollback_failure_blocks_owner(self):
        coordinator = UpdateCoordinator(stage_timeout=0.03)

        async def hung():
            await asyncio.Event().wait()

        with self.assertRaises(TimeoutError):
            await coordinator.apply([("pi", hung)])

        async def prepare():
            return ConfigurationUpdate(AsyncMock(side_effect=OSError()), AsyncMock(side_effect=OSError()))

        with self.assertRaises(OSError):
            await coordinator.apply([("pi", prepare)])
        self.assertEqual(coordinator.failed_owners, {"pi"})

    async def test_same_attempt_is_polled_once_and_retry_requires_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state/configuration").mkdir(parents=True)
            runtime = ConfigurationRuntime(SimpleNamespace(config=SimpleNamespace(state_dir=root / "state")))
            release = asyncio.Event()

            async def apply(*args):
                await release.wait()
                return {"phase": "applied"}

            runtime.apply = AsyncMock(side_effect=apply)
            first = await runtime.submit("frozen/config.toml", "revision", attempt_id="one")
            self.assertTrue(first["busy"])
            await asyncio.sleep(0)
            for _ in range(5):
                await runtime.submit("frozen/config.toml", "revision", attempt_id="one")
            runtime.apply.assert_awaited_once()
            with self.assertRaises(ConfigurationError):
                await runtime.submit("frozen/config.toml", "different", attempt_id="two")
            release.set()
            await runtime.task
            result = await runtime.submit("frozen/config.toml", "revision", attempt_id="one")
            self.assertEqual(result["phase"], "applied")
            self.assertFalse(result["busy"])
            runtime.apply.assert_awaited_once()
            await runtime.submit("frozen/config.toml", "revision", attempt_id="two")
            await runtime.task
            self.assertEqual(runtime.apply.await_count, 2)
            await runtime.close()

    async def test_failed_preparation_exposes_safe_issue_without_leaking_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "state/configuration").mkdir(parents=True)
            runtime = ConfigurationRuntime(SimpleNamespace(config=SimpleNamespace(state_dir=root / "state")))
            runtime.apply = AsyncMock(
                side_effect=ConfigurationError(
                    ["models"], "update_prepare", "Model preparation timed out; previous settings retained"
                )
            )
            await runtime.submit("frozen/config.toml", "revision", attempt_id="one")
            await runtime.task
            result = runtime.status()
            self.assertEqual(result["phase"], "failed")
            self.assertEqual(result["issues"][0]["code"], "update_prepare")
            self.assertFalse(result["busy"])
            await runtime.close()
