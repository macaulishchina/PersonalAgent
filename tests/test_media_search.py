"""Public discovery evidence, bounded query fan-out and reusable read observations."""

import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from fastmcp import Client

from assistant_runtime.mcp.media import create_server
from assistant_runtime.plugins.media.errors import MediaError
from assistant_runtime.plugins.media.search import PanSou, title_match
from assistant_runtime.plugins.media.search_candidates import public_link
from assistant_runtime.plugins.media.search_history import SearchHistory


class DiscoveryTest(unittest.IsolatedAsyncioTestCase):
    async def adapter(self, search):
        app = web.Application()
        app.router.add_post("/api/search", search)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        session = aiohttp.ClientSession()
        self.addAsyncCleanup(session.close)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        return PanSou(
            session, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", SearchHistory(db)
        )

    async def test_alias_merge_platform_identity_password_conflict_and_partial_failure(self):
        async def search(request):
            query = (await request.json())["kw"]
            if query == "failed":
                return web.Response(status=503)
            return web.json_response(
                {
                    "code": 0,
                    "data": {
                        "results": [
                            {
                                "title": query,
                                "channel": query,
                                "links": [
                                    {
                                        "type": "quark",
                                        "url": "https://www.aliyundrive.com/s/shared123",
                                        "password": "1111" if query == "中文名" else "2222",
                                    },
                                    {"type": "aliyun", "url": "https://pan.quark.cn/s/shared123"},
                                    {"url": "https://evil.test/s/shared123"},
                                ],
                            }
                        ]
                    },
                }
            )

        adapter = await self.adapter(search)
        result = await adapter.search("中文名", aliases=["Original Name", "failed"])
        self.assertEqual(len(result["items"]), 2)
        aliyun, quark = result["items"]
        self.assertNotEqual(aliyun["candidate_id"], quark["candidate_id"])
        self.assertTrue(aliyun["password_conflict"])
        self.assertEqual(aliyun["password"], "")
        self.assertCountEqual(aliyun["sources"], ["中文名", "Original Name"])
        self.assertCountEqual(aliyun["matched_queries"], ["中文名", "Original Name"])
        self.assertFalse(quark["acquisition"]["transfer"])
        self.assertEqual(quark["acquisition"]["blocker"], "account_not_configured")
        self.assertFalse(result["complete"])
        self.assertEqual(result["diagnostics"][2]["state"], "failed")
        self.assertTrue(all(q["source_completion"] == "unknown" for q in result["diagnostics"]))
        self.assertIn("report_failed_queries_separately", result["next_actions"])
        self.assertEqual(len(adapter.history.list()["items"]), 1)
        with self.assertRaisesRegex(ValueError, "query changed"):
            await adapter.search("中文名", search_id=result["search_id"])
        page = await adapter.search(
            "中文名", aliases=["Original Name", "failed"], search_id=result["search_id"]
        )
        self.assertEqual(page["items"], result["items"])

    async def test_alias_fanout_is_bounded_and_cancelled_without_orphans(self):
        adapter = PanSou(None, "http://unused")
        active = peak = 0
        entered = asyncio.Event()

        async def fetch(query, platforms):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1

        adapter.fetch = fetch
        task = asyncio.create_task(adapter.search("one", aliases=["two", "three", "four"]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertEqual(adapter.pending, 0)

    async def test_discovery_only_and_empty_results_have_different_next_steps(self):
        rows = [{"title": "title", "links": [{"url": "https://pan.quark.cn/s/shared123"}]}]

        async def search(request):
            return web.json_response({"code": 0, "data": {"results": rows}})

        adapter = await self.adapter(search)
        result = await adapter.search("title")
        self.assertEqual(result["outcome"], "discovery_only_candidates")
        self.assertIn("report_provider_not_connected", result["next_actions"])
        rows.clear()
        empty = await adapter.search("title", refresh=True)
        self.assertEqual(empty["outcome"], "no_candidates_in_observed_results")
        self.assertFalse(empty["complete"])
        adapter.fetch = AsyncMock(side_effect=MediaError("search_unavailable"))
        with self.assertRaises(MediaError):
            await adapter.search("title", refresh=True)
        self.assertEqual(adapter.history.list()["items"][0]["outcome"], "search_failed")
        self.assertEqual(
            (await adapter.search("title", search_id=result["search_id"]))["items"], result["items"]
        )

    async def test_mcp_forwards_scope_without_caller_identity_override(self):
        local = SimpleNamespace(invoke=AsyncMock(return_value={}))
        async with Client(create_server(local, "run-id")) as client:
            await client.call_tool(
                "search_media", {"query": "title", "aliases": ["alias"], "platforms": ["quark"]}
            )
            request = local.invoke.await_args.args[0]
            self.assertEqual(request.caller_run, "run-id")
            self.assertEqual(request.arguments["aliases"], ["alias"])
            self.assertEqual(request.arguments["platforms"], ["quark"])
            await client.call_tool("media_search_history", {"before": 20, "limit": 5})
            self.assertEqual(local.invoke.await_args.args[0].action, "search_history")
            await client.call_tool("media_search_sources", {})
            self.assertEqual(local.invoke.await_args.args[0].action, "search_sources")
            await client.call_tool(
                "inspect_media_share", {"share_url": "https://www.alipan.com/s/public123", "refresh": True}
            )
            self.assertTrue(local.invoke.await_args.args[0].arguments["refresh"])

    def test_public_identity_and_parameter_validation(self):
        for url in [
            "https://pan.quark.cn.evil.test/s/shared123",
            "https://user@pan.quark.cn/s/shared123",
            "http://pan.quark.cn/s/shared123",
            "https://pan.quark.cn:9/s/shared123",
            "magnet:?xt=http://evil.test",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_link(url)
        raw = bytes.fromhex("12" * 20)
        a = public_link("magnet:?xt=urn:btih:" + raw.hex() + "&tr=http://private.test")
        b = public_link("magnet:?xt=urn:btih:" + base64.b32encode(raw).decode())
        self.assertEqual(a, b)
        self.assertNotIn("private", a[2])
        self.assertEqual(public_link("https://115.com/s/shared123#password=code")[3], "code")
        self.assertFalse(title_match("!!!", "anything"))


class SearchHistoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.history = SearchHistory(self.db)
        self.provider = SimpleNamespace(
            call=AsyncMock(return_value={"items": [{"file_id": "one"}], "marker": None})
        )
        self.values = {
            "account": "aliyun",
            "share_id": "shared123",
            "password": "",
            "parent_file_id": "root",
            "marker": "",
        }

    async def test_inspection_cache_scope_expiry_refresh_and_concurrent_reuse(self):
        results = await asyncio.gather(*(self.history.inspect(self.provider, self.values) for _ in range(3)))
        self.assertEqual(self.provider.call.await_count, 1)
        self.assertEqual([r["inspection"]["cached"] for r in results], [False, True, True])
        for field in ["password", "parent_file_id", "marker"]:
            await self.history.inspect(self.provider, {**self.values, field: "different"})
        self.assertEqual(self.provider.call.await_count, 4)
        await self.history.inspect(self.provider, self.values, refresh=True)
        self.assertEqual(self.provider.call.await_count, 5)
        self.db.execute("UPDATE media_share_checks SET observed=observed-121")
        self.assertTrue(self.history.observation("shared123", "")["expired"])
        await self.history.inspect(self.provider, self.values)
        self.assertEqual(self.provider.call.await_count, 6)
        self.assertFalse(self.history.observation("shared123", "")["content_identity_verified"])

    async def test_definitive_negative_cache_does_not_poison_other_password_or_auth(self):
        self.provider.call.side_effect = MediaError("share_password_invalid")
        for _ in range(2):
            with self.assertRaisesRegex(MediaError, "share_password_invalid"):
                await self.history.inspect(self.provider, self.values)
        self.assertEqual(self.provider.call.await_count, 1)
        self.provider.call.side_effect = None
        await self.history.inspect(self.provider, {**self.values, "password": "correct"})
        self.assertEqual(self.provider.call.await_count, 2)
        for code in ["auth_required", "provider_timeout"]:
            self.provider.call.side_effect = MediaError(code)
            for _ in range(2):
                with self.assertRaises(MediaError):
                    await self.history.inspect(self.provider, {**self.values, "share_id": code})
        self.assertEqual(self.provider.call.await_count, 6)
        self.assertIsNone(self.history.observation("auth_required", ""))

    async def test_refreshes_in_flight_coalesce_and_cancellation_does_not_save(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            await release.wait()
            return {"items": []}

        self.provider.call.side_effect = fetch
        first = asyncio.create_task(self.history.inspect(self.provider, self.values, True))
        await entered.wait()
        second = asyncio.create_task(self.history.inspect(self.provider, self.values, True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.provider.call.await_count, 1)
        entered.clear()
        release.clear()
        third = asyncio.create_task(self.history.inspect(self.provider, self.values, True))
        await entered.wait()
        third.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await third
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_share_checks").fetchone()[0], 1)

    async def test_storage_and_pages_bounded_restart_and_no_large_page_caching(self):
        for i in range(210):
            self.history.record({"queries": [str(i)]})
        page = self.history.list(limit=50)
        ids = [x["id"] for x in page["items"]]
        while page["before"]:
            page = self.history.list(before=page["before"], limit=50)
            ids.extend(x["id"] for x in page["items"])
        self.assertEqual(len(ids), 200)
        self.assertEqual(len(set(ids)), 200)
        for i in range(135):
            await self.history.inspect(self.provider, {**self.values, "share_id": str(i)})
        self.assertEqual(self.db.execute("SELECT count(*) FROM media_share_checks").fetchone()[0], 128)
        await self.history.inspect(self.provider, self.values)
        self.assertIsNotNone(self.history.observation("shared123", ""))
        self.provider.call.return_value = {"items": ["x" * 65536]}
        await self.history.inspect(self.provider, self.values, refresh=True)
        self.assertIsNone(self.history.observation("shared123", ""))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            db = sqlite3.connect(path)
            history = SearchHistory(db)
            history.record({"queries": ["preserved"]})
            db.close()
            db = sqlite3.connect(path)
            try:
                self.assertEqual(SearchHistory(db).list()["items"][0]["queries"], ["preserved"])
            finally:
                db.close()
        self.assertNotIn("correct", json.dumps(self.history.list()))


if __name__ == "__main__":
    unittest.main()
