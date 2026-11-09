"""Independent management pages: ownership, stale versions and no action replay."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from assistant_runtime.core.configuration import pages
from assistant_runtime.core.configuration.interface import validate_interface
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.app.configuration_updates import ConfigurationRuntime
from assistant_runtime.api.configuration import ConfigurationSpec, ConfigurationError

PAGE = {"version": 1, "root": "management", "entry": "index.html"}
SPEC = {
    "namespace": "plugin_settings.demo",
    "schema": {"type": "object", "properties": {"value": {"type": "string"}}},
    "interface": {
        "page": PAGE,
        "actions": {
            "write": {
                "read_only": False,
                "input": {"type": "object", "additionalProperties": False},
                "output": {"type": "object"},
            },
            "read": {
                "read_only": True,
                "input": {"type": "object", "additionalProperties": False},
                "output": {"type": "object"},
            },
        },
    },
}


class DeclarationTests(unittest.TestCase):
    def test_rejects_invalid_version_paths_and_non_boolean_action(self):
        for change in (
            {"version": 2},
            {"version": True},
            {"root": "../etc"},
            {"entry": "/tmp/x.html"},
            {"root": "x\\y"},
            {"entry": "x.js"},
            {"script": "https://example.test"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                pages.validate_page({**PAGE, **change})
        bad = deepcopy(SPEC["interface"])
        bad["actions"]["write"]["read_only"] = "false"
        with self.assertRaises(ValueError):
            validate_interface(bad)
        validate_interface(SPEC["interface"])

    def test_static_and_runtime_page_contracts_match(self):
        from assistant_runtime.core.configuration.owners import check_definition

        spec = SimpleNamespace(configuration=ConfigurationSpec(**deepcopy(SPEC)))
        check_definition(spec, SPEC)
        spec.configuration.interface["actions"]["write"]["read_only"] = True
        with self.assertRaisesRegex(ValueError, "contracts differ"):
            check_definition(spec, SPEC)

    def test_static_asset_containment_and_missing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "management").mkdir()
            (root / "management/index.html").write_text("example")
            with patch.object(pages, "package_directory", return_value=root):
                self.assertEqual(pages.asset("demo", PAGE, "index.html").read_text(), "example")
                for name in ("../outside", "/etc/passwd", "a/../../x", "not-found.js"):
                    with self.assertRaises(ValueError):
                        pages.asset("demo", PAGE, name)
                from platform_support import symlink_or_skip

                symlink_or_skip(root / "management/link.html", root / "management/index.html")
                with self.assertRaises(ValueError):
                    pages.asset("demo", PAGE, "link.html")


class PageEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        entry = self.root / "config.toml"
        entry.write_text(f'root="{self.root.as_posix()}"\nplugins=[]\n')
        self.business = BusinessProcess(ManagementSettings(self.root, entry))
        self.console = Console(self.business)
        self.version = self.business.files.snapshot().version
        self.business.status = AsyncMock(return_value={"loaded_version": self.version})
        self.business.request = AsyncMock(return_value={"ok": True})
        self.description = patch.object(pages, "description", return_value=SPEC)
        self.description.start()
        app = web.Application(middlewares=[self.console.guard])
        app.add_routes(
            [
                web.post("/api/login", self.console.login),
                web.post("/api/plugin-pages/{owner}/action", self.console.plugin_pages.action),
                web.post("/api/plugin-pages/{owner}/validate", self.console.plugin_pages.validate_patch),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.origin = str(self.client.make_url("/")).rstrip("/")
        await self.client.post(
            "/api/login",
            json={"token": (self.business.state / "admin-token").read_text().strip()},
            headers={"Origin": self.origin},
        )

    async def asyncTearDown(self):
        await self.client.close()
        await self.console.close()
        self.description.stop()
        self.temp.cleanup()

    async def call(self, ident="unique-request-0001", **overrides):
        data = {
            "version": self.version,
            "action": "write",
            "arguments": {},
            "request_id": ident,
            "confirmed": True,
            **overrides,
        }
        return await self.client.post(
            "/api/plugin-pages/demo/action", json=data, headers={"Origin": self.origin}
        )

    async def test_version_confirmation_and_origin_rejected_before_dispatch(self):
        for overrides in (
            {"version": "stale"},
            {"confirmed": False},
            {"action": "missing"},
            {"arguments": {"not_declared": True}},
        ):
            response = await self.call(**overrides)
            self.assertGreaterEqual(response.status, 400)
        response = await self.client.post(
            "/api/plugin-pages/demo/action", json={}, headers={"Origin": "http://other.test"}
        )
        self.assertEqual(response.status, 403)
        self.business.request.assert_not_awaited()

    async def test_timeout_receipt_survives_recreation_and_never_replays(self):
        self.business.request.side_effect = TimeoutError("private-upstream-token")
        response = await self.call()
        result = await response.json()
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("private-upstream-token", json.dumps(result))
        from assistant_runtime.core.configuration.page_service import PageService

        self.console.plugin_pages = PageService(self.console)
        result = await (await self.call()).json()
        self.assertEqual(result["state"], "unknown")
        self.business.request.assert_awaited_once()

    async def test_concurrent_clicks_dispatch_once_and_success_stays_recorded(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(payload):
            started.set()
            await release.wait()
            return {"ok": True}

        self.business.request.side_effect = slow
        first = asyncio.create_task(self.call())
        await started.wait()
        second = await self.call("different-request-0002")
        self.assertEqual(second.status, 409)
        release.set()
        self.assertEqual((await (await first).json())["state"], "completed")
        self.assertEqual((await (await self.call()).json())["state"], "completed")
        self.business.request.assert_awaited_once()

    async def test_receipt_cannot_be_reused_for_different_arguments(self):
        pages.description.return_value = deepcopy(SPEC)
        pages.description.return_value["interface"]["actions"]["write"]["input"]["properties"] = {
            "label": {"type": "string"}
        }
        await self.call(arguments={"label": "first"})
        response = await self.call(arguments={"label": "second"})
        self.assertEqual(response.status, 400)
        self.business.request.assert_awaited_once()

    async def test_unapplied_write_is_blocked_but_query_uses_loaded_version(self):
        self.business.status.return_value = {"loaded_version": "previous"}
        self.assertEqual((await self.call()).status, 400)
        response = await self.call(action="read")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.business.request.call_args.args[0]["version"], "previous")

    async def test_cross_owner_patch_and_prototype_rejected(self):
        for path in (["__proto__"], ["..", "models"], ["model_api", "enabled"]):
            response = await self.client.post(
                "/api/plugin-pages/demo/validate",
                json={"version": self.version, "patch": {"path": path, "op": "set", "value": True}},
                headers={"Origin": self.origin},
            )
            self.assertGreaterEqual(response.status, 400)
        self.business.request.assert_not_awaited()


class RuntimeActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_schema_failure_after_write_is_unknown(self):
        plugin = SimpleNamespace(
            definition=SimpleNamespace(
                configuration=ConfigurationSpec(
                    interface={
                        "actions": {
                            "write": {
                                "read_only": False,
                                "input": {"type": "object"},
                                "output": {"type": "integer"},
                            }
                        }
                    }
                )
            ),
            configuration_action=AsyncMock(return_value="invalid-result"),
        )
        runtime = ConfigurationRuntime(
            SimpleNamespace(runtime=SimpleNamespace(plugins={"demo": plugin}, contexts={}))
        )
        with self.assertRaises(ConfigurationError) as error:
            await runtime.action("demo", "write", {}, True)
        self.assertEqual(error.exception.code, "outcome_unknown")
        plugin.configuration_action.assert_awaited_once()

    async def test_output_refs_are_redacted_and_stale_worker_rejects_before_dispatch(self):
        schema = {
            "$defs": {"secret": {"type": "string", "writeOnly": True}},
            "type": "object",
            "properties": {"token": {"$ref": "#/$defs/secret"}},
        }
        plugin = SimpleNamespace(
            definition=SimpleNamespace(
                configuration=ConfigurationSpec(
                    interface={"actions": {"read": {"read_only": True, "output": schema}}}
                )
            ),
            configuration_action=AsyncMock(return_value={"token": "secret-content"}),
        )
        runtime = ConfigurationRuntime(
            SimpleNamespace(runtime=SimpleNamespace(plugins={"demo": plugin}, contexts={}))
        )
        with self.assertRaises(ConfigurationError):
            await runtime.action("demo", "read", {}, version="stale")
        plugin.configuration_action.assert_not_awaited()
        self.assertEqual(await runtime.action("demo", "read", {}), {"token": {"configured": True}})
