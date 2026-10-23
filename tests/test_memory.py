import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant_runtime.api.contracts import MemoryRequest
from assistant_runtime.core.memory.service import MemoryPlugin
from assistant_runtime.app.store import Store


class FakeMemory:
    """Contract test backend: policy is independent of Mem0 and its vector store."""

    def __init__(self):
        self.records = {}
        self.fail_delete = False
        self.inference = AsyncMock(return_value=[])
        self.capture_calls = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def export(self):
        return copy.deepcopy(list(self.records.values()))

    async def read(self, ident):
        return copy.deepcopy(self.records.get(ident))

    async def upsert(self, record):
        self.records[record["id"]] = copy.deepcopy(record)

    async def delete(self, ident):
        if self.fail_delete:
            raise RuntimeError("offline")
        self.records.pop(ident, None)

    async def search(self, query, scope):
        return [r for r in await self.export() if query in r["text"] and r["scope"] == scope]

    async def capture(self, sources, scope, project, job_id):
        from assistant_runtime.core.memory.policy import validate_facts

        self.capture_calls += 1
        if scope == "project":
            return {"records": await self.export(), "deleted": []}
        candidates = await self.inference(sources, [])
        response = {
            "facts": [c["text"] for c in candidates],
            "provenance": [
                {"fact": c["text"], "source_id": c["source_id"], "evidence": c["evidence"]}
                for c in candidates
            ],
        }
        evidence = validate_facts(response, sources)
        for candidate in candidates:
            self.records[candidate["id"]] = {k: candidate[k] for k in ("id", "title", "text", "scope")}
            self.records[candidate["id"]].update(revision=1, updated=1, source=evidence[candidate["text"]])
        return {"records": await self.export(), "deleted": []}

    async def import_records(self, records):
        for r in records:
            await self.upsert(r)


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.store = Store(self.path)
        self.provider = FakeMemory()
        self.curator = self.provider.inference
        self.plugin = MemoryPlugin(
            SimpleNamespace(memory_auto_capture=True), self.store, self.provider, lambda: False
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def call(self, action, **kw):
        return await self.plugin.handle(MemoryRequest(action, kw))

    def batch(self, text="我喜欢乌龙茶，不加糖。"):
        self.store.transcripts.append("user", text)
        self.plugin.enqueue()
        return self.store.db.execute(
            "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def candidate(self):
        return {
            "id": "tea",
            "revision": 0,
            "scope": "user",
            "title": "饮品偏好",
            "text": "用户喜欢乌龙茶，不加糖。",
            "source_id": 1,
            "evidence": "我喜欢乌龙茶，不加糖。",
        }

    async def test_auto_capture_chinese_fact_idempotent_and_persistent(self):
        job = self.batch()
        self.curator.return_value = [self.candidate()]
        await self.plugin.process_job(job)
        await self.plugin.process_job(job)
        self.assertEqual(len(await self.provider.export()), 1)
        self.assertIn("乌龙茶", self.plugin.context_text())
        self.assertEqual((await self.provider.read("tea"))["revision"], 1)
        self.store.db.close()
        self.store = Store(self.path)
        p = MemoryPlugin(SimpleNamespace(memory_auto_capture=True), self.store, self.provider, lambda: False)
        await p.refresh()
        self.assertIn("乌龙茶", p.context_text())

    async def test_forget_prevents_old_batch_and_import_resurrection(self):
        job = self.batch()
        self.curator.return_value = [self.candidate()]
        await self.plugin.process_job(job)
        export = await self.call("export")
        await self.call("forget", id="tea")
        await self.plugin.process_job(job)
        await self.call("import", records=export["records"])
        self.assertEqual(await self.provider.export(), [])
        self.assertEqual(self.plugin.context_text(), "")

    async def test_failed_physical_delete_still_hidden_from_recall(self):
        await self.call("remember", id="tea", title="偏好", text="乌龙茶", scope="user")
        self.provider.fail_delete = True
        with self.assertRaises(RuntimeError):
            await self.call("forget", id="tea")
        self.assertEqual((await self.call("search", query="乌龙茶"))["records"], [])
        self.assertIsNone((await self.call("read", id="tea"))["record"])
        self.assertEqual(self.plugin.context_text(), "")

    async def test_correction_requires_revision_and_invalidates_queued_sources(self):
        await self.call("remember", id="tea", title="偏好", text="乌龙茶", scope="user")
        job = self.batch()
        with self.assertRaisesRegex(ValueError, "revision"):
            await self.call("remember", id="tea", title="偏好", text="绿茶", scope="user")
        await self.call("remember", id="tea", title="偏好", text="绿茶", scope="user", revision=1)
        self.curator.return_value = [self.candidate()]
        await self.plugin.process_job(job)
        self.assertEqual((await self.provider.read("tea"))["text"], "绿茶")

    async def test_model_guess_without_source_evidence_rejected(self):
        job = self.batch()
        c = self.candidate()
        c["evidence"] = "我住在北京"
        self.curator.return_value = [c]
        with self.assertRaisesRegex(ValueError, "evidence"):
            await self.plugin.process_job(job)
        self.assertEqual(await self.provider.export(), [])

    async def test_assistant_guess_is_not_user_evidence(self):
        self.store.transcripts.append("assistant", "我喜欢乌龙茶，不加糖。")
        self.plugin.enqueue()
        job = self.store.db.execute("SELECT id,start_id,end_id,epoch FROM memory_jobs").fetchone()
        self.curator.return_value = [self.candidate()]
        await self.plugin.process_job(job)
        self.curator.assert_not_awaited()
        self.assertEqual(await self.provider.export(), [])

    async def test_export_import_uses_provider_contract(self):
        await self.call("remember", id="tea", title="偏好", text="乌龙茶", scope="user")
        records = (await self.call("export"))["records"]
        second = FakeMemory()
        await second.import_records(records)
        self.assertEqual((await second.read("tea"))["text"], "乌龙茶")

    async def test_preempt_does_not_cancel_foreground_task(self):
        self.plugin.current = asyncio.create_task(asyncio.sleep(60))
        self.plugin.preempt()
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.current

    async def test_retry_uses_durable_source_claim_even_if_model_changes_id(self):
        job = self.batch()
        self.curator.return_value = [self.candidate()]
        await self.plugin.process_job(job)
        changed = self.candidate()
        changed["id"] = "different_model_generated_id"
        self.curator.return_value = [changed]
        await self.plugin.process_job(job)
        self.assertEqual(len(await self.provider.export()), 1)
