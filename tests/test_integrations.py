"""Instance MCP ownership, explicit scopes, revisions, redaction and provider lifecycle."""

from platform_support import private_mode

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.app.config import Config
from assistant_runtime.api.contracts import IntegrationRequest, TaskRequest
from assistant_runtime.kernel.runtime import Runtime, Manifest
from tool_expectations import INTEGRATION_TOOLS
from assistant_runtime.core.integrations.tools import create_server
from assistant_runtime.core.pi.backend import PiBackend
from assistant_runtime.core.pi.integrations import PiIntegrations
from assistant_runtime.core.pi.mcp import server_config
from assistant_runtime.core.speech.disabled import DisabledVoice
from assistant_runtime.core.integrations.registry import McpRegistry
from assistant_runtime.core.integrations.settings import IntegrationSettings, definition
from assistant_runtime.core.integrations.plugin import IntegrationsPlugin
from assistant_runtime.app.store import Store

STDIO = {"transport": {"type": "stdio", "command": "python", "args": ["--flag", "--flag", ""]}}
HTTP = {"transport": {"type": "http", "url": "https://example.test/mcp?private=value"}}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = McpRegistry(self.root, {})

    def tearDown(self):
        self.temp.cleanup()

    def test_private_atomic_roundtrip_revision_and_idempotent_retry(self):
        r = self.registry
        self.assertEqual(r.path, self.root / "config/mcp/servers.json")
        self.assertEqual(r.work, self.root / "state/mcp/servers")
        r.configure("mobile", STDIO, 0)
        self.assertEqual(r.revision, 1)
        self.assertEqual(private_mode(r.path), 0o600)
        self.assertEqual(r.configure("mobile", STDIO, 0)["revision"], 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            r.configure("other", HTTP, 0)
        r.configure("other", HTTP, 1)
        reopened = McpRegistry(self.root, {})
        self.assertEqual(reopened.snapshot(), r.snapshot())
        before = r.snapshot()
        with patch(
            "assistant_runtime.core.integrations.registry.os.replace", side_effect=OSError("disk error")
        ):
            with self.assertRaises(OSError):
                r.configure("mobile", {"enabled": False}, 2)
        self.assertEqual(r.snapshot(), before)
        self.assertEqual(McpRegistry(self.root, {}).snapshot(), before)
        self.assertEqual([p.name for p in r.path.parent.iterdir()], [r.path.name])

    def test_values_are_redacted_and_removal_does_not_retain_credentials(self):
        spec = copy.deepcopy(STDIO)
        spec["transport"].update(args=["private-command-argument"], env={"TOKEN": "private-environment"})
        self.registry.configure("mobile", spec, 0)
        output = json.dumps(self.registry.public("mobile"))
        self.assertNotIn("private-command-argument", output)
        self.assertNotIn("private-environment", output)
        self.registry.remove("mobile", 1)
        self.assertNotIn("private-environment", self.registry.path.read_text())
        self.assertEqual(self.registry.snapshot()["removed"], ["mobile"])
        self.registry.configure("mobile", HTTP, 2)
        self.assertEqual(self.registry.snapshot()["removed"], [])
        self.assertNotIn("private=value", json.dumps(self.registry.public("mobile")))

    def test_project_registries_are_not_discovered_and_custom_private_path_is_explicit(self):
        project = self.root / ".agents/mcp/servers.json"
        project.parent.mkdir(parents=True)
        project.write_text("Coding AI configuration must never be read")
        self.assertEqual(McpRegistry(self.root, {}).revision, 0)
        self.assertFalse(self.registry.path.exists())
        private = self.root / "custom/servers.json"
        private.parent.mkdir(parents=True)
        private.write_text('{"revision":0,"servers":{},"removed":[]}')
        registry = McpRegistry(self.root, {"registry_path": str(private)})
        self.assertEqual(registry.path, private)
        self.assertEqual(registry.revision, 0)

    def test_validation_never_replaces_good_registry_and_manual_edits_require_revision(self):
        self.registry.configure("first", STDIO, 0)
        before = self.registry.snapshot()
        data = copy.deepcopy(before)
        data["servers"]["first"]["definition"]["enabled"] = False
        self.registry.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Increment"):
            self.registry.reload()
        self.assertEqual(self.registry.snapshot(), before)
        data["revision"] += 1
        data["servers"]["first"]["revision"] += 1
        self.registry.path.write_text(json.dumps(data))
        self.registry.reload()
        self.assertFalse(self.registry.get("first")["definition"]["enabled"])
        self.registry.path.write_text("not json")
        with self.assertRaises(ValueError):
            self.registry.reload()
        self.assertEqual(self.registry.revision, 2)

    def test_strict_transport_and_policy_validation(self):
        for changes in (
            {"contexts": ["maintenance"]},
            {"enabled": 1},
            {"approval": "automatic"},
            {"tool_timeout_seconds": True},
            {"tool_timeout_seconds": 3601},
            {"startup_timeout_seconds": 3601},
            {"allow_tools": "all"},
            {"unknown": True},
            {"transport": {"type": "stdio", "command": "p", "cwd": "relative"}},
            {"transport": {"type": "stdio", "command": "p", "cwd": "/invalid\0path"}},
            {"transport": {"type": "http", "url": "https://user:secret@example.test/"}},
            {"transport": {"type": "http", "url": "file:///etc/passwd"}},
            {"transport": {"type": "http", "url": "https://host/", "bearer_token_env": []}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                definition({**STDIO, **changes})
        self.assertEqual(definition(STDIO)["transport"]["args"], ["--flag", "--flag", ""])
        self.assertEqual(definition(STDIO)["approval"], "prompt")
        with self.assertRaises(ValueError):
            IntegrationSettings.parse({"registry_path": "/invalid\0path"})


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = McpRegistry(Path(self.temp.name), {})
        self.config = Config(root=Path(self.temp.name), workspace=self.temp.name)
        self.client = SimpleNamespace(request=AsyncMock(side_effect=self.rpc), respond=AsyncMock())
        self.backend = SimpleNamespace(
            config=self.config,
            client=self.client,
            submit_lock=asyncio.Lock(),
            busy=False,
            active_turn=None,
            voice_active=lambda: False,
            execution_session_id="foreground",
        )
        self.adapter = PiIntegrations(self.backend, self.registry)
        self.catalog = {"echo": {"description": "synthetic", "inputSchema": {"type": "object"}}}
        self.status = "connected"
        self.adapter.inherited = {"unmanaged": {"command": "unknown", "env": {"TOKEN": "private"}}}
        self.registry.configure("mobile", STDIO, 0)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def rpc(self, method, params, **kwargs):
        if method == "configuration.read":
            return {"config": {"mcp_servers": self.adapter.inherited}}
        if method == "session.read":
            return {"session": {"status": {"type": "idle"}}}
        if method == "session.create":
            return {"session": {"id": "probe"}}
        if method == "mcp.inspect":
            return {
                "data": [
                    {
                        "name": "external_mobile",
                        "tools": self.catalog,
                        "runtimeStatus": self.status,
                        "authStatus": "unsupported",
                        "toolsError": None,
                    }
                ]
            }
        return {}

    async def test_scopes_inherited_policy_and_maintenance_are_explicit(self):
        self.registry.configure(
            "mobile",
            {"contexts": ["foreground"], "approval": "approve", "tool_approvals": {"danger": "reject"}},
            1,
        )
        foreground = self.adapter.configuration("foreground")
        self.assertTrue(foreground["external_mobile"]["enabled"])
        self.assertFalse(foreground["unmanaged"]["enabled"])
        self.assertIn("danger", foreground["external_mobile"]["disabled_tools"])
        self.assertNotIn("danger", foreground["external_mobile"]["tools"])
        self.assertFalse(self.adapter.configuration("background", "run")["external_mobile"]["enabled"])
        self.assertTrue(
            all(not spec["enabled"] for spec in self.adapter.configuration("maintenance").values())
        )
        self.registry.remove("mobile", 2)
        self.assertFalse(self.adapter.configuration("foreground")["external_mobile"]["enabled"])

    async def test_apply_defers_busy_voice_then_resumes_same_idle_thread_without_turn(self):
        self.backend.busy = True
        self.assertEqual((await self.adapter.apply())["state"], "deferred")
        self.backend.busy = False
        self.backend.voice_active = lambda: True
        self.assertEqual((await self.adapter.apply())["state"], "deferred")
        self.client.request.assert_not_awaited()
        self.backend.voice_active = lambda: False
        result = await self.adapter.apply()
        self.assertEqual(result["state"], "applied")
        methods = [call.args[0] for call in self.client.request.await_args_list]
        self.assertEqual(methods, ["configuration.read", "session.read", "session.close", "session.resume"])
        self.assertEqual(self.client.request.await_args.args[1]["session_id"], "foreground")
        self.assertNotIn("run.start", methods)
        self.client.request.reset_mock()
        await self.adapter.apply()
        self.client.request.assert_not_awaited()
        await self.adapter.apply(force=True)
        self.assertEqual(self.client.request.await_count, 4)

    async def test_owned_refresh_crosses_configuration_barrier_but_other_requests_stay_paused(self):
        parent = asyncio.current_task()
        ready = asyncio.Event()
        self.client.configuration_tasks = {parent}

        async def guarded(method, params, **kwargs):
            if asyncio.current_task() not in self.client.configuration_tasks:
                await ready.wait()
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = guarded
        other = asyncio.create_task(self.client.request("session.read", {}))
        try:
            async with asyncio.timeout(1):
                result = await self.adapter.apply(force=True, staged=True)
            self.assertEqual(result["state"], "applied")
            self.assertFalse(other.done())
            self.assertEqual(self.client.configuration_tasks, {parent})
        finally:
            ready.set()
            await other

    async def test_probe_is_real_provider_discovery_and_closes_only_its_thread(self):
        result = await self.adapter.verify("mobile", "background", True)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["tools"][0]["input_schema"], {"type": "object"})
        self.assertFalse(self.adapter.probes)
        methods = [c.args[0] for c in self.client.request.await_args_list]
        self.assertEqual(methods, ["configuration.read", "session.create", "mcp.inspect", "session.close"])
        self.assertNotIn("mcpServer/tool/call", methods)
        self.assertNotIn("run.start", methods)
        self.assertEqual(self.client.request.await_args.args[1]["session_id"], "probe")

    async def test_bad_refresh_restores_known_configuration_without_repeated_automatic_attempts(self):
        await self.adapter.apply()
        old = copy.deepcopy(self.adapter.applied_configuration)
        self.registry.configure("mobile", {"tool_timeout_seconds": 10}, 1)
        calls = []

        async def rpc(method, params, **kwargs):
            calls.append(method)
            if method == "session.resume" and params["config"]["mcp_servers"] != old:
                raise RuntimeError("configuration failure with private details")
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        result = await self.adapter.apply()
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["using_previous"])
        self.assertEqual(self.adapter.applied_revision, 1)
        self.assertEqual(self.adapter.failed_revision, 2)
        count = len(calls)
        await self.adapter.apply()
        self.assertEqual(len(calls), count)
        self.assertNotIn("run.start", calls)
        self.assertNotIn("private details", json.dumps(result))

    async def test_disconnected_apply_keeps_lock_until_foreground_is_restored(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def rpc(method, params, **kwargs):
            if method == "session.resume":
                started.set()
                await release.wait()
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        pending = asyncio.create_task(self.adapter.apply())
        await started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assertTrue(self.backend.submit_lock.locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.backend.submit_lock.locked())
        self.assertEqual(self.adapter.applied_revision, self.registry.revision)
        self.assertFalse(self.adapter.status()["foreground_pending"])

    async def test_unconfirmed_rollback_blocks_text_and_voice_even_when_refresh_is_deferred(self):
        backend = PiBackend(self.config, None, AsyncMock(), AsyncMock(), self.registry)
        adapter = backend.integrations
        backend.execution_session_id = "foreground"
        backend.client = self.client
        backend.client.settings = SimpleNamespace(enabled=True)
        await adapter.apply()
        self.registry.configure("mobile", {"enabled": False}, self.registry.revision)

        async def rpc(method, params, **kwargs):
            if method == "session.resume":
                raise RuntimeError("cannot resume")
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        with self.assertRaisesRegex(RuntimeError, "rollback unconfirmed"):
            await adapter.apply()
        self.assertIsNone(adapter.applied_revision)
        self.registry.configure("mobile", {"enabled": True}, self.registry.revision)
        backend.active_turn = "uncertain-provider-turn"
        self.assertEqual((await adapter.apply())["state"], "deferred")
        self.client.request.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "context is unconfirmed"):
            await backend.submit("must not be sent")
        voice = DisabledVoice()
        with self.assertRaisesRegex(RuntimeError, "Voice is not enabled"):
            await voice.open()
        self.assertFalse(voice.opened)
        self.client.request.assert_not_awaited()

    async def test_oauth_uses_owned_temporary_context_and_cleanup(self):
        self.registry.configure("mobile", HTTP, 1)
        tasks = []

        def spawn(coroutine, label):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        self.adapter.context = SimpleNamespace(spawn=spawn)

        async def rpc(method, params, **kwargs):
            if method == "mcp.login":
                return {"authorizationUrl": "https://example.test/authorize"}
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        result = await self.adapter.login("mobile")
        self.assertEqual(result["state"], "user_action_required")
        self.assertEqual(self.adapter.probes, {"probe"})
        await self.adapter.event(
            {
                "kind": "mcp.login_completed",
                "data": {"name": "external_mobile", "success": True},
            }
        )
        await asyncio.gather(*tasks)
        self.assertFalse(self.adapter.probes)
        self.assertFalse(self.adapter.logins)

    async def test_oauth_reserves_duplicate_and_capacity_limits_during_startup(self):
        names = ["mobile", "second", "third", "fourth", "fifth"]
        for ident in names:
            self.registry.configure(ident, HTTP, self.registry.revision)
        self.adapter.context = SimpleNamespace()
        started = asyncio.Queue()

        async def opening(ident, context):
            started.put_nowait(ident)
            await asyncio.Event().wait()

        pending = []
        with patch.object(self.adapter, "open_probe", side_effect=opening):
            try:
                for ident in names[:4]:
                    pending.append(asyncio.create_task(self.adapter.login(ident)))
                    self.assertEqual(await asyncio.wait_for(started.get(), 1), ident)
                with self.assertRaisesRegex(ValueError, "already in progress"):
                    await self.adapter.login("mobile")
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    await self.adapter.login("fifth")
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self.assertFalse(self.adapter.logins)
        self.assertFalse(self.adapter.probes)

    async def test_probe_detects_missing_tools_and_provider_failure(self):
        self.registry.configure("mobile", {"allow_tools": ["missing"]}, 1)
        result = await self.adapter.verify("mobile", "foreground")
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["missing_allowed_tools"], ["missing"])
        self.assertFalse(self.adapter.probes)
        self.status = "failed"
        self.assertEqual((await self.adapter.verify("mobile", "foreground"))["state"], "unavailable")

    async def test_canceled_discovery_unsubscribes_probe(self):
        started = asyncio.Event()

        async def rpc(method, params, **kwargs):
            if method == "mcp.inspect":
                started.set()
                await asyncio.Event().wait()
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        pending = asyncio.create_task(self.adapter.verify("mobile", "foreground"))
        await started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.adapter.probes)
        self.assertEqual(self.client.request.await_args.args[0], "session.close")

    async def test_preflight_reports_missing_dependencies_without_exposing_values(self):
        self.registry.configure(
            "mobile", {"transport": {"type": "stdio", "command": str(Path(self.temp.name) / "missing")}}, 1
        )
        self.assertEqual((await self.adapter.verify("mobile", "foreground"))["reason"], "command_not_found")
        self.registry.configure(
            "mobile",
            {
                "transport": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                    "bearer_token_env": "PERSONAL_AGENT_MISSING_TEST_TOKEN",
                }
            },
            2,
        )
        result = await self.adapter.verify("mobile", "foreground")
        self.assertEqual(result["missing_environment"], ["PERSONAL_AGENT_MISSING_TEST_TOKEN"])
        self.client.request.assert_not_awaited()

    async def test_current_cwd_configuration_is_captured_without_transplanting_other_project_servers(self):
        self.client.request.return_value = {
            "config": {"mcp_servers": {"new_project_server": {"command": "p", "tool_timeout_sec": None}}}
        }
        self.client.request.side_effect = None
        captured = await self.adapter.capture("background", "run", cwd=self.temp.name)
        self.assertEqual(captured["new_project_server"], {"enabled": False})
        self.assertNotIn("unmanaged", captured)
        self.assertEqual(self.client.request.await_args.args[1]["cwd"], self.temp.name)

    async def test_relative_executable_preflight_uses_server_working_directory(self):
        executable = Path(self.temp.name) / ("server.exe" if os.name == "nt" else "server")
        executable.write_text("fixture")
        executable.chmod(0o700)
        self.registry.configure(
            "mobile",
            {"transport": {"type": "stdio", "command": "./" + executable.name, "cwd": self.temp.name}},
            self.registry.revision,
        )
        self.assertEqual((await self.adapter.verify("mobile", "foreground"))["state"], "verified")

    async def test_canceled_open_waits_for_ack_and_releases_late_created_thread(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def rpc(method, params, **kwargs):
            if method == "session.create":
                started.set()
                await release.wait()
            return await self.rpc(method, params, **kwargs)

        self.client.request.side_effect = rpc
        pending = asyncio.create_task(self.adapter.open_probe("mobile", "foreground"))
        await started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.client.request.await_args.args[0], "session.close")

    def test_background_management_catalog_is_explicit(self):
        for manage in (False, True):
            item = server_config(
                self.config.socket,
                task_run="run",
                manage_integrations=manage,
                plugins=["memory", "devices", "ai_usage"] + (["integrations"] if True else []),
            )["integrations"]
            if manage:
                self.assertNotIn("enabled_tools", item)
            else:
                self.assertEqual(set(item["enabled_tools"]), set(INTEGRATION_TOOLS[:3]))
            self.assertEqual("--manage-integrations" in item["args"], manage)


class ManagementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.registry = McpRegistry(Path(self.temp.name), {})
        self.adapter = SimpleNamespace(
            status=lambda: {"foreground_pending": False},
            apply=AsyncMock(return_value={"state": "applied"}),
            verify=AsyncMock(return_value={"state": "verified"}),
            login=AsyncMock(),
        )
        self.host = SimpleNamespace(config=Config(root=Path(self.temp.name)), store=self.store)
        outer = self

        class Brain:
            manifest = Manifest("pi")

            async def start(self, context):
                async def scope(request):
                    return {"run": {"mcp_management": outer.can_manage}}

                context.register(TaskRequest, scope)

        self.can_manage = False
        self.runtime = Runtime(self.store)
        self.host.runtime = self.runtime
        self.runtime.add(Brain())
        self.plugin = IntegrationsPlugin(self.host, self.registry, lambda: self.adapter)
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_actual_mcp_tools_and_redacted_configuration(self):
        async with Client(create_server(SimpleNamespace(invoke=self.runtime.invoke))) as client:
            self.assertEqual({tool.name for tool in await client.list_tools()}, set(INTEGRATION_TOOLS))
            result = await client.call_tool(
                "configure_mcp_server", {"name": "mobile", "revision": 0, "changes": STDIO}
            )
            self.assertEqual(result.structured_content["state"], "saved")
            listing = (await client.call_tool("list_mcp_servers", {})).structured_content
            self.assertEqual(listing["revision"], 1)
            self.assertEqual(listing["servers"][0]["runtime_name"], "external_mobile")
            with self.assertRaises(ToolError):
                await client.call_tool(
                    "configure_mcp_server", {"name": "other", "revision": True, "changes": HTTP}
                )
        async with Client(create_server(SimpleNamespace(invoke=self.runtime.invoke), "run")) as client:
            self.assertEqual({tool.name for tool in await client.list_tools()}, set(INTEGRATION_TOOLS[:3]))
            await client.call_tool("verify_mcp_server", {"name": "mobile"})
            self.adapter.verify.assert_awaited_with("mobile", "background", False)

    async def test_background_management_requires_active_scoped_task(self):
        request = IntegrationRequest("configure", {"name": "mobile", "revision": 0, "changes": STDIO}, "run")
        with self.assertRaises(ValueError):
            await self.runtime.invoke(request)
        self.can_manage = True
        await self.runtime.invoke(request)
        self.can_manage = False
        with self.assertRaises(ValueError):
            await self.runtime.invoke(
                IntegrationRequest("enable", {"name": "mobile", "revision": 1, "enabled": False}, "run")
            )
        self.assertTrue(self.registry.get("mobile")["definition"]["enabled"])

    async def test_verification_receipt_is_stale_after_concurrent_configuration(self):
        self.registry.configure("mobile", STDIO, 0)
        started, release = asyncio.Event(), asyncio.Event()

        async def verify(*args):
            started.set()
            await release.wait()
            return {"state": "verified"}

        self.adapter.verify.side_effect = verify
        pending = asyncio.create_task(self.runtime.invoke(IntegrationRequest("verify", {"name": "mobile"})))
        await started.wait()
        self.registry.configure("mobile", {"enabled": False}, 1)
        release.set()
        result = await pending
        self.assertFalse(result["current"])
        item = await self.runtime.invoke(IntegrationRequest("get", {"name": "mobile"}))
        self.assertFalse(item["server"]["verification"]["foreground"]["current"])

    async def test_probe_failure_does_not_expose_transport_secrets_or_degrade_plugin(self):
        self.registry.configure("mobile", STDIO, 0)
        self.adapter.verify.side_effect = RuntimeError("private-token-in-underlying-error")
        result = await self.runtime.invoke(IntegrationRequest("verify", {"name": "mobile"}))
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("private-token", json.dumps(result))
        self.assertEqual(self.runtime.contexts["integrations"].state, "ready")

    async def test_manual_definition_change_invalidates_receipt_even_with_unchanged_server_revision(self):
        self.registry.configure("mobile", STDIO, 0)
        await self.runtime.invoke(IntegrationRequest("verify", {"name": "mobile"}))
        data = self.registry.snapshot()
        data["revision"] += 1
        data["servers"]["mobile"]["definition"]["tool_timeout_seconds"] = 10
        self.registry.path.write_text(json.dumps(data))
        await self.runtime.invoke(IntegrationRequest("reload"))
        result = await self.runtime.invoke(IntegrationRequest("get", {"name": "mobile"}))
        check = result["server"]["verification"]["foreground"]
        self.assertFalse(check["current"])
        self.assertNotIn("signature", check)

    async def test_validation_capacity_and_cancellation_cleanup_remain_owned(self):
        self.registry.configure("mobile", STDIO, 0)
        started, release = asyncio.Event(), asyncio.Event()

        async def verify(*args):
            started.set()
            await release.wait()
            return {"state": "verified"}

        self.adapter.verify.side_effect = verify
        requests = [
            asyncio.create_task(self.runtime.invoke(IntegrationRequest("verify", {"name": "mobile"})))
            for _ in range(4)
        ]
        try:
            await started.wait()
            for _ in range(20):
                if self.plugin.pending == 4:
                    break
                await asyncio.sleep(0)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                await self.runtime.invoke(IntegrationRequest("verify", {"name": "mobile"}))
        finally:
            for task in requests:
                task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
        self.assertEqual(self.plugin.pending, 0)
