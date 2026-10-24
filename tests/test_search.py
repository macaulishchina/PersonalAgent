"""Independent HTTP search, lifecycle, privacy and MCP/IPC contracts; no external IO."""

from assistant_runtime.platforms.ipc import serve as serve_control

from assistant_runtime.app.bundled import context_for

from usage_fixture import attach_usage

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from fastmcp import Client
from fastmcp.exceptions import ToolError

from assistant_runtime.app.host import AssistantService
from assistant_runtime.app.config import Config
from assistant_runtime.api.contracts import SearchRequest, TaskRequest
from assistant_runtime.kernel.runtime import Manifest, Runtime
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.search.tools import create_server
from assistant_runtime.core.pi.mcp import server_config
from assistant_runtime.plugins.search.deepseek import query_payload, search_result
from assistant_runtime.plugins.search.plugin import SearchPlugin, credential
from assistant_runtime.plugins.search.settings import SearchSettings
from assistant_runtime.plugins.search.urls import SearchError, public_url, validate_fetch_url
from assistant_runtime.app.store import Store


class Calls:
    manifest = Manifest("tasks")

    def __init__(self):
        self.runs = {"run": {"state": "running"}}

    async def start(self, context):
        async def request(value):
            return self.runs

        context.register(TaskRequest, request)


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "test.db")
        self.addCleanup(self.store.db.close)
        self.seen = []
        self.statuses = []
        self.entered = asyncio.Event()
        self.release = None
        self.body = {
            "results": [
                {
                    "title": "Python reference",
                    "url": "https://docs.python.org/3/",
                    "summary": "Reference",
                    "snippet": "Brief",
                }
            ]
        }

        async def handle(request):
            self.seen.append(
                {
                    "action": request.match_info["action"],
                    "payload": await request.json(),
                    "auth": request.headers.get("Authorization"),
                    "at": time.monotonic(),
                }
            )
            self.entered.set()
            if self.release:
                await self.release.wait()
            status = self.statuses.pop(0) if self.statuses else 200
            if status != 200:
                return web.Response(
                    status=status,
                    text="PRIVATE upstream detail",
                    headers={"Retry-After": "0.01", "Location": self.endpoint + "/must-not-follow"},
                )
            return web.json_response(self.body, headers={"x-request-id": "provider-test-1"})

        app = web.Application()
        app.router.add_post("/v1/tools/{action}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.runtime = Runtime(self.store)
        self.calls = Calls()
        self.runtime.add(self.calls)
        self.settings = {
            "default_provider": "ds",
            "requests_per_second": 100,
            "providers": {"ds": {"base_url": self.endpoint, "api_key_env": "SEARCH_TEST_KEY"}},
        }
        self.host = SimpleNamespace(
            config=SimpleNamespace(search=self.settings, root=self.root),
            store=self.store,
            runtime=self.runtime,
        )
        attach_usage(self.host, self.runtime)
        self.plugin = SearchPlugin(context_for(self.host, "search"))
        self.runtime.add(self.plugin)
        with patch.dict(os.environ, {"SEARCH_TEST_KEY": "LOCAL_TEST_SECRET"}):
            await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)

    async def call(self, action="search", run=None, **args):
        return await self.runtime.invoke(SearchRequest(action, args, run), timeout=3)

    async def test_real_http_mapping_cache_history_and_no_body_secrets_in_receipts(self):
        with self.assertLogs("assistant_runtime.plugins.search.plugin", level="INFO") as logs:
            first = await self.call(
                query="PRIVATE question",
                allowed_domains=["docs.python.org"],
                languages=["en"],
                after="2026-01-01",
            )
            again = await self.call(
                query="PRIVATE question",
                allowed_domains=["docs.python.org"],
                languages=["en"],
                after="2026-01-01",
            )
        self.assertEqual(first["state"], "completed")
        self.assertFalse(first["cached"])
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(
            self.seen[0]["payload"],
            {
                "query": "PRIVATE question site:docs.python.org lang:en after:2026-01-01",
                "count": 10,
                "need_content": False,
            },
        )
        self.assertEqual(self.seen[0]["auth"], "Bearer LOCAL_TEST_SECRET")
        history = await self.call("history")
        self.assertEqual(len(history["items"]), 2)
        public = json.dumps(history) + str(logs.output)
        for value in ("LOCAL_TEST_SECRET", "PRIVATE question", "Reference", "docs.python.org"):
            self.assertNotIn(value, public)
        self.assertIn("provider-test-1", public)
        self.assertNotEqual(first["request_id"], again["request_id"])

    async def test_retry_is_bounded_and_no_provider_fallback_or_error_body_leak(self):
        self.statuses = [429, 503]
        result = await self.call(query="retry")
        self.assertEqual(result["error"]["code"], "provider_unavailable")
        self.assertEqual(result["upstream_attempts"], 2)
        self.assertEqual(len(self.seen), 2)
        self.assertNotIn("PRIVATE", json.dumps(result))
        for status, code in [
            (401, "authentication_failed"),
            (402, "insufficient_balance"),
            (403, "provider_access_denied"),
            (302, "unexpected_provider_status"),
        ]:
            self.statuses = [status]
            previous = len(self.seen)
            result = await self.call(query=str(status))
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(len(self.seen), previous + 1)
        self.assertEqual(self.plugin.cache_bytes, 0)
        caps = await self.call("capabilities")
        self.assertFalse(caps["automatic_provider_fallback"])

    async def test_same_query_coalesces_and_one_cancel_does_not_kill_other_waiter(self):
        self.release = asyncio.Event()
        first = asyncio.create_task(self.call(query="coalesce"))
        await self.entered.wait()
        second = asyncio.create_task(self.call(query="coalesce"))
        await asyncio.sleep(0.03)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.release.set()
        result = await second
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["coalesced"])
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.plugin.pending, 0)
        self.assertFalse(self.plugin.flights)

    async def test_cancel_all_and_shutdown_release_owned_work_without_poisoning_plugin(self):
        self.release = asyncio.Event()
        task = asyncio.create_task(self.call(query="cancel"))
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin.flights)
        self.assertEqual(self.plugin.pending, 0)
        self.assertEqual(self.runtime.health()["search"]["state"], "ready")
        self.entered.clear()
        task = asyncio.create_task(self.call(query="shutdown"))
        await self.entered.wait()
        await self.plugin.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        with self.assertRaisesRegex(SearchError, "stopping"):
            await self.call(query="later")

    async def test_deadline_and_admission_and_rate_are_effective(self):
        from dataclasses import replace

        self.plugin.settings = replace(self.plugin.settings, timeout_seconds=1, concurrency=1, max_pending=1)
        self.release = asyncio.Event()
        task = asyncio.create_task(self.call(query="deadline"))
        await self.entered.wait()
        with self.assertRaisesRegex(SearchError, "busy"):
            await self.call(query="other")
        result = await task
        self.assertEqual(result["error"]["code"], "search_timeout")
        self.assertEqual(result["upstream_attempts"], 1)
        self.assertEqual(self.plugin.pending, 0)
        self.assertFalse(self.plugin.flights)
        self.release.set()
        self.release = None
        self.plugin.providers["ds"].interval = 0.08
        self.statuses = [500, 200]
        await self.call(query="rate")
        self.assertGreaterEqual(self.seen[-1]["at"] - self.seen[-2]["at"], 0.07)

    async def test_empty_and_filtered_results_are_distinct_and_refresh_replaces_old_data(self):
        self.body = {"results": []}
        empty = await self.call(query="empty")
        self.assertTrue(empty["empty"])
        self.assertEqual(empty["upstream_attempts"], 1)
        self.body = {"results": [{"url": "http://127.0.0.1/private", "title": "local"}]}
        filtered = await self.call(query="empty", refresh=True)
        self.assertFalse(filtered["empty"])
        self.assertEqual(filtered["rejected_results"], 1)
        self.assertFalse(filtered["results"])
        self.statuses = [402]
        await self.call(query="empty", refresh=True)
        self.assertFalse(self.plugin.cache)

    async def test_fetch_dns_guard_content_limits_and_response_limits(self):
        self.body = {"title": "Page", "content": "x" * 21000}
        with patch(
            "assistant_runtime.plugins.search.plugin.validate_fetch_url",
            new=AsyncMock(return_value="https://example.com/"),
        ):
            result = await self.call("fetch", url="https://example.com/")
        self.assertEqual(len(result["content"]), 20000)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["final_url"])
        before = len(self.seen)
        bad = await self.call("fetch", url="http://127.0.0.1/private")
        self.assertEqual(bad["state"], "failed")
        self.assertEqual(len(self.seen), before)
        self.body = {"results": [], "extra": "x" * (2 * 1024 * 1024)}
        result = await self.call(query="oversized")
        self.assertEqual(result["error"]["code"], "search_response_too_large")
        self.body = {"title": "Empty", "content": ""}
        with patch("assistant_runtime.plugins.search.plugin.validate_fetch_url", new=AsyncMock()):
            result = await self.call("fetch", url="https://example.com/", refresh=True)
        self.assertEqual(result["error"]["code"], "page_content_unavailable")

    async def test_caller_validation_applies_to_cache_and_late_results(self):
        await self.call(query="owned", run="run")
        self.calls.runs["run"]["state"] = "completed"
        result = await self.call(query="owned", run="run")
        self.assertEqual(result["error"]["code"], "inactive_search_caller")
        self.assertNotIn("results", result)
        self.calls.runs["run"]["state"] = "running"
        self.entered.clear()
        self.release = asyncio.Event()
        task = asyncio.create_task(self.call(query="late", run="run"))
        await self.entered.wait()
        self.calls.runs["run"]["state"] = "completed"
        self.release.set()
        result = await task
        self.assertEqual(result["error"]["code"], "inactive_search_caller")
        self.assertNotIn("results", result)

    async def test_mcp_exposes_only_configured_read_tools_and_bound_caller(self):
        async with Client(create_server(self.runtime, "run")) as client:
            tools = {t.name: t for t in await client.list_tools()}
            self.assertEqual(set(tools), {"search_capabilities", "search_web", "fetch_webpage"})
            self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
            result = (await client.call_tool("search_web", {"query": "public"})).structured_content
            self.assertEqual(result["state"], "completed")
            with self.assertRaises(ToolError):
                await client.call_tool("search_web", {"query": "x", "count": 51})
            with self.assertRaises(ToolError):
                await client.call_tool(
                    "search_web", {"query": "x", "caller_run": "forged", "base_url": "https://evil.test"}
                )
            history = await self.call("history", limit=1)
            self.assertEqual(history["items"][0]["caller"], "run")
            self.assertEqual(history["items"][0]["provider"], "ds")
        settings = server_config(
            "/tmp/test",
            task_run="run",
            plugins=["memory", "devices", "ai_usage"] + (["search"] if True else []),
        )
        self.assertIn("search", settings)
        self.assertIn("run", settings["search"]["args"])
        self.assertNotIn("search", server_config("/tmp/test"))
        self.assertFalse(
            server_config(
                "/tmp/test",
                enabled=False,
                plugins=["memory", "devices", "ai_usage"] + (["search"] if True else []),
            )["search"]["enabled"]
        )

    async def test_expired_cache_credentials_and_malformed_unicode(self):
        first = await self.call(query="expires")
        key = next(iter(self.plugin.cache))
        created, result, size = self.plugin.cache[key]
        self.plugin.cache[key] = (created - 61, result, size)
        second = await self.call(query="expires")
        self.assertFalse(second["cached"])
        self.assertNotEqual(first["upstream_request_id"], second["upstream_request_id"])
        self.assertEqual(len(self.seen), 2)
        for key, code in (("", "credentials_missing"), ("bad\nkey", "credentials_invalid")):
            self.plugin.providers["ds"].key = key
            result = await self.call(query="no credential")
            self.assertEqual(result["error"]["code"], code)
        self.assertEqual(len(self.seen), 2)
        self.plugin.providers["ds"].key = "LOCAL_TEST_SECRET"
        self.body["results"][0]["title"] = "\ud800"
        result = await self.call(query="bad response")
        self.assertEqual(result["error"]["code"], "invalid_search_response")
        result = await self.call(query="\ud800")
        self.assertEqual(result["error"]["code"], "invalid_search_query_or_count")
        self.assertEqual(len(self.seen), 3)
        self.assertEqual(self.runtime.health()["search"]["state"], "ready")

    async def test_ipc_dispatch_and_disconnect_cancel_search_without_other_effects(self):
        socket_path = self.root / "capability.sock"
        from assistant_runtime.api.lifecycle import WorkIntake

        service = SimpleNamespace(runtime=self.runtime, intake=WorkIntake())
        service._operation_for_client = lambda reader, operation: AssistantService._operation_for_client(
            service, reader, operation
        )

        async def connection(reader, writer):
            await AssistantService._connection(service, reader, writer)

        server = await serve_control(socket_path, connection)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        client = LocalRuntimeClient(socket_path)
        result = await client.invoke(SearchRequest("search", {"query": "wire"}, "run"))
        self.assertEqual(result["state"], "completed")
        self.entered.clear()
        self.release = asyncio.Event()
        task = asyncio.create_task(client.invoke(SearchRequest("search", {"query": "disconnect"})))
        await self.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not self.plugin.pending:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.plugin.pending, 0)
        self.release.set()
        server.close()

    async def test_storage_cache_bounds_and_plugin_disabled_does_not_touch_runtime(self):
        for n in range(135):
            self.plugin.remember(str(n), {"state": "completed", "results": []})
        self.assertEqual(len(self.plugin.cache), 128)
        for n in range(40):
            self.plugin.remember("big" + str(n), {"state": "completed", "content": "x" * 600000})
        self.assertLessEqual(self.plugin.cache_bytes, 16 * 1024 * 1024)
        with self.store.db:
            self.store.db.executemany(
                "INSERT INTO search_calls(data) VALUES(?)",
                [(json.dumps({"request_id": str(n)}),) for n in range(1005)],
            )
        self.plugin.record({"request_id": "new", "state": "completed"})
        history = await self.call("history", limit=100)
        self.assertEqual(len(history["items"]), 100)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM search_calls").fetchone()[0], 1000)
        other = Runtime(self.store)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await other.invoke(SearchRequest("capabilities"))
        self.assertEqual(self.runtime.health()["tasks"]["state"], "ready")


