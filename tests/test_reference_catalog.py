"""Background metadata refresh preserves readable snapshots and never changes config."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from aiohttp import web
from aiohttp.test_utils import TestServer
from unittest.mock import patch

from assistant_runtime.core.models.capabilities import search_references
from assistant_runtime.core.models.reference_catalog import ReferenceCatalog, compile_catalog, validate_cache


def compiled(name="new-model"):
    return compile_catalog(
        json.dumps(
            {
                "lab": {
                    "models": {
                        name: {
                            "reasoning": True,
                            "tool_call": True,
                            "limit": {"context": 64000, "output": 4096},
                        }
                    }
                }
            }
        ).encode()
    )


class ReferenceCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = ReferenceCatalog(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.catalog.close)

    async def test_reads_are_immediate_and_concurrent_updates_are_coalesced(self):
        release = asyncio.Event()

        async def download():
            await release.wait()
            return compiled()

        self.catalog.download = AsyncMock(side_effect=download)
        original = self.catalog.read()
        for _ in range(20):
            self.assertIs(self.catalog.read(), original)
        self.assertTrue(self.catalog.status()["updating"])
        await asyncio.sleep(0)
        self.catalog.download.assert_awaited_once()
        self.assertFalse(search_references("new-model", catalog=original)["candidates"])
        release.set()
        await self.catalog.task
        self.assertEqual(search_references("new-model", catalog=self.catalog.read(update=False))["total"], 1)
        self.catalog.download.assert_awaited_once()
        restored = ReferenceCatalog(self.root)
        self.assertEqual(restored.data, self.catalog.data)
        await restored.close()

    async def test_failed_network_validation_and_save_keep_previous_data(self):
        for error in (OSError("offline"), ValueError("bad JSON")):
            self.catalog.download = AsyncMock(side_effect=error)
            previous = self.catalog.read()
            await self.catalog.task
            self.assertIs(self.catalog.read(update=False), previous)
            self.assertTrue(self.catalog.status()["error"])
        self.catalog.download = AsyncMock(return_value=compiled())
        with patch.object(self.catalog, "persist", side_effect=OSError("disk full")):
            previous = self.catalog.read()
            await self.catalog.task
        self.assertIs(self.catalog.data, previous)
        self.assertFalse(self.catalog.path.exists())
        self.catalog.read()
        await self.catalog.task
        self.assertIsNone(self.catalog.error)

    async def test_corrupt_cache_falls_back_and_close_cancels_background_fetch(self):
        self.catalog.path.parent.mkdir(parents=True)
        data = compiled()
        data["models"]["lab/new-model"] = "corrupt"
        self.catalog.path.write_text(json.dumps(data))
        restored = ReferenceCatalog(self.root)
        self.assertNotEqual(restored.data, data)
        self.assertTrue(restored.error)
        await restored.close()
        self.catalog.download = AsyncMock(side_effect=lambda: asyncio.sleep(20))

        # A pending network coroutine is canceled without replacing the snapshot.
        async def wait():
            await asyncio.sleep(20)

        self.catalog.download = AsyncMock(side_effect=wait)
        previous = self.catalog.read()
        await asyncio.sleep(0)
        await self.catalog.close()
        self.assertTrue(self.catalog.task.done())
        self.assertIs(self.catalog.read(), previous)

    async def test_real_http_metadata_only_and_error_preserves_disk(self):
        received = []

        async def serve(request):
            received.append((request.method, dict(request.headers)))
            return web.json_response({"lab": {"models": {"http-model": {"limit": {"context": 4096}}}}})

        app = web.Application()
        app.router.add_get("/api.json", serve)
        async with TestServer(app) as server:
            # Only the fixed download address is substituted by the local test server.
            with patch(
                "assistant_runtime.core.models.reference_catalog.URL", str(server.make_url("/api.json"))
            ):
                self.catalog.read()
                await self.catalog.task
                self.assertIsNone(self.catalog.error)
                self.assertIn("lab/http-model", self.catalog.data["models"])
                saved = self.catalog.path.read_bytes()
                self.catalog.download = AsyncMock(return_value={"models": {}})
                self.catalog.read()
                await self.catalog.task
                self.assertEqual(self.catalog.path.read_bytes(), saved)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "GET")
        self.assertNotIn("Authorization", received[0][1])


class CompilerValidationTest(unittest.TestCase):
    def test_invalid_cache_and_empty_upstream_are_rejected(self):
        for raw in (b"[]", b"{}", b'{"lab":{"models":{"m":false}}}'):
            with self.assertRaises(ValueError):
                compile_catalog(raw)
        data = compiled()
        data["retrieved_at"] = "2026-01-01"
        with self.assertRaises(ValueError):
            validate_cache(data)
