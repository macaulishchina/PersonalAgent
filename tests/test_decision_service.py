"""Decision policy, concurrent admission and archive/adoption evidence."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.kernel.contracts import DecisionOutcome, DecisionQuery
from assistant_runtime.plugins.decisions.settings import DecisionSettings
from assistant_runtime.plugins.decisions.audit import append, page
import test_decisions as fixtures
from test_decisions import sample


class PolicyTest(unittest.TestCase):
    def test_global_defaults_and_explicit_scene_overrides(self):
        s = DecisionSettings.parse(
            {
                "default_threshold": 0.6,
                "thresholds": {"model_review": 0.2},
                "scenes": {"voice_intent": {"mode": "shadow", "timeout_seconds": 0.5}},
            }
        )
        self.assertEqual(s.policy("model_review")["threshold"], 0.2)
        self.assertEqual(s.policy("event_relevance")["threshold"], 0.6)
        self.assertEqual(s.policy("voice_intent")["timeout_seconds"], 0.5)
        self.assertEqual(s.policy("voice_intent")["mode"], "shadow")
        for settings in (
            {"default_threshold": True},
            {"concurrency": 1},
            {"scenes": {"bad": {}}},
            {"scenes": {"selection": {"mode": "execute"}}},
        ):
            with self.assertRaises(ValueError):
                DecisionSettings.parse(settings)

    def test_daily_archives_are_private_paged_and_do_not_delete_previous_dates(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            for day in ("2026-10-20", "2026-10-21"):
                for n in range(3):
                    append(
                        path,
                        "UTC",
                        {
                            "at": datetime.fromisoformat(day + "T12:00:00+00:00").timestamp(),
                            "kind": "voice_intent",
                            "decision_id": str(n),
                            "status": "ok",
                        },
                    )
            first = page(path, "2026-10-20", limit=2)
            second = page(path, "2026-10-20", offset=first["next_offset"])
            self.assertEqual([e["decision_id"] for e in first["items"] + second["items"]], ["0", "1", "2"])
            self.assertEqual(len(page(path, "2026-10-21", decision_id="1")["items"]), 1)
            self.assertEqual((path / "archive/2026-10-20.jsonl").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                page(path, "../../private")

    def test_torn_archive_tail_cannot_swallow_next_complete_record(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            entry = {
                "at": datetime.fromisoformat("2026-10-21T12:00:00+00:00").timestamp(),
                "decision_id": "one",
            }
            append(path, "UTC", entry)
            archive = path / "archive/2026-10-21.jsonl"
            with archive.open("ab") as out:
                out.write(b'null\n42\n{"incomplete":')
            append(path, "UTC", {**entry, "decision_id": "two"})
            result = page(path, "2026-10-21")
            self.assertEqual([e["decision_id"] for e in result["items"]], ["one", "two"])


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ShadowTest.asyncSetUp
    asyncTearDown = fixtures.ShadowTest.asyncTearDown

    def query(self, kind, reference):
        state = (
            sample().state
            if kind == "voice_intent"
            else {"question": "Which option?", "context": {}, "options": {"one": "First"}}
        )
        return self.runtime.invoke(DecisionQuery(kind, reference, state, summary="Bounded summary"))

    async def test_background_saturation_preserves_interactive_worker(self):
        self.plugin.settings.mode = "active"
        self.provider.release.clear()
        work = [asyncio.create_task(self.query("selection", str(n))) for n in range(4)]
        for _ in range(100):
            if len(self.provider.calls) >= 3:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.provider.calls), 3)
        voice = asyncio.create_task(self.query("voice_intent", "voice"))
        for _ in range(100):
            if len(self.provider.calls) == 4:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(len(self.provider.calls), 4)
        self.assertEqual(self.provider.calls[-1]["kind"], "voice_intent")
        self.provider.release.set()
        await asyncio.gather(*work, voice)
        self.assertEqual(len(self.provider.calls), 5)

    async def test_each_cached_call_has_own_adoption_but_one_provider_request(self):
        self.plugin.settings.mode = "active"
        first, second = await asyncio.gather(
            self.query("voice_intent", "same"), self.query("voice_intent", "same")
        )
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        await self.runtime.invoke(DecisionOutcome(first["decision_id"], "adopted", "local_stop", "turn-one"))
        await self.runtime.invoke(
            DecisionOutcome(second["decision_id"], "rejected", "stale_input", "turn-two")
        )
        await self.plugin.audit_queue.join()
        entries = [
            json.loads(line)
            for path in (self.root / "state/decisions/archive").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        outcomes = [e for e in entries if e["stage"] == "adoption"]
        self.assertEqual({e["status"] for e in outcomes}, {"adopted", "rejected"})
        self.assertEqual(len(self.provider.calls), 1)
        self.assertFalse(any("input" in e for e in entries))

    async def test_live_caller_joins_running_observation_despite_duplicate_sample(self):
        self.plugin.settings.mode = "active"
        self.provider.release.clear()
        self.plugin.enqueue(sample("shared-observation"), "assistant")
        await asyncio.wait_for(self.provider.entered.wait(), 1)
        query = asyncio.create_task(self.query("voice_intent", "shared-observation"))
        try:
            for _ in range(100):
                if self.plugin.waiters or query.done():
                    break
                await asyncio.sleep(0.005)
            self.assertFalse(query.done())
            self.plugin.enqueue(sample("shared-observation"), "assistant")
            for _ in range(100):
                await asyncio.sleep(0.005)
                if self.plugin.queue.empty() and self.plugin.working == 1:
                    break
            self.assertFalse(query.done())
        finally:
            self.provider.release.set()
            result = await query
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_scene_shadow_records_without_releasing_an_action_choice(self):
        self.plugin.settings.mode = "active"
        self.plugin.settings.scenes["voice_intent"] = {"mode": "shadow"}
        result = await self.query("voice_intent", "shadow")
        self.assertEqual(result["reason"], "shadow_mode")
        self.assertIsNone(result["choice"])
        await self.plugin.queue.join()
        self.assertEqual(len(self.provider.calls), 1)

    async def test_archive_failure_does_not_break_decision(self):
        self.plugin.settings.mode = "active"
        with patch("assistant_runtime.plugins.decisions.audit.append", side_effect=OSError("full")):
            result = await self.query("voice_intent", "storage")
            await self.plugin.audit_queue.join()
        self.assertEqual(result["status"], "ok")
        self.assertGreater(self.plugin.audit_dropped, 0)
        self.assertEqual(self.plugin.audit_error, "archive_unavailable")

    async def test_sensitive_summaries_are_neither_logged_nor_sent(self):
        self.plugin.settings.mode = "active"
        result = await self.runtime.invoke(
            DecisionQuery("voice_intent", "private", sample().state, summary="Bearer abcdefghijklmnop")
        )
        self.assertEqual(result["reason"], "invalid_request")
        await self.plugin.audit_queue.join()
        raw = b"".join(p.read_bytes() for p in (self.root / "state/decisions/archive").glob("*.jsonl"))
        self.assertNotIn(b"abcdefghijklmnop", raw)
        self.assertFalse(self.provider.calls)

    async def test_shutdown_drains_requested_and_result_archive(self):
        self.plugin.settings.mode = "active"
        result = await self.query("voice_intent", "shutdown")
        await self.runtime.close()
        raw = "".join(p.read_text() for p in (self.root / "state/decisions/archive").glob("*.jsonl"))
        self.assertIn(result["decision_id"], raw)
        self.assertIn('"stage": "result"', raw)
