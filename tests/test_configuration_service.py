"""Configuration identity, inactive owners and effective defaults use private instances."""

import json
from pathlib import Path
import tempfile
import unittest
import asyncio
from unittest.mock import patch

from assistant_runtime.app.config import Config
from assistant_runtime.app.management_bootstrap import control_config, resolve_management
from assistant_runtime.api.capacity import Capacity
from assistant_runtime.api.configuration import ConfigurationUpdate
from assistant_runtime.core.configuration.updates import UpdateCoordinator


class ConfigurationIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "config").mkdir()
        self.entry = self.root / "config/config.toml"
        self.entry.write_text('root=".."\nplugins=[]\n', encoding="utf-8")
        (self.root / "management.toml").write_text(
            "root=" + json.dumps(str(self.root)) + "\nentry=" + json.dumps(str(self.entry)),
            encoding="utf-8",
        )

    def test_explicit_entry_cannot_select_another_instance_entry(self):
        other = self.entry.with_name("other.toml")
        other.write_text("plugins=[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_management(other)

    def test_control_identity_survives_syntactically_broken_business_config(self):
        self.entry.write_text("not [toml", encoding="utf-8")
        identity = control_config(self.entry)
        self.assertEqual(identity.root, self.root)
        self.assertEqual(identity.state_dir, self.root / "state")

    def test_workspace_is_instance_relative_and_independent_of_process_directory(self):
        with patch("pathlib.Path.cwd", return_value=self.root / "unrelated"):
            config = Config.load(self.entry)
        self.assertEqual(Path(config.workspace), self.root)
        self.assertEqual(
            Path(Config(root=self.root, plugins=[], workspace="work").workspace), self.root / "work"
        )

    def test_disabled_owner_keeps_structural_settings_without_activation_validation(self):
        config = Config(root=self.root, plugins=[], web={"port": 70000}, wake_words=[])
        self.assertEqual(config.web["port"], 70000)
        with self.assertRaises(ValueError):
            Config(root=self.root, plugins=[], web={"port": "invalid type"})

    def test_dormant_model_settings_do_not_block_other_application_steps(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        self.entry.write_text('root=".."\nplugins=[]\n[model_api]\nconcurrency=0\n', encoding="utf-8")
        business = BusinessProcess(ManagementSettings(self.root, self.entry))
        self.assertEqual(business.application_steps(business.files.snapshot()), [])


class ConfigurationUpdateTest(unittest.IsolatedAsyncioTestCase):
    async def test_unrecoverable_owner_stops_new_background_dispatch_until_shutdown(self):
        from types import SimpleNamespace
        from assistant_runtime.api.lifecycle import configuration_boundary

        ready, cancelled = asyncio.Event(), asyncio.Event()
        ready.set()
        context = SimpleNamespace(
            runtime=SimpleNamespace(configuration_ready=ready),
            state="failed",
            cancelled=cancelled,
        )
        waiting = asyncio.create_task(configuration_boundary(context))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        cancelled.set()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

    async def test_volume_policy_changes_without_replacing_or_cancelling_audio(self):
        from types import SimpleNamespace
        from assistant_runtime.app.configuration_replacement import prepare

        route_settings = SimpleNamespace(minimum_volume=10)
        provider = SimpleNamespace(minimum_volume=10)
        plugin = SimpleNamespace(route=SimpleNamespace(config=route_settings))
        current = SimpleNamespace(devices={}, default_output="speaker")
        host = SimpleNamespace(
            config=current,
            runtime=SimpleNamespace(plugins={"pipewire": plugin}),
            audio_outputs=SimpleNamespace(providers={"speaker": provider}),
        )
        change = await prepare(host, "pipewire", SimpleNamespace(devices={}, minimum_volume=20))
        await change.commit()
        self.assertEqual((route_settings.minimum_volume, provider.minimum_volume), (20, 20))
        self.assertIs(host.runtime.plugins["pipewire"], plugin)
        await change.rollback()
        self.assertEqual((route_settings.minimum_volume, provider.minimum_volume), (10, 10))

    async def test_cli_client_uses_operator_authentication_and_saved_revision(self):
        from aiohttp.test_utils import TestServer
        from types import SimpleNamespace
        from assistant_runtime.core.configuration.console import Console, web
        from assistant_runtime.core.configuration.auth import AdminAccess
        from assistant_runtime.core.configuration.client import operate

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            console = object.__new__(Console)
            console.access = AdminAccess(root / "state/configuration/admin-token")

            async def snapshot(request):
                return web.json_response({"version": "saved-revision"})

            async def update(request):
                self.assertEqual(await request.json(), {"version": "saved-revision"})
                return web.json_response({"phase": "preparing"})

            app = web.Application(middlewares=[console.guard])
            app.add_routes(
                [
                    web.post("/api/login", console.login),
                    web.get("/api/configuration", snapshot),
                    web.get("/api/status", snapshot),
                    web.post("/api/update", update),
                ]
            )
            async with TestServer(app) as server:
                settings = SimpleNamespace(root=root, host="0.0.0.0", port=server.port)
                self.assertEqual(await operate(settings, "status"), {"version": "saved-revision"})
                self.assertEqual(await operate(settings, "apply"), {"phase": "preparing"})

    async def test_connection_replacement_releases_registrations_and_recovers_failed_candidate(self):
        from types import SimpleNamespace
        from assistant_runtime.app.configuration_replacement import prepare
        from assistant_runtime.app.plugin_context import (
            BoundExtension,
            DeviceRegistration,
            ResourceRegistration,
            environment,
        )
        from assistant_runtime.core.devices.directory import DeviceDirectory, DevicesPlugin
        from assistant_runtime.core.devices.video import VideoInputs
        from assistant_runtime.kernel.runtime import Runtime, Manifest

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def config(ident):
                return SimpleNamespace(
                    root=root,
                    state_dir=root / "state",
                    devices={
                        ident: {"adapter": "go2rtc", "kind": "camera", "label": ident},
                    },
                )

            original = config("original")
            runtime = Runtime(None)
            host = SimpleNamespace(
                config=original,
                runtime=runtime,
                store=SimpleNamespace(get=lambda *a: None, set=lambda *a: None),
                devices=DeviceDirectory(original.devices),
                video_inputs=VideoInputs(original.devices),
                device_resources=SimpleNamespace(
                    held={("unrelated-microphone", "audio.input"): {"voice": {}}}
                ),
            )

            def create(host, name):
                settings = host.config
                public, attach = environment(
                    host,
                    name,
                    settings,
                    resources={
                        "directory": DeviceRegistration(host.devices, name),
                        "video": ResourceRegistration(host.video_inputs, name),
                    },
                )

                class Adapter:
                    manifest = Manifest("go2rtc")

                    def maintenance_pending(self):
                        return []

                    async def start(self, context):
                        for ident in settings.devices:

                            async def probe():
                                return None

                            context.resource("directory").register(name, ident, ["video.snapshot"], probe)
                            context.resource("video").register(name, ident, object())
                        if "broken" in settings.devices:
                            raise OSError("candidate connection refused")

                return BoundExtension(Adapter(), public, attach)

            runtime.add(create(host, "go2rtc"))
            await runtime.start()
            devices = DevicesPlugin(host.devices, host.video_inputs)
            runtime.plugins["devices"] = devices
            devices.register_operations()
            try:
                with patch("assistant_runtime.app.bundled.create", side_effect=create):
                    candidate = config("replacement")
                    change = await prepare(host, "go2rtc", candidate)
                    await change.commit()
                    self.assertEqual(set(host.devices.entries), {"replacement"})
                    self.assertEqual(set(host.video_inputs.providers), {"replacement"})
                    self.assertEqual(set(devices.operations.routes), {("replacement", "video.snapshot")})
                    await change.rollback()
                    self.assertEqual(set(host.devices.entries), {"original"})
                    coordinator = UpdateCoordinator()
                    with self.assertRaises(OSError):
                        await coordinator.apply(
                            [("go2rtc", lambda: prepare(host, "go2rtc", config("broken")))]
                        )
                    self.assertFalse(coordinator.failed_owners)
                    self.assertEqual(set(host.devices.entries), {"original"})
                    self.assertEqual(set(host.video_inputs.providers), {"original"})
                    self.assertEqual(set(devices.operations.routes), {("original", "video.snapshot")})
                    self.assertEqual(runtime.contexts["go2rtc"].state, "ready")
                    self.assertIs(host.devices.config, original.devices)
            finally:
                await runtime.close()
            self.assertFalse(host.devices.entries)
            self.assertFalse(host.video_inputs.providers)

    async def test_rollback_can_invoke_owned_capability_while_new_requests_wait(self):
        from assistant_runtime.kernel.runtime import Runtime, Context

        class Probe:
            pass

        runtime = Runtime(None)
        context = runtime.contexts["probe"] = Context(runtime, "probe")
        context.state = "ready"

        async def handle(request):
            return "restored"

        runtime.register("probe", Probe, handle)
        coordinator = UpdateCoordinator()
        runtime.configuration_tasks = coordinator.authorized_tasks
        runtime.configuration_ready.clear()
        restored = []

        async def prepare():
            async def commit():
                raise OSError("candidate connection failed")

            async def rollback():
                restored.append(await runtime.invoke(Probe()))

            return ConfigurationUpdate(commit, rollback)

        incoming = asyncio.create_task(runtime.invoke(Probe()))
        with self.assertRaises(OSError):
            await asyncio.wait_for(coordinator.apply([("probe", prepare)]), 2)
        self.assertEqual(restored, ["restored"])
        self.assertFalse(incoming.done())
        runtime.configuration_ready.set()
        self.assertEqual(await incoming, "restored")
        self.assertFalse(coordinator.authorized_tasks)

    async def test_resource_view_does_not_pin_spawned_tasks_to_an_old_generation(self):
        import os
        from assistant_runtime.api.configuration_io import configuration_path, resource_view

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            root = Path(folder)
            original = root / "secret"
            old, new = root / "old.json", root / "new.json"
            old.write_text(json.dumps({str(original.absolute()): str(root / "old")}), encoding="utf-8")
            new.write_text(json.dumps({str(original.absolute()): str(root / "new")}), encoding="utf-8")
            proceed = asyncio.Event()

            async def background():
                await proceed.wait()
                return configuration_path(original)

            with resource_view(old):
                self.assertEqual(configuration_path(original), root / "old")
                task = asyncio.create_task(background())
            os.environ["PERSONAL_AGENT_RESOURCE_MANIFEST"] = str(new)
            proceed.set()
            self.assertEqual(await task, root / "new")

    async def test_unrecoverable_owner_is_recorded_and_prepared_resources_are_released(self):
        released = []

        async def prepare():
            async def fail():
                raise OSError("unavailable")

            async def release(committed):
                released.append(committed)

            return ConfigurationUpdate(fail, fail, release)

        coordinator = UpdateCoordinator()
        with self.assertRaises(OSError):
            await coordinator.apply([("connection", prepare)])
        self.assertEqual(coordinator.failed_owners, {"connection"})
        self.assertEqual(released, [False])

    async def test_failed_candidate_is_not_retried_after_repeated_business_restarts(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            entry = root / "config.toml"
            entry.write_text("root=" + json.dumps(str(root)) + '\nplugins=["ai_usage"]\n', encoding="utf-8")
            business = BusinessProcess(
                ManagementSettings(root, entry, startup_seconds=15, shutdown_seconds=10)
            )
            try:
                await business.start()
                effective = business.loaded
                saved = business.files.patch(
                    effective, [{"path": ["ai_usage", "max_requests"], "op": "set", "value": 31}]
                )
                with patch.object(
                    business, "request", AsyncMock(side_effect=ConnectionError("lost response"))
                ):
                    business.update(saved["version"])
                    await business.apply_task
                self.assertEqual(business.application["phase"], "failed")
                for _ in range(2):
                    await business.stop()
                    await business.start()
                    self.assertEqual(business.outcome, "running", business.error)
                    self.assertEqual(business.loaded, effective)
                    self.assertTrue((business.state / "blocked-update.json").exists())
                self.assertEqual(business.files.snapshot().version, saved["version"])
            finally:
                await business.stop()

    async def test_lowering_capacity_preserves_leases_and_blocks_new_work(self):
        capacity = Capacity(2)
        await capacity.acquire()
        await capacity.acquire()
        capacity.resize(1)
        waiting = asyncio.create_task(capacity.acquire())
        await asyncio.sleep(0)
        capacity.release()
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        capacity.release()
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(capacity.active, 1)
        capacity.resize(3)
        await capacity.acquire()
        capacity.release()
        capacity.release()

    async def test_failed_commit_rolls_back_already_applied_and_partial_owner(self):
        active = {"first": "old", "second": "old"}
        released = []

        def prepare(name, fail=False):
            async def create():
                async def commit():
                    active[name] = "new"
                    if fail:
                        raise RuntimeError("resource unavailable")

                async def rollback():
                    active[name] = "old"

                async def release(committed):
                    released.append((name, committed))

                return ConfigurationUpdate(commit, rollback, release)

            return create

        coordinator = UpdateCoordinator()
        with self.assertRaises(RuntimeError):
            await coordinator.apply([("first", prepare("first")), ("second", prepare("second", True))])
        self.assertEqual(active, {"first": "old", "second": "old"})
        self.assertEqual(released, [("second", False), ("first", False)])

    async def test_real_worker_applies_budget_without_restart_and_retains_restart_candidate(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            entry = root / "config.toml"
            entry.write_text("root=" + json.dumps(str(root)) + '\nplugins=["ai_usage"]\n', encoding="utf-8")
            business = BusinessProcess(
                ManagementSettings(root, entry, startup_seconds=15, shutdown_seconds=10)
            )
            try:
                await business.start()
                self.assertEqual(business.outcome, "running", business.error)
                pid = business.process.pid
                first = business.files.snapshot()
                saved = business.files.patch(
                    first.version, [{"path": ["ai_usage", "max_requests"], "op": "set", "value": 37}]
                )
                business.update(saved["version"])
                await asyncio.wait_for(business.apply_task, 10)
                self.assertEqual(business.application["phase"], "applied", business.application)
                self.assertEqual(business.process.pid, pid)
                status = await business.request({"command": "management-status"})
                self.assertEqual(status["configuration_version"], saved["version"])
                # A successful business update must not hide a failed independent listener switch.
                from unittest.mock import AsyncMock

                business.application["management_pending"] = True
                business.management_applied = AsyncMock(side_effect=OSError("address already in use"))
                business.update(saved["version"])
                await asyncio.wait_for(business.apply_task, 10)
                self.assertEqual(business.application["phase"], "partially_applied")
                self.assertTrue(business.application["management_pending"])
                self.assertEqual(business.process.pid, pid)
                business.management_applied = AsyncMock(return_value="http://127.0.0.1:8047")
                business.update(saved["version"])
                await asyncio.wait_for(business.apply_task, 10)
                self.assertEqual(business.application["phase"], "applied")
                self.assertFalse(business.application["management_pending"])
                business.management_applied.assert_awaited_once()
                saved = business.files.patch(
                    saved["version"], [{"path": ["plugins"], "op": "set", "value": []}]
                )
                business.update(saved["version"])
                await asyncio.wait_for(business.apply_task, 10)
                self.assertEqual(business.application["phase"], "pending_restart", business.application)
                self.assertEqual(business.process.pid, pid)
                self.assertNotEqual(business.loaded, saved["version"])
            finally:
                await business.stop()
