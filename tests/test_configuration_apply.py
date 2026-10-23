"""Lifecycle boundaries use local workers and synthetic blockers, never suppliers."""

import asyncio
from pathlib import Path
import tempfile
import unittest

from assistant_runtime.api.lifecycle import WorkIntake, admits_work
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.settings import ManagementSettings


class IntakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_and_inherited_children_survive_drain_but_new_roots_do_not(self):
        intake = WorkIntake()
        entered, release = asyncio.Event(), asyncio.Event()

        class Entrance:
            @admits_work()
            async def submit(self):
                entered.set()
                await release.wait()
                with intake.accept("associated child"):
                    return True

        entrance = Entrance()
        entrance.intake = intake
        task = asyncio.create_task(entrance.submit())
        await entered.wait()
        intake.paused = True
        self.assertEqual(len(intake.pending), 1)
        with self.assertRaises(RuntimeError):
            await entrance.submit()
        release.set()
        self.assertTrue(await task)
        self.assertFalse(intake.pending)

    async def test_finished_preparation_does_not_leave_admission_in_delayed_child_context(self):
        intake = WorkIntake()
        release = asyncio.Event()

        async def delayed():
            await release.wait()
            with intake.accept("late new work"):
                return True

        with intake.accept("preparation"):
            child = asyncio.create_task(delayed())
        intake.paused = True
        release.set()
        with self.assertRaises(RuntimeError):
            await child
        self.assertFalse(intake.pending)


class ApplyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entry = self.root / "config.toml"
        self.entry.write_text(f'root="{self.root}"\nplugins=[]\n')
        self.business = BusinessProcess(
            ManagementSettings(self.root, self.entry, startup_seconds=10, shutdown_seconds=10)
        )
        await self.business.start()
        self.assertEqual(self.business.outcome, "running", self.business.error)

    async def asyncTearDown(self):
        if self.business.apply_task and not self.business.apply_task.done():
            self.business.apply_task.cancel()
            await asyncio.gather(self.business.apply_task, return_exceptions=True)
        await self.business.stop()
        self.temp.cleanup()

    async def test_save_does_not_restart_apply_changes_pid_and_loads_exact_confirmed_version(self):
        pid = self.business.process.pid
        snapshot = self.business.files.fields()
        saved = self.business.files.patch(
            snapshot["version"], [{"path": ["assistant_name"], "op": "set", "value": "changed"}]
        )
        self.assertEqual(self.business.process.pid, pid)
        with self.assertRaises(ValueError):
            self.business.apply(saved["version"], "wait", False)
        self.business.apply(saved["version"], "wait", True)
        await asyncio.wait_for(self.business.apply_task, 15)
        self.assertEqual(self.business.application["phase"], "applied")
        self.assertNotEqual(self.business.process.pid, pid)
        self.assertEqual(self.business.loaded, saved["version"])

    async def test_wait_has_no_timeout_and_withdrawal_resumes_same_worker(self):
        original = self.business.request
        paused = asyncio.Event()

        async def request(payload):
            result = await original(payload)
            if payload["command"] == "management-pause":
                paused.set()
            if payload["command"] == "management-work":
                return {"ready": False, "blockers": [{"id": "question", "state": "waiting_user"}]}
            return result

        self.business.request = request
        pid = self.business.process.pid
        self.business.apply(self.business.files.fields()["version"], "wait", True)
        await asyncio.wait_for(paused.wait(), 5)
        for _ in range(100):
            if self.business.application.get("blockers"):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.business.application["blockers"][0]["state"], "waiting_user")
        await self.business.abort_apply()
        self.assertEqual(self.business.process.pid, pid)
        self.assertFalse((await original({"command": "management-work"}))["paused"])
        self.assertEqual(self.business.application["phase"], "withdrawn")

    async def test_force_cannot_be_withdrawn_after_cancellation_has_started(self):
        original = self.business.request
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(payload):
            if payload["command"] == "management-cancel":
                entered.set()
                await release.wait()
            return await original(payload)

        self.business.request = request
        self.business.apply(self.business.files.fields()["version"], "force", True)
        await asyncio.wait_for(entered.wait(), 5)
        self.assertEqual(self.business.application["phase"], "cancelling")
        with self.assertRaises(ValueError):
            await self.business.abort_apply()
        release.set()
        await asyncio.wait_for(self.business.apply_task, 15)
        self.assertEqual(self.business.application["phase"], "applied")

    async def test_worker_restart_does_not_claim_external_deployment_is_complete(self):
        from unittest.mock import patch

        step = {"component": "model_gateway", "state": "manual_action_required", "message": "Deploy gateway"}
        with patch.object(self.business, "application_steps", return_value=[step]):
            version = self.business.files.fields()["version"]
            self.assertEqual(self.business.preview()["required_steps"], [step])
            self.business.apply(version, "wait", True)
            await asyncio.wait_for(self.business.apply_task, 15)
        self.assertEqual(self.business.outcome, "running")
        self.assertEqual(self.business.loaded, version)
        self.assertEqual(self.business.application["phase"], "partially_applied")
        self.assertEqual(self.business.application["required_steps"], [step])

    async def test_force_requests_cancellation_before_stopping(self):
        original = self.business.request
        commands = []

        async def request(payload):
            commands.append(payload["command"])
            return await original(payload)

        self.business.request = request
        self.business.apply(self.business.files.fields()["version"], "force", True)
        await asyncio.wait_for(self.business.apply_task, 15)
        self.assertIn("management-cancel", commands)
        self.assertEqual(self.business.application["phase"], "applied")


class MaintenanceEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_calls_and_extension_background_work_prevent_false_idle(self):
        from types import SimpleNamespace
        from assistant_runtime.app.maintenance import Maintenance

        pending = asyncio.get_running_loop().create_future()

        class Extension:
            pass

        Extension.__module__ = "assistant_runtime.plugins.example"
        host = SimpleNamespace(
            intake=WorkIntake(),
            runtime=SimpleNamespace(
                plugins={"example": Extension()},
                contexts={"example": SimpleNamespace(calls={pending}, tasks={pending})},
            ),
        )
        maintenance = Maintenance(host)
        status = maintenance.pause()
        self.assertFalse(status["ready"])
        self.assertEqual({b["state"] for b in status["blockers"]}, {"capability_call", "drain_not_supported"})
        pending.cancel()
        host.runtime.contexts["example"].tasks.clear()
        self.assertTrue(maintenance.status()["ready"])
        maintenance.resume()
        self.assertFalse(host.intake.paused)

    async def test_failed_new_candidate_keeps_existing_worker_and_confirmation_is_version_bound(self):
        from assistant_runtime.core.configuration.files import Conflict

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text(f'root="{root}"\nplugins=[]\n')
            business = BusinessProcess(
                ManagementSettings(root, entry, startup_seconds=10, shutdown_seconds=10)
            )
            try:
                await business.start()
                pid = business.process.pid
                version = business.loaded
                entry.write_text(entry.read_text() + 'assistant_name="changed"\n')
                with self.assertRaises(Conflict):
                    business.apply(version, "wait", True)
                entry.write_text("bad= [\n")
                with self.assertRaises(ValueError):
                    business.apply(version, "force", True)
                self.assertEqual(business.process.pid, pid)
                self.assertIsNone(business.process.returncode)
            finally:
                await business.stop()


class ConsoleConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_queued_writer_rechecks_freeze_and_apply_joins_the_same_write_lock(self):
        from types import SimpleNamespace
        from assistant_runtime.core.configuration.console import Console

        calls = []
        console = object.__new__(Console)
        console.writes = asyncio.Lock()
        console.business = SimpleNamespace(application={"phase": "idle"})
        console.files = SimpleNamespace(patch=lambda *a, **kw: calls.append("saved"))

        class Request:
            async def json(self):
                return {"version": "test", "patches": [], "mode": "wait", "confirmed": True}

        await console.writes.acquire()
        saving = asyncio.create_task(console.save(Request()))
        await asyncio.sleep(0)
        console.business.application["phase"] = "waiting"
        console.writes.release()
        with self.assertRaises(ValueError):
            await saving
        self.assertFalse(calls)
        console.business.apply = lambda *a: calls.append("apply") or {"phase": "preparing"}
        await console.writes.acquire()
        applying = asyncio.create_task(console.apply(Request()))
        await asyncio.sleep(0)
        self.assertFalse(calls)
        console.writes.release()
        await applying
        self.assertEqual(calls, ["apply"])
