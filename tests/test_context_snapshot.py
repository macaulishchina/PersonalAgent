"""Context snapshots through real Pi and a local synthetic provider."""

import json
import asyncio
import base64
import unittest
import uuid

import test_pi_skeleton as fixtures
import test_pi_waiting as waiting_fixtures
from assistant_runtime.api import ConversationRequest, FileRequest
from pi_gateway import sse_text_reply, sse_tool_call


class NativeContextTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PiSkeletonTest.asyncSetUp
    asyncTearDown = fixtures.PiSkeletonTest.asyncTearDown
    session = fixtures.PiSkeletonTest.session
    completed = fixtures.PiSkeletonTest.completed

    async def test_tool_history_is_not_reexecuted_in_independent_task(self):
        marker = self.root / "history.txt"
        self.gateway.script_fn = lambda step, body: (
            sse_tool_call("historic-write", "write", {"path": str(marker), "content": "original"})
            if step == 1
            else sse_text_reply("history-done", "WRITE-CONFIRMED")
        )
        source = await self.session()
        await self.client.request(
            "run.start", {"session_id": source, "input": [{"type": "text", "text": "Write synthetic marker"}]}
        )
        await self.completed()
        checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        exported = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": checkpoint}
        )
        self.assertTrue(any(m["role"] == "toolResult" for m in exported["content"]["messages"]))
        marker.write_text("changed after original operation")
        target = await self.session(model="model-b")
        await self.client.request(
            "session.seed",
            {"session_id": target, "snapshot_ref": "tool-history", "content": exported["content"]},
        )
        await self.client.request(
            "run.start", {"session_id": target, "input": [{"type": "text", "text": "Report only"}]}
        )
        await self.completed()
        self.assertEqual(marker.read_text(), "changed after original operation")
        self.assertEqual(len(self.gateway.requests), 3)

    async def test_native_compaction_projection_and_target_copy(self):
        source = await self.session()
        folder = self.client.workers[source].meta["agent_dir"]
        from pathlib import Path

        settings_path = Path(folder) / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["compaction"]["keepRecentTokens"] = 100
        settings_path.write_text(json.dumps(settings))
        for index in range(2):
            await self.client.request(
                "run.start",
                {"session_id": source, "input": [{"type": "text", "text": f"ROUND-{index} " * 100}]},
            )
            await self.completed()
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"reply-{step}", "COMPACTED-SOURCE")
        await self.client.workers[source].command({"type": "compact"})
        await self.client.request(
            "run.start",
            {"session_id": source, "input": [{"type": "text", "text": "Complete after compaction"}]},
        )
        await self.completed()
        checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        exported = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": checkpoint}
        )
        self.assertTrue(exported["content"]["compacted"])
        self.assertFalse(any(m["role"] == "system" for m in exported["content"]["messages"]))
        target = await self.session(model="model-b")
        await self.client.request(
            "session.seed",
            {"session_id": target, "snapshot_ref": "compacted", "content": exported["content"]},
        )
        await self.client.request(
            "run.start", {"session_id": target, "input": [{"type": "text", "text": "Independent check"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed")
        self.assertIn("COMPACTED-SOURCE", json.dumps(self.gateway.requests[-1]))
        self.assertEqual((await self.usage.snapshot(self.client.last_work_id))["requests"], 1)

    async def test_fixed_history_seeds_independent_model_without_inference(self):
        source = await self.session()
        await self.client.request(
            "run.start",
            {
                "session_id": source,
                "context_input_id": "source-input",
                "input": [{"type": "text", "text": "SOURCE-FACT"}],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed")
        checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        self.assertEqual(checkpoint["inputs"], ["source-input"])
        frozen = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": checkpoint}
        )
        before = len(self.gateway.requests)
        target = await self.session(model="model-b")
        await self.client.request(
            "session.seed",
            {
                "session_id": target,
                "snapshot_ref": "snapshot",
                "content": frozen["content"],
                "additional": "KFC-EXPLANATION",
            },
        )
        self.assertEqual(len(self.gateway.requests), before)
        self.gateway.script_fn = lambda step, body: sse_text_reply("target", "DONE")
        await self.client.request(
            "run.start", {"session_id": target, "input": [{"type": "text", "text": "NEW-REQUIREMENT"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed")
        request = json.dumps(self.gateway.requests[-1])
        self.assertLess(request.index("SOURCE-FACT"), request.index("KFC-EXPLANATION"))
        self.assertLess(request.index("KFC-EXPLANATION"), request.index("NEW-REQUIREMENT"))
        self.assertEqual(self.gateway.requests[-1]["model"], "model-b")
        self.assertEqual(await self.client.request("session.checkpoint", {"session_id": source}), checkpoint)


class PublicSnapshotTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = waiting_fixtures.TaskWaitingTest.asyncSetUp
    asyncTearDown = waiting_fixtures.TaskWaitingTest.asyncTearDown
    start = waiting_fixtures.TaskWaitingTest.start
    stop = waiting_fixtures.TaskWaitingTest.stop
    wait = waiting_fixtures.TaskWaitingTest.wait
    call = waiting_fixtures.TaskWaitingTest.call

    async def conversation(self, action, **args):
        return await self.host.runtime.invoke(ConversationRequest(action, args))

    async def source(self):
        self.gateway.script_fn = lambda step, body: sse_text_reply(f"reply{step}", "SOURCE-ANSWER")
        chat = await self.conversation("create", title="Snapshot source")
        request = await self.submit(chat, "SOURCE-FACT")
        return chat, request

    async def submit(self, chat, text):
        request = str(uuid.uuid4())
        await self.conversation("submit", session_id=chat["id"], request_id=request, text=text)
        async with asyncio.timeout(20):
            while (await self.conversation("receipt", session_id=chat["id"], request_id=request))[
                "state"
            ] != "completed":
                await asyncio.sleep(0.02)
        return request

    async def snapshot(self, chat, request=None):
        return await self.conversation(
            "snapshot", session_id=chat["id"], request_id=request or str(uuid.uuid4())
        )

    async def task(self, context=None, **changes):
        spec = {
            "title": "Snapshot task",
            "prompt": "NEW-REQUIREMENT",
            "execution": {"model": "model-b", "effort_mode": "model_default"},
            "tools": [],
            "notify_completion": False,
            "auto_archive": False,
            **changes,
        }
        if context is not None:
            spec["context"] = context
        return await self.call("create", request_id=str(uuid.uuid4()), spec=spec)

    async def test_public_receipt_coverage_frozen_task_and_restart(self):
        chat, source_request = await self.source()
        intent = str(uuid.uuid4())
        snapshot = await self.snapshot(chat, intent)
        self.assertEqual(snapshot["state"], "ready", snapshot)
        self.assertTrue(snapshot["coverage_known"])
        self.assertEqual(await self.snapshot(chat, intent), snapshot)
        self.assertEqual(
            (
                await self.conversation(
                    "snapshot_inputs", session_id=chat["id"], snapshot_ref=snapshot["snapshot_ref"]
                )
            )["request_ids"],
            [source_request],
        )
        await self.submit(chat, "LATER-SOURCE-FACT")
        background = "KFC-EXPLANATION\n" + "补充背景。" * 1200
        registered = await self.host.runtime.invoke(
            FileRequest(
                "put", {"name": "background.txt", "data": base64.b64encode(background.encode()).decode()}
            )
        )
        before = len(self.gateway.requests)
        task = await self.task(
            {"snapshot_ref": snapshot["snapshot_ref"], "additional_input": registered["id"]}
        )
        run = await self.wait(task, {"completed", "failed"})
        self.assertEqual(run["state"], "completed", run)
        self.assertTrue(run["data"]["context"]["seeded"])
        request = json.dumps(self.gateway.requests[-1], ensure_ascii=False)
        self.assertIn("SOURCE-FACT", request)
        self.assertNotIn("LATER-SOURCE-FACT", request)
        self.assertNotIn("本会话临时脚本", request)
        self.assertIn("只完成给定授权范围", request)
        self.assertLess(request.index("SOURCE-FACT"), request.index("KFC-EXPLANATION"))
        self.assertLess(request.index("KFC-EXPLANATION"), request.index("NEW-REQUIREMENT"))
        self.assertEqual(len(self.gateway.requests), before + 1)
        self.assertEqual(task["context_info"]["additional_input"]["sha256"], registered["sha256"])
        await self.stop()
        await self.start()
        self.assertEqual(
            await self.conversation("snapshot_receipt", session_id=chat["id"], request_id=intent), snapshot
        )
        self.assertEqual(len(self.gateway.requests), before + 1)

    async def test_empty_opt_in_validation_conflicts_and_ordinary_task(self):
        chat = await self.conversation("create", title="Empty")
        intent = str(uuid.uuid4())
        snapshot = await self.snapshot(chat, intent)
        self.assertEqual(snapshot["input_count"], 0)
        other = await self.conversation("create", title="Other")
        with self.assertRaisesRegex(ValueError, "conflict"):
            await self.snapshot(other, intent)
        missing = await self.conversation(
            "snapshot_receipt", session_id=chat["id"], request_id=str(uuid.uuid4())
        )
        self.assertEqual(missing["state"], "not_found")
        binary = await self.host.runtime.invoke(
            FileRequest("put", {"name": "binary", "data": base64.b64encode(b"\xff\x00").decode()})
        )
        with self.assertRaises(Exception):
            await self.task({"snapshot_ref": snapshot["snapshot_ref"], "additional_input": binary["id"]})
        self.gateway.script_fn = lambda step, body: sse_text_reply("ordinary", "ORDINARY")
        ordinary = await self.task()
        run = await self.wait(ordinary, {"completed", "failed"})
        self.assertEqual(run["state"], "completed")
        self.assertNotIn("context_info", ordinary)
        self.assertNotIn("context", run["data"])

    async def test_waiting_source_uses_previous_checkpoint_and_answer_coverage(self):
        chat, first = await self.source()
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            "source-question", "request_user_input", {"questions": [{"id": "q", "question": "Confirm?"}]}
        )
        second = str(uuid.uuid4())
        await self.conversation("submit", session_id=chat["id"], request_id=second, text="UNFINISHED-ROUND")
        async with asyncio.timeout(20):
            while True:
                state = await self.conversation("state", session_id=chat["id"])
                if state.get("question_id"):
                    break
                await asyncio.sleep(0.02)
        async with asyncio.timeout(3):
            frozen = await self.snapshot(chat)
        covered = await self.conversation(
            "snapshot_inputs", session_id=chat["id"], snapshot_ref=frozen["snapshot_ref"]
        )
        self.assertEqual(covered["request_ids"], [first])
        self.gateway.script_fn = lambda step, body: sse_text_reply("after-answer", "ANSWER-CONFIRMED")
        await self.conversation(
            "answer",
            session_id=chat["id"],
            request_id=second,
            question_id=state["question_id"],
            text="EXPLICIT-ANSWER",
        )
        async with asyncio.timeout(20):
            while (await self.conversation("receipt", session_id=chat["id"], request_id=second))[
                "state"
            ] != "completed":
                await asyncio.sleep(0.02)
        newer = await self.snapshot(chat)
        covered = await self.conversation(
            "snapshot_inputs", session_id=chat["id"], snapshot_ref=newer["snapshot_ref"], limit=1
        )
        self.assertEqual(covered["request_ids"], [first])
        last = await self.conversation(
            "snapshot_inputs",
            session_id=chat["id"],
            snapshot_ref=newer["snapshot_ref"],
            cursor=covered["next_cursor"],
        )
        self.assertEqual(last["request_ids"], [second])
        task = await self.task({"snapshot_ref": newer["snapshot_ref"]})
        self.assertEqual((await self.wait(task, {"completed", "failed"}))["state"], "completed")
        self.assertIn("EXPLICIT-ANSWER", json.dumps(self.gateway.requests[-1]))

    async def test_periodic_tasks_keep_frozen_history_and_holds(self):
        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        task = await self.task(
            {"snapshot_ref": snapshot["snapshot_ref"]}, trigger={"kind": "interval", "seconds": 3600}
        )
        self.assertEqual(len(self.gateway.requests), 1)
        first = await self.wait(task, {"completed", "failed"})
        self.assertEqual(first["state"], "completed")
        await self.submit(chat, "LATER-SOURCE-FACT")
        await self.call("run", task_id=task["id"], request_id=str(uuid.uuid4()))
        async with asyncio.timeout(20):
            while True:
                saved = await self.call("get", task_id=task["id"])
                second = next((r for r in saved["runs"] if r["id"] != first["id"]), None)
                if second and second["state"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(second["state"], "completed", second)
        self.assertNotEqual(first["data"]["execution_session_id"], second["data"]["execution_session_id"])
        self.assertNotIn("LATER-SOURCE-FACT", json.dumps(self.gateway.requests[-1]))
        snapshot_row = self.host.conversations.snapshots.row(ident=snapshot["snapshot_ref"])
        results = self.host.workfiles.results
        self.assertTrue(self.host.backend.task_contexts.protected(task["id"]))
        self.assertIsNotNone(
            results.db.execute(
                "SELECT 1 FROM result_holds WHERE id=? AND reason=?",
                (snapshot_row["result_id"], "task_context:" + task["id"]),
            ).fetchone()
        )
        with self.assertRaises(Exception):
            await self.call(
                "update",
                task_id=task["id"],
                revision=task["revision"],
                changes={"context": {"snapshot_ref": "different"}},
            )

    async def test_large_background_uses_existing_automatic_compaction(self):
        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        background = await self.host.runtime.invoke(
            FileRequest(
                "put",
                {
                    "name": "large.txt",
                    "data": base64.b64encode(("historical-background " * 14000).encode()).decode(),
                },
            )
        )
        task = await self.task(
            {"snapshot_ref": snapshot["snapshot_ref"], "additional_input": background["id"]}
        )
        run = await self.wait(task, {"completed", "failed"})
        self.assertEqual(run["state"], "completed", run)
        self.assertGreaterEqual(run["data"]["ai_usage"]["compactions"], 1)
        self.assertEqual(
            (await self.snapshot(chat, snapshot["request_id"]))["checkpoint"], snapshot["checkpoint"]
        )

    async def test_interrupted_snapshot_preparation_keeps_original_checkpoint(self):
        chat, _ = await self.source()
        manager = self.host.conversations.snapshots
        prepare = manager.prepare
        gate = asyncio.Event()

        async def paused(row):
            await gate.wait()
            await prepare(row)

        manager.prepare = paused
        intent = str(uuid.uuid4())
        pending = asyncio.create_task(self.snapshot(chat, intent))
        async with asyncio.timeout(5):
            while not manager.row(request=intent):
                await asyncio.sleep(0.01)
        receipt = await self.conversation("snapshot_receipt", session_id=chat["id"], request_id=intent)
        self.assertEqual(receipt["state"], "preparing")
        await self.stop()
        await asyncio.gather(pending, return_exceptions=True)
        await self.start()
        await self.submit(chat, "LATER-SOURCE-FACT")
        recovered = await self.snapshot(chat, intent)
        self.assertEqual(recovered["state"], "ready")
        self.assertEqual(recovered["checkpoint"], receipt["checkpoint"])
        task = await self.task({"snapshot_ref": recovered["snapshot_ref"]})
        self.assertEqual((await self.wait(task, {"completed", "failed"}))["state"], "completed")
        self.assertNotIn("LATER-SOURCE-FACT", json.dumps(self.gateway.requests[-1]))

    async def test_snapshot_permission_corruption_and_absent_capability(self):
        from assistant_runtime.api.ai_usage import UsageScope, current_usage
        from assistant_runtime.api.tool_calls import ToolFailure

        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        requests = len(self.gateway.requests)
        token = current_usage.set(UsageScope("foreign-work", "test"))
        try:
            with self.assertRaises(ToolFailure):
                await self.task({"snapshot_ref": snapshot["snapshot_ref"]})
            with self.assertRaises(ValueError):
                await self.snapshot(chat)
        finally:
            current_usage.reset(token)
        row = self.host.conversations.snapshots.row(ident=snapshot["snapshot_ref"])
        with self.host.store.db:
            self.host.store.db.execute(
                "UPDATE received_results SET version=? WHERE id=?", ("0" * 64, row["result_id"])
            )
        with self.assertRaises(Exception):
            await self.task({"snapshot_ref": snapshot["snapshot_ref"]})
        self.assertEqual(len(self.gateway.requests), requests)
        await self.stop()
        self.config.plugins = [p for p in self.config.plugins if p != "conversation"]
        self.config.notifications = {"channels": ["inbox"], "voice_enabled": False}
        await self.start()
        with self.assertRaises(ToolFailure):
            await self.task({"snapshot_ref": snapshot["snapshot_ref"]})
        ordinary = await self.task()
        self.assertEqual((await self.wait(ordinary, {"completed", "failed"}))["state"], "completed")

    async def test_waiting_task_pause_continue_keeps_own_context_once(self):
        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            "task-question", "request_user_input", {"questions": [{"id": "q", "question": "Confirm task?"}]}
        )
        task = await self.task({"snapshot_ref": snapshot["snapshot_ref"]}, tools=["request_user_input"])
        run = await self.wait(task, {"waiting_user", "failed"})
        self.assertEqual(run["state"], "waiting_user")
        stopped = await self.call("pause_run", run_id=run["id"])
        self.assertEqual(stopped["state"], "paused")
        self.gateway.script_fn = lambda step, body: sse_text_reply("continued", "CONTINUED")
        await self.call("continue", run_id=run["id"], text="Explicitly continue, report only")
        ended = await self.wait(task, {"completed", "failed"})
        self.assertEqual(ended["state"], "completed", ended)
        self.assertEqual(ended["id"], run["id"])
        self.assertEqual(ended["data"]["execution_session_id"], run["data"]["execution_session_id"])
        self.assertEqual(json.dumps(self.gateway.requests[-1]).count("SOURCE-FACT"), 1)

    async def test_expired_and_quota_failed_snapshot_receipts_do_not_recapture(self):
        import time

        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        results = self.host.workfiles.results
        row = self.host.conversations.snapshots.row(ident=snapshot["snapshot_ref"])
        results.days = 7
        with results.db:
            results.db.execute(
                "UPDATE received_results SET accessed=?,created=? WHERE id=?",
                (time.time() - 8 * 86400, time.time() - 8 * 86400, row["result_id"]),
            )
        await results.collect()
        expired = await self.snapshot(chat, snapshot["request_id"])
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["checkpoint"], snapshot["checkpoint"])
        with self.assertRaises(Exception):
            await self.task({"snapshot_ref": snapshot["snapshot_ref"]})
        results.maximum = 10
        failed = await self.snapshot(chat)
        self.assertEqual(failed["state"], "unavailable")
        self.assertEqual(failed["error"], "result_too_large")
        self.assertEqual(await self.snapshot(chat, failed["request_id"]), failed)
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_retained_tool_result_is_authorized_without_rereading_source(self):
        material = self.root / "source-material.txt"
        material.write_text("before " * 1300 + "HIDDEN-MIDDLE" + " after" * 2200)
        self.gateway.script_fn = lambda step, body: (
            sse_tool_call("source-read", "read", {"path": str(material)})
            if step == 1
            else sse_text_reply("source-read-done", "SOURCE-READ-DONE")
        )
        chat = await self.conversation("create", title="Retained context")
        await self.submit(chat, "Read synthetic material once")
        snapshot = await self.snapshot(chat)
        row = self.host.conversations.snapshots.row(ident=snapshot["snapshot_ref"])
        dependencies = json.loads(row["dependencies"])
        self.assertTrue(dependencies)
        material.write_text("The original source is now different")
        self.gateway.script_fn = lambda step, body: (
            sse_tool_call(
                "target-result-read", "read_result", {"id": dependencies[0], "offset": 8500, "length": 1500}
            )
            if step == 3
            else sse_text_reply("target-read-done", "RESULT-READ")
        )
        task = await self.task({"snapshot_ref": snapshot["snapshot_ref"]}, tools=["read_result"])
        run = await self.wait(task, {"completed", "failed"})
        self.assertEqual(run["state"], "completed", run)
        self.assertIn("HIDDEN-MIDDLE", json.dumps(self.gateway.requests[-1]))
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_long_term_task_uses_independent_goal_with_snapshot(self):
        chat, _ = await self.source()
        snapshot = await self.snapshot(chat)
        self.gateway.script_fn = lambda step, body: (
            sse_tool_call(
                "goal-complete",
                "update_task_goal",
                {"status": "complete", "summary": "Synthetic objective verified"},
            )
            if step == 2
            else sse_text_reply("goal-final", "GOAL-DONE")
        )
        task = await self.task({"snapshot_ref": snapshot["snapshot_ref"]}, long_term=True, tools=None)
        run = await self.wait(task, {"completed", "failed"})
        self.assertEqual(run["state"], "completed", run)
        self.assertEqual(run["data"]["goal"]["status"], "complete")
        self.assertTrue(run["data"]["context"]["seeded"])
        self.assertIn("SOURCE-FACT", json.dumps(self.gateway.requests[-1]))
        self.assertEqual(len(self.gateway.requests), 3)

    async def test_active_and_cancelled_first_round_have_provably_empty_baseline(self):
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            "first-question",
            "request_user_input",
            {"questions": [{"id": "q", "question": "Confirm first round?"}]},
        )
        chat = await self.conversation("create", title="Incomplete first round")
        request = str(uuid.uuid4())
        await self.conversation(
            "submit", session_id=chat["id"], request_id=request, text="FIRST-UNFINISHED-INPUT"
        )
        async with asyncio.timeout(20):
            while not (await self.conversation("state", session_id=chat["id"]))["question_id"]:
                await asyncio.sleep(0.02)
        before = await self.snapshot(chat)
        self.assertEqual(before["input_count"], 0)
        self.assertTrue(before["coverage_known"])
        backend = (await self.host.conversations.get(chat["id"])).backend
        self.assertNotIn(backend.execution_session_id, before["checkpoint"])
        await self.conversation("cancel", session_id=chat["id"], request_id=request)
        after = await self.snapshot(chat)
        self.assertEqual(after["input_count"], 0)
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertEqual(len(self.gateway.requests), 1)
