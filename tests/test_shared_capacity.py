"""One host admission for executions and standalone inference."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace

from assistant_runtime.api.capacity import Capacity, current_capacity_lease
from assistant_runtime.core.execution.resources import capacity_for
import test_models as fixtures


class SharedCapacityTest(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_cancellation_resize_and_unbounded_waiters(self):
        pool = Capacity(1)
        await pool.acquire()
        order = []
        gate = asyncio.Event()

        async def work(index):
            async with pool.inference():
                order.append(index)
                await gate.wait()

        jobs = [asyncio.create_task(work(i)) for i in range(130)]
        await asyncio.sleep(0)
        self.assertEqual(pool.inference_waiting, 130)
        for job in jobs[::2]:
            job.cancel()
        await asyncio.sleep(0)
        pool.resize(16)
        await asyncio.sleep(0)
        self.assertEqual(pool.active, 16)
        self.assertEqual(order, list(range(1, 30, 2)))
        pool.resize(1)
        pool.release()
        self.assertEqual(pool.active, 15)
        gate.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.assertEqual(order, list(range(1, 130, 2)))
        self.assertEqual((pool.active, pool.inference_waiting, len(pool.waiters)), (0, 0, 0))

    async def test_cancel_after_grant_returns_exactly_one_slot(self):
        pool = Capacity(1)
        await pool.acquire()
        pending = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        pool.release()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(pool.active, 0)

    async def test_nested_borrow_is_serial_and_close_waits_for_inflight_call(self):
        pool = Capacity(1)
        await pool.acquire()
        lease = pool.reserve("execution")
        started, finish = asyncio.Event(), asyncio.Event()

        async def nested():
            async with pool.inference():
                started.set()
                await finish.wait()

        token = current_capacity_lease.set(lease)
        try:
            first = asyncio.create_task(nested())
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(nested())
        finally:
            current_capacity_lease.reset(token)
        independent = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        pool.release_reservation("execution")
        self.assertEqual(pool.active, 1)
        self.assertFalse(independent.done())
        finish.set()
        await first
        with self.assertRaisesRegex(RuntimeError, "admission has ended"):
            await second
        await asyncio.wait_for(independent, 1)
        self.assertEqual(pool.active, 1)
        pool.release()
        self.assertEqual(pool.active, 0)

    async def test_host_owners_reuse_exact_same_pool(self):
        host = SimpleNamespace(config=SimpleNamespace(execution={"concurrency": 16}))
        self.assertIs(capacity_for(host), capacity_for(host))
        self.assertEqual(capacity_for(host).limit, 16)


class ModelAdmissionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ModelsTest.asyncSetUp
    asyncTearDown = fixtures.ModelsTest.asyncTearDown
    request = fixtures.ModelsTest.request

    async def test_queue_wait_counts_toward_model_deadline(self):
        self.plugin.settings = replace(self.plugin.settings, timeout_seconds=1)
        pool = self.plugin.capacity
        await pool.acquire()
        request = self.request()
        pending = asyncio.create_task(self.runtime.invoke(request))
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 1.5)
            self.assertTrue(pending.done(), "The model deadline must expire while queued")
            self.client.complete.assert_not_awaited()
            self.assertEqual(pool.inference_waiting, 0)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            pool.release()

    async def test_models_only_capacity_apply_does_not_replace_client(self):
        import os
        import tomlkit
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.configuration_updates import ConfigurationRuntime

        self.host.config = Config(
            root=self.host.config.root,
            plugins=["ai_usage", "models"],
            model_api=self.host.config.model_api,
            main_model="test-model",
            execution={"concurrency": 1},
        )
        revision = "capacity-only"
        folder = self.host.config.state_dir / "configuration/generations" / revision
        folder.mkdir(parents=True)
        entry = folder / "config.toml"
        entry.write_text(
            tomlkit.dumps(
                {
                    "root": str(self.host.config.root),
                    "plugins": ["ai_usage", "models"],
                    "model_api": self.host.config.model_api,
                    "main_model": "test-model",
                    "execution": {"concurrency": 2},
                }
            ),
            encoding="utf-8",
        )
        client = self.plugin.client
        updater = ConfigurationRuntime(self.host)
        with patch.dict(os.environ):
            preview = await updater._apply(entry, revision, preview=True)
            self.assertEqual(preview["phase"], "ready")
            self.assertEqual(self.plugin.capacity.limit, 1)
            result = await updater._apply(entry, revision)
        self.assertEqual(result["phase"], "applied")
        self.assertEqual(self.host.config.execution["concurrency"], 2)
        self.assertEqual(self.plugin.capacity.limit, 2)
        self.assertIs(self.plugin.client, client)
        client.close.assert_not_awaited()
        original = self.host.config
        changed = tomlkit.parse(entry.read_text(encoding="utf-8"))
        changed["execution"]["concurrency"] = 1
        revision = "capacity-lower"
        folder = original.state_dir / "configuration/generations" / revision
        folder.mkdir(parents=True)
        entry = folder / "config.toml"
        entry.write_text(tomlkit.dumps(changed), encoding="utf-8")
        pool = self.plugin.capacity
        await pool.acquire()
        await pool.acquire()
        try:
            with patch.dict(os.environ):
                with patch.object(updater.updates, "apply", side_effect=RuntimeError("synthetic failure")):
                    with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                        await updater._apply(entry, revision)
                self.assertIs(self.host.config, original)
                self.assertEqual(pool.limit, 2)
                result = await updater._apply(entry, revision)
            self.assertEqual(result["phase"], "applied")
            self.assertEqual((pool.limit, pool.active), (1, 2))
            self.assertTrue(self.runtime.configuration_ready.is_set())
        finally:
            pool.release()
            pool.release()

    async def test_execution_and_auxiliary_request_share_limit_and_nested_lease(self):
        pool = self.plugin.capacity
        await pool.acquire()
        lease = pool.reserve("synthetic-execution")
        outside = asyncio.create_task(self.runtime.invoke(self.request()))
        await asyncio.sleep(0.02)
        self.client.complete.assert_not_awaited()
        token = current_capacity_lease.set(lease)
        try:
            result = await asyncio.wait_for(self.runtime.invoke(self.request()), 2)
            self.assertEqual(result["state"], "completed")
        finally:
            current_capacity_lease.reset(token)
        self.assertEqual(pool.active, 1)
        self.assertFalse(outside.done())
        pool.release_reservation("synthetic-execution")
        self.assertEqual((await asyncio.wait_for(outside, 2))["state"], "completed")
        self.assertEqual(self.client.complete.await_count, 2)
        self.assertEqual(pool.active, 0)

    async def test_sixty_four_requests_reach_upstream_without_a_private_gate(self):
        self.plugin.capacity.resize(64)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 64:
                entered.set()
            await release.wait()
            return {"text": "synthetic", "usage": {"output_tokens": 1}}

        self.client.complete.side_effect = complete
        jobs = [asyncio.create_task(self.runtime.invoke(self.request())) for _ in range(72)]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(calls, 64)
            self.assertEqual(self.plugin.capacity.active, 64)
            release.set()
            self.assertTrue(all(r["state"] == "completed" for r in await asyncio.gather(*jobs)))
            self.assertEqual(calls, 72)
            self.assertEqual(self.plugin.capacity.active, 0)
        finally:
            release.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
