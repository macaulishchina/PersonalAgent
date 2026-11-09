"""Manual operator restart against isolated gateways; never restart a real service."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from aiohttp import web

from assistant_runtime.api.configuration import ConfigurationError
from assistant_runtime.app.configuration_updates import ConfigurationRuntime
from assistant_runtime.core.configuration.catalog import catalog
from assistant_runtime.core.configuration.interface import describe
from assistant_runtime.plugins.go2rtc.camera import Go2RTCCamera
from assistant_runtime.plugins.go2rtc.plugin import Go2RTCPlugin


class Go2RTCManagementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.status = 200
        self.disconnect = False
        self.authorization = None
        app = web.Application()
        app.router.add_post("/api/restart", self.restart)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        config = SimpleNamespace(
            state_dir=Path("unused-private-test-state"),
            devices={
                name: {"adapter": "go2rtc", "kind": "camera", **self.spec(self.base)}
                for name in ("camera", "second_lens")
            },
        )
        self.plugin = Go2RTCPlugin(config, None, None)
        for provider in self.plugin.providers.values():
            await provider.start()
            provider.identity = "previous-source"
            provider.capture_generation = provider.generation
        self.runtime = ConfigurationRuntime(
            SimpleNamespace(runtime=SimpleNamespace(plugins={"go2rtc": self.plugin}))
        )

    @staticmethod
    def spec(base):
        return {"connection": {"base_url": base, "stream": "camera", "timeout_seconds": 0.2}}

    async def asyncTearDown(self):
        for provider in self.plugin.providers.values():
            await provider.close()
        await self.runner.cleanup()

    async def restart(self, request):
        self.calls.append((request.method, request.path, dict(request.query)))
        self.authorization = request.headers.get("Authorization")
        if self.disconnect:
            request.transport.close()
            return web.Response()
        return web.Response(
            status=self.status,
            text="private-cloud-token" if self.status != 200 else "",
            headers={"Location": "/not-a-restart"} if self.status == 302 else {},
        )

    async def action(self, arguments=None, confirmed=True):
        return await self.runtime.action("go2rtc", "restart_gateway", arguments or {}, confirmed)

    async def test_single_gateway_posts_once_and_invalidates_both_lenses(self):
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])  # Loading configuration never triggers a restart.
        self.plugin.providers["second_lens"].session.headers["Authorization"] = "Basic private-test"
        result = await self.action()
        self.assertEqual(self.calls, [("POST", "/api/restart", {})])
        self.assertEqual(self.authorization, "Basic private-test")
        self.assertIn("已提交", result["message"])
        self.assertIn("不代表", result["message"])
        for provider in self.plugin.providers.values():
            self.assertIsNone(provider.identity)
            self.assertNotEqual(provider.capture_generation, provider.generation)

    async def test_confirmation_and_schema_are_checked_before_dispatch(self):
        with self.assertRaises(ConfigurationError) as error:
            await self.action(confirmed=False)
        self.assertEqual(error.exception.code, "confirmation")
        for arguments in ({"url": self.base}, {"device_id": 123}, {"device_id": "camera"}):
            with self.assertRaises(ConfigurationError):
                await self.action(arguments)
        self.assertEqual(self.calls, [])

    async def test_multiple_gateways_each_receive_one_request_without_device_input(self):
        other_calls = []

        async def restart_other(request):
            other_calls.append((request.method, request.path, dict(request.query)))
            return web.Response()

        app = web.Application()
        app.router.add_post("/api/restart", restart_other)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        other = Go2RTCCamera("other", self.spec(f"http://127.0.0.1:{runner.addresses[0][1]}"))
        self.plugin.providers["other"] = other
        await other.start()
        other.identity = "previous-source"
        result = await self.action()
        self.assertIn("全部 2 个", result["message"])
        self.assertEqual(self.calls, [("POST", "/api/restart", {})])
        self.assertEqual(other_calls, self.calls)
        self.assertIsNone(other.identity)

    async def test_failed_gateway_does_not_prevent_other_gateways_from_restarting(self):
        other = Go2RTCCamera("other", self.spec("http://127.0.0.1:1"))
        await other.start()
        self.plugin.providers = {"other": other, **self.plugin.providers}
        result = await self.action()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("1/2", result["message"])
        self.assertIn("other", result["message"])
        self.assertIn("不会自动重试", result["message"])

    async def test_active_acquisition_recording_and_motion_block_restart(self):
        second = self.plugin.providers["second_lens"]
        await second.lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "正在"):
                await self.action()
        finally:
            second.lock.release()
        second.recording = True
        with self.assertRaisesRegex(ValueError, "正在"):
            await self.action()
        second.recording = False
        self.plugin.controllers["camera"] = SimpleNamespace(active=object(), pending_stops=0)
        with self.assertRaisesRegex(ValueError, "正在"):
            await self.action()
        self.assertEqual(self.calls, [])

    async def test_denial_redirect_and_missing_api_do_not_retry_or_expose_response(self):
        for status in (401, 302, 404, 500):
            self.status = status
            before = len(self.calls)
            with self.assertRaises(ConfigurationError) as error:
                await self.action()
            self.assertEqual(error.exception.code, "outcome_unknown")
            self.assertIn("不会自动重试", str(error.exception))
            self.assertNotIn("private-cloud-token", str(error.exception))
            self.assertEqual(len(self.calls), before + 1)

    async def test_disconnect_does_not_repeat_an_uncertain_restart(self):
        self.disconnect = True
        with self.assertRaisesRegex(ConfigurationError, "未获确认"):
            await self.action()
        self.assertEqual(len(self.calls), 1)

    async def test_disabled_plugin_and_empty_gateway_list_have_clear_errors(self):
        providers = self.plugin.providers
        self.plugin.providers = {}
        try:
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                await self.action()
        finally:
            self.plugin.providers = providers
        self.runtime.host.runtime.plugins.clear()
        with self.assertRaises(ConfigurationError) as error:
            await self.action()
        self.assertEqual(error.exception.code, "disabled")
        self.assertEqual(self.calls, [])

    def test_restart_action_is_visible_on_the_plugin_configuration_page(self):
        inventory = catalog()
        pages = describe(inventory)
        plugin = next(item for item in inventory["plugins"] if item["id"] == "go2rtc")
        self.assertEqual(plugin["page"], "plugin:go2rtc")
        self.assertTrue(any(page["id"] == "plugin:go2rtc" for page in pages))
        operation = plugin["configuration"]["interface"]["actions"]["restart_gateway"]
        self.assertFalse(operation["read_only"])
        self.assertEqual(operation["input"]["properties"], {})
        self.assertIn("视频流", operation["confirmation"])
