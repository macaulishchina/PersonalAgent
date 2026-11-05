"""Capacity configuration, shared admission and unlimited-task result delivery."""

import asyncio
import unittest
from types import SimpleNamespace

from assistant_runtime.api import ExecutionRequest
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.core.configuration.schema import resolve
from assistant_runtime.core.execution.declaration import settings
from assistant_runtime.core.tasks.settings import TaskSettings
import test_execution_service as execution_fixtures
import test_tasks as task_fixtures
import test_pi_skeleton as pi_fixtures


class SettingsTest(unittest.TestCase):
    def test_bounds_and_default(self):
        import inspect
        from assistant_runtime.core.execution.service import ExecutionService

        self.assertEqual(settings({})["concurrency"], 16)
        self.assertEqual(inspect.signature(ExecutionService).parameters["concurrency"].default, 16)
        self.assertFalse(hasattr(TaskSettings.parse({}), "concurrency"))
        spec = configuration_spec(TaskSettings.parse.__code__.co_filename)
        self.assertNotIn("concurrency", resolve(spec, {}))
        for value in (-1, 0, 1, 64, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TaskSettings.parse({"concurrency": value})
            with self.subTest(schema=value), self.assertRaises(ValueError):
                resolve(spec, {"concurrency": value})
        for value in (1, 4, 8, 16, 64):
            self.assertEqual(settings({"concurrency": value})["concurrency"], value)
        for value in (-1, 0, 65, True):
            with self.assertRaises(ValueError):
                settings({"concurrency": value})


class SessionRegistrationTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pi_fixtures.PiSkeletonTest.asyncSetUp
    asyncTearDown = pi_fixtures.PiSkeletonTest.asyncTearDown
    session = pi_fixtures.PiSkeletonTest.session

    async def test_queued_and_idle_metadata_does_not_consume_execution_capacity(self):
        sessions = [await self.session() for _ in range(40)]
        self.assertEqual(len(set(sessions)), 40)
        self.assertEqual(self.gateway.requests, [])
        self.assertTrue(all(worker.process is None for worker in self.client.workers.values()))
        for session in sessions:
            await self.client.request("session.close", {"session_id": session})
        self.assertEqual(self.client.workers, {})


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = execution_fixtures.ExecutionServiceTest.asyncSetUp
    asyncTearDown = execution_fixtures.ExecutionServiceTest.asyncTearDown

    async def test_capacity_is_content_free_and_tracks_waiting_quarantine_and_resize(self):
        admission = self.service.admission
        calls = []

        async def dispatch(session):
            calls.append(session)
            return {"run": {"id": session + "-turn"}}

        await admission.start({"session_id": "one"}, lambda: dispatch("one"))
        waiting = asyncio.create_task(admission.start({"session_id": "two"}, lambda: dispatch("two")))
        await asyncio.sleep(0.02)
        result = await self.service.request(ExecutionRequest("capacity", {}))
        self.assertEqual((result["occupied"], result["queued"], result["limit"]), (1, 1, 1))
        self.assertNotIn("one", result.values())
        self.service.records.update(self.service.sessions["one"], "unknown")
        self.assertEqual((await self.service.request(ExecutionRequest("capacity", {})))["quarantined"], 1)
        admission.slots.resize(16)
        await asyncio.wait_for(waiting, 2)
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual((await self.service.request(ExecutionRequest("capacity", {})))["limit"], 16)
        admission.slots.resize(1)
        self.assertEqual(admission.slots.active, 2)  # Lowering never revokes work.
        for ident in tuple(admission.leases):
            admission.release(ident)
        with self.assertRaises(ValueError):
            await self.service.request(ExecutionRequest("capacity", {"owner": "other"}))


class TasksTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_fixtures.TasksTest.asyncSetUp
    asyncTearDown = task_fixtures.TasksTest.asyncTearDown
    create = task_fixtures.TasksTest.create

    async def test_tasks_have_no_private_cap_and_deliver_final_results(self):
        self.host.backend.executions = SimpleNamespace(concurrency=16)
        self.assertEqual(self.plugin.result_delivery_limit(), 16)
        definitions = [
            await self.create(trigger={"kind": "date", "at": "2030-01-01T00:00:00+08:00"}) for _ in range(10)
        ]
        runs = [self.plugin.repo.enqueue(d, "capacity-test") for d in definitions]
        busy = [self.plugin.repo.update_run(r["id"], "running") for r in runs[:-1]]
        self.assertIsNone(self.plugin.lifecycle.wait(runs[-1], busy, [*busy, runs[-1]]))
        for run in runs:
            self.plugin.repo.update_run(run["id"], "completed", result="capacity-result")
        async with asyncio.timeout(5):
            while not all(self.plugin.repo.run(r["id"])["data"].get("completion_delivered") for r in runs):
                await asyncio.sleep(0.05)
        self.assertNotIn("limit", await self.plugin.request(TaskRequest("capacity", {})))
        with self.assertRaises(ValueError):
            await self.plugin.request(TaskRequest("capacity", {}, "forged-background"))