class ValidationTest(unittest.IsolatedAsyncioTestCase):
    def test_private_dotenv_credentials_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            root = Path(d)
            self.assertEqual(credential(root, "SEARCH_TEST_KEY"), "")
            path = root / ".env"
            path.write_text("SEARCH_TEST_KEY=file-value\nOTHER_KEY=other-private-key\n")
            self.assertEqual(credential(root, "SEARCH_TEST_KEY"), "file-value")
            self.assertNotIn("SEARCH_TEST_KEY", os.environ)
            with patch.dict(os.environ, {"SEARCH_TEST_KEY": "environment-value"}):
                self.assertEqual(credential(root, "SEARCH_TEST_KEY"), "environment-value")
            path.write_text("SEARCH_TEST_KEY=${OTHER_KEY}\nOTHER_KEY=must-not-expand\n")
            self.assertEqual(credential(root, "SEARCH_TEST_KEY"), "${OTHER_KEY}")
            path.write_text("x" * 65537)
            with self.assertRaisesRegex(SearchError, "credential_file_too_large"):
                credential(root, "SEARCH_TEST_KEY")

    def test_settings_filters_and_invalid_payloads_fail_explicitly(self):
        values = {"default_provider": "deepseek", "providers": {"deepseek": {}}}
        cfg = SearchSettings.parse(values)
        self.assertEqual(cfg.providers["deepseek"]["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(cfg.requests_per_second, 5)
        for bad in [
            dict(values, default_provider="missing"),
            dict(values, concurrency=True),
            dict(values, api_key="SECRET"),
            {"providers": {"a": {"adapter": "unknown"}}},
        ]:
            with self.assertRaises(ValueError):
                SearchSettings.parse(bad)
        for bad in [
            {"query": "x", "languages": ["bad injected"]},
            {"query": "x", "after": "yesterday"},
            {"query": "x", "count": False},
            {"query": "x", "allowed_domains": ["https://example.com"]},
            {"query": "x" * 128, "languages": ["zh"]},
            {"query": "x", "after": "2026-02-01", "before": "2026-01-01"},
        ]:
            with self.assertRaises(SearchError):
                query_payload(bad)
        result = query_payload(
            {
                "query": '"query"',
                "allowed_domains": ["example.com", "python.org"],
                "blocked_domains": ["bad.com"],
                "languages": ["zh", "en"],
            }
        )
        self.assertIn("site:example.com OR site:python.org", result["query"])
        self.assertIn("-site:bad.com", result["query"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "config.toml").write_text('root="."\nprofile="headless"\ninclude=["search.toml"]\n')
            (p / "search.toml").write_text(
                '[search]\ndefault_provider="ds"\n[search.providers.ds]\nadapter="deepseek"\n'
            )
            self.assertIn("ds", Config.load(p / "config.toml").search["providers"])

    async def test_public_url_and_dns_rebinding_boundary(self):
        for url in [
            "file:///etc/passwd",
            "http://localhost/a",
            "https://127.0.0.1/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://10.1.2.3/",
            "http://169.254.169.254/",
            "https://x.local/",
            "https://user:pass@example.com/",
            "https://example.com/\nsecret",
            "https://example.com/\ud800",
            "http://224.0.0.1/",
        ]:
            with self.subTest(url=url), self.assertRaises(SearchError):
                public_url(url)
        self.assertEqual(public_url("https://example.com#fragment"), "https://example.com/")
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "getaddrinfo",
            new=AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]),
        ):
            with self.assertRaisesRegex(SearchError, "non_public"):
                await validate_fetch_url("https://public.example.com/")
        with patch.object(
            loop,
            "getaddrinfo",
            new=AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]),
        ):
            self.assertEqual(await validate_fetch_url("https://example.com/"), "https://example.com/")
        with self.assertRaisesRegex(SearchError, "invalid_search_response"):
            search_result({"results": [{"url": "https://example.com/", "title": 123}]}, 10)


if __name__ == "__main__":
    unittest.main()
