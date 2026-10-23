"""Real local Mem0 engine with deterministic model transport; no cloud requests."""

import asyncio
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.core.memory.mem0 import Mem0Memory
from assistant_runtime.core.memory.policy import validate_facts


class ProvenanceTest(unittest.TestCase):
    def test_only_exact_user_evidence_accepted(self):
        sources = [{"id": 1, "time": 1, "role": "user", "text": "我喜欢乌龙茶。"}]
        response = {
            "facts": ["喜欢乌龙茶"],
            "provenance": [{"fact": "喜欢乌龙茶", "source_id": 1, "evidence": "喜欢乌龙茶"}],
        }
        self.assertEqual(validate_facts(response, sources)["喜欢乌龙茶"]["id"], 1)
        for role, source_id, quote in (
            ("assistant", 1, "喜欢乌龙茶"),
            ("verified", 1, "喜欢乌龙茶"),
            ("user", 2, "喜欢乌龙茶"),
            ("user", 1, "喜欢绿茶"),
        ):
            changed = copy.deepcopy(response)
            changed["provenance"][0].update(source_id=source_id, evidence=quote)
            with self.subTest(role=role, quote=quote), self.assertRaises(ValueError):
                validate_facts(changed, [{**sources[0], "role": role}])

    def test_unsupported_or_oversized_facts_rejected(self):
        source = [{"id": 1, "time": 1, "role": "user", "text": "乌龙茶"}]
        with self.assertRaises(ValueError):
            validate_facts({"facts": ["北京居民"], "provenance": []}, source)
        with self.assertRaises(ValueError):
            validate_facts({"facts": ["x"] * 13, "provenance": []}, source)


class LocalMem0Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cache = Path(__file__).resolve().parents[1] / "models/memory-embedding"
        if not cache.exists():
            self.tmp.cleanup()
            self.skipTest("Prepare the local memory embedding model for real-engine contract tests")
        self.source = {"id": 1, "role": "user", "time": time.time(), "text": "我喜欢乌龙茶。"}
        self.fact = "用户喜欢乌龙茶。"
        self.event = "ADD"
        self.model_calls = 0
        self.provider = Mem0Memory(self.root / "mem0", cache, self.complete, "/test/project")

    async def asyncTearDown(self):
        if hasattr(self, "provider"):
            await self.provider.close()
        self.tmp.cleanup()

    async def complete(self, messages, schema):
        self.model_calls += 1
        if "facts" in schema["properties"]:
            return {
                "facts": [self.fact],
                "provenance": [
                    {"fact": self.fact, "source_id": self.source["id"], "evidence": self.source["text"]}
                ],
            }
        return {"memory": [{"id": "0", "text": self.fact, "event": self.event, "old_memory": ""}]}

    async def test_native_add_repeat_update_receipt_and_restart(self):
        outcome = await self.provider.capture([self.source], "user", "/test/project", "first")
        self.assertEqual(len(outcome["records"]), 1)
        original = outcome["records"][0]
        self.event = "NONE"
        repeat = await self.provider.capture([self.source], "user", "/test/project", "second")
        self.assertEqual(len(repeat["records"]), 1)
        self.assertEqual(repeat["records"][0]["id"], original["id"])
        calls = self.model_calls
        await self.provider.close()
        replay = await self.provider.capture([self.source], "user", "/test/project", "second")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.model_calls, calls)
        self.source = {**self.source, "id": 2, "text": "更正，我现在喜欢绿茶。"}
        self.fact, self.event = "用户现在喜欢绿茶。", "UPDATE"
        changed = await self.provider.capture([self.source], "user", "/test/project", "correction")
        self.assertEqual(changed["records"][0]["id"], original["id"])
        self.assertEqual(changed["records"][0]["revision"], 2)
        self.assertIn("绿茶", (await self.provider.read(original["id"]))["text"])

    async def test_explicit_crud_and_scope_isolation(self):
        for ident, scope, project in (
            ("user", "user", ""),
            ("project", "project", "/test/project"),
            ("other", "project", "/other/project"),
        ):
            await self.provider.upsert(
                {
                    "id": ident,
                    "scope": scope,
                    "project": project,
                    "title": "偏好",
                    "text": "喜欢乌龙茶",
                    "revision": 1,
                    "updated": 1,
                    "source": {"kind": "test"},
                }
            )
        self.assertEqual([r["id"] for r in await self.provider.search("乌龙茶", "user")], ["user"])
        self.assertEqual([r["id"] for r in await self.provider.search("乌龙茶", "project")], ["project"])
        changed = {**await self.provider.read("user"), "text": "喜欢绿茶", "revision": 2}
        await self.provider.upsert(changed)
        await self.provider.close()
        self.assertEqual((await self.provider.read("user"))["text"], "喜欢绿茶")
        await self.provider.delete("user")
        self.assertIsNone(await self.provider.read("user"))
        self.assertEqual(len(await self.provider.export()), 2)

    async def test_cancellation_stops_owned_worker_before_next_operation(self):
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.sleep(60)

        self.provider.complete = blocked
        task = asyncio.create_task(self.provider.capture([self.source], "user", "/test/project", "blocked"))
        await asyncio.wait_for(entered.wait(), 20)
        pid = self.provider.process.pid
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(await self.provider.export(), [])

    async def test_missing_provenance_is_not_silent_success(self):
        self.provider.complete = AsyncMock(return_value={"facts": ["用户住在北京"], "provenance": []})
        with self.assertRaisesRegex(RuntimeError, "evidence"):
            await self.provider.capture([self.source], "user", "/test/project", "invalid")
        self.assertEqual(await self.provider.export(), [])


if __name__ == "__main__":
    unittest.main()
