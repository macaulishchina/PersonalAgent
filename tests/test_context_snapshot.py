"""Context snapshots through real Pi and a local synthetic provider."""

import json
import asyncio
import base64
import unittest
import uuid
import sys

import test_pi_skeleton as fixtures
import test_pi_waiting as waiting_fixtures
from assistant_runtime.api import ConversationRequest, FileRequest
from pi_gateway import sse_text_reply, sse_tool_call, sse_tool_batch


class NativeContextTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PiSkeletonTest.asyncSetUp
    asyncTearDown = fixtures.PiSkeletonTest.asyncTearDown
    session = fixtures.PiSkeletonTest.session
    completed = fixtures.PiSkeletonTest.completed

    async def test_foreign_tool_history_then_parallel_tools_keeps_wire_pairing(self):
        material = self.root / "material.txt"
        material.write_text("SYNTHETIC-MATERIAL", encoding="utf-8")
        rejected = []
        model_steps = {}

        def script(step, body):
            calls, results = [], []
            pending = set()
            interrupted = False
            for item in body.get("input", []):
                if item.get("type") == "function_call":
                    calls.append(item["call_id"])
                    pending.add(item["call_id"])
                elif item.get("type") == "function_call_output":
                    results.append(item["call_id"])
                    pending.discard(item["call_id"])
                elif pending:
                    interrupted = True
            if sorted(calls) != sorted(results) or interrupted:
                rejected.append({"model": body["model"], "calls": calls, "results": results})
                return 400, {"error": {"message": "Unpaired tool messages", "type": "invalid_request_error"}}
            model = body["model"]
            model_steps[model] = model_steps.get(model, 0) + 1
            round = model_steps[model]
            if round in (1, 2):
                offset = (round - 1) * 2 + (0 if model == "model-a" else 4)
                return sse_tool_batch(
                    [
                        (
                            f"mcp__extensions__kfc_workbench:{offset + i}",
                            "read",
                            {
                                "path": str(
                                    material if model == "model-a" or i == 0 else self.root / "missing.txt"
                                )
                            },
                        )
                        for i in range(2)
                    ],
                    trailing_text="" if model == "model-b" else None,
                )
            return sse_text_reply(f"done-{model}", "PAIRED")

        self.gateway.script_fn = script
        source = await self.session()
        await self.client.request(
            "run.start", {"session_id": source, "input": [{"type": "text", "text": "Source tools"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed")
        checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        frozen = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": checkpoint}
        )
        target = await self.session(model="model-b")
        await self.client.request(
            "session.seed",
            {"session_id": target, "snapshot_ref": "parallel-history", "content": frozen["content"]},
        )
        await self.client.request(
            "run.start", {"session_id": target, "input": [{"type": "text", "text": "New parallel tools"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed", rejected)
        self.assertEqual(rejected, [])
        self.assertEqual(model_steps, {"model-a": 3, "model-b": 3})
        from pathlib import Path

        saved = [
            json.loads(line).get("message", {})
            for line in Path(self.client.workers[target].meta["path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(any(m.get("role") == "toolResult" and m.get("isError") for m in saved))
        self.assertTrue(
            any(
                b.get("type") == "text" and b.get("text") == ""
                for m in saved
                if m.get("role") == "assistant"
                for b in m.get("content", [])
            )
        )

    async def test_mixed_mcp_reply_with_plain_same_cross_snapshot_and_restart(self):
        server = self.root / "synthetic_mcp.py"
        log = self.root / "mcp-calls.jsonl"
        server.write_text(
            "from fastmcp import FastMCP\nimport json\nfrom pathlib import Path\n"
            "m=FastMCP('synthetic')\n"
            "@m.tool(annotations={'readOnlyHint':True,'destructiveHint':False})\n"
            "def kfc_workbench(action:str)->str:\n"
            f" with Path({str(log)!r}).open('a',encoding='utf-8') as f: f.write(json.dumps(action)+'\\n')\n"
            " return json.dumps({'action':action,'fact':'SYNTHETIC'})\n"
            "if __name__=='__main__': m.run(transport='stdio',show_banner=False)\n",
            encoding="utf-8",
        )
        mcp = {"extensions": {"command": sys.executable, "args": [str(server)], "enabled": True}}
        requests = []
        phase = {"step": 0, "base": 0}
        rejected = []

        def script(step, body):
            pending = set()
            for item in body.get("input", []):
                kind = item.get("type")
                if kind == "function_call":
                    pending.add(item["call_id"])
                elif kind == "function_call_output":
                    if item["call_id"] not in pending or "No result provided" in json.dumps(item["output"]):
                        rejected.append("orphan or fabricated result")
                    pending.discard(item["call_id"])
                elif (
                    pending
                    and kind == "message"
                    and not any(part.get("text") for part in item.get("content", []))
                ):
                    rejected.append("empty assistant message interrupted tool results")
            if pending or rejected:
                return 400, {"error": {"message": "Unpaired or interrupted tool results"}}
            requests.append(body)
            phase["step"] += 1
            index = phase["step"]
            if index == 1:
                return sse_tool_call(
                    f"discover-{phase['base']}",
                    "tool_search",
                    {"query": "extensions kfc_workbench", "limit": 1},
                )
            if index in (2, 3):
                return sse_tool_batch(
                    [
                        (
                            f"mcp__extensions__kfc_workbench:{phase['base'] + index * 2 + i}",
                            "mcp__extensions__kfc_workbench",
                            {"action": action},
                        )
                        for i, action in enumerate(("catalog", "history"))
                    ],
                    trailing_text="" if index == 2 else "I will inspect both results, then finish.",
                )
            return sse_text_reply("finished", "DONE")

        self.gateway.script_fn = script
        source = await self.session(config={"model_reasoning_effort": "low", "mcp_servers": mcp})
        from pathlib import Path

        settings_path = Path(self.client.workers[source].meta["agent_dir"]) / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["compaction"]["keepRecentTokens"] = 100
        settings_path.write_text(json.dumps(settings))
        await self.client.request(
            "run.start", {"session_id": source, "input": [{"type": "text", "text": "Plain MCP turn"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed", rejected)
        checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        snapshot = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": checkpoint}
        )
        original = json.dumps(snapshot["content"], sort_keys=True)
        for model in ("model-a", "model-b"):
            with self.subTest(model=model):
                phase["step"] = 0
                phase["base"] += 20
                target = await self.session(
                    model=model, config={"model_reasoning_effort": "low", "mcp_servers": mcp}
                )
                await self.client.request(
                    "session.seed",
                    {"session_id": target, "snapshot_ref": "mcp-history", "content": snapshot["content"]},
                )
                route = self.settings.route(model)
                await self.client.request(
                    "run.start",
                    {
                        "session_id": target,
                        "model": model,
                        "expected_model": {k: route[k] for k in ("model", "revision")},
                        "input": [{"type": "text", "text": "New mixed MCP turn"}],
                    },
                )
                completed = await self.completed()
                self.assertEqual(completed["status"], "completed", rejected)
                self.assertEqual(completed["route"], route)
                await self.client.request("session.close", {"session_id": target})
                await self.client.request("session.resume", {"session_id": target})
                await self.client.request(
                    "run.start",
                    {
                        "session_id": target,
                        "input": [{"type": "text", "text": "Continue without repeating tools"}],
                    },
                )
                self.assertEqual((await self.completed())["status"], "completed", rejected)
        self.assertEqual(rejected, [])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 12)
        self.assertEqual(len(requests), 14)
        self.assertEqual(json.dumps(snapshot["content"], sort_keys=True), original)
        self.assertEqual(await self.client.request("session.checkpoint", {"session_id": source}), checkpoint)
        self.assertIn("I will inspect both results, then finish.", original)
        self.assertTrue(
            any(
                b.get("type") == "text" and b.get("text") == ""
                for m in snapshot["content"]["messages"]
                if m.get("role") == "assistant"
                for b in m.get("content", [])
            )
        )

        # Compact only the source copy, then import the SDK's compressed projection.
        await self.client.request(
            "run.start",
            {
                "session_id": source,
                "input": [
                    {
                        "type": "text",
                        "text": "Second source turn before compaction. " + "SYNTHETIC-BACKGROUND " * 100,
                    }
                ],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed", rejected)
        await self.client.workers[source].command({"type": "compact"})
        await self.client.request(
            "run.start",
            {
                "session_id": source,
                "input": [{"type": "text", "text": "Complete a checkpoint after compaction"}],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed", rejected)
        new_checkpoint = await self.client.request("session.checkpoint", {"session_id": source})
        compressed = await self.client.request(
            "session.snapshot", {"session_id": source, "checkpoint": new_checkpoint}
        )
        self.assertTrue(compressed["content"]["compacted"])
        phase.update(step=0, base=60)
        target = await self.session(
            model="model-b", config={"model_reasoning_effort": "low", "mcp_servers": mcp}
        )
        await self.client.request(
            "session.seed",
            {"session_id": target, "snapshot_ref": "compacted-mcp-history", "content": compressed["content"]},
        )
        await self.client.request(
            "run.start",
            {"session_id": target, "input": [{"type": "text", "text": "New calls after compressed history"}]},
        )
        self.assertEqual((await self.completed())["status"], "completed", rejected)
        self.assertEqual(rejected, [])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 16)
        self.assertEqual(len(requests), 21)
        self.assertEqual(json.dumps(snapshot["content"], sort_keys=True), original)

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

    async def test_public_snapshot_task_does_not_replay_empty_assistant_between_tools(self):
        material = self.root / "protocol-material.txt"
        material.write_text("SYNTHETIC-PROTOCOL", encoding="utf-8")
        rejected = []

        def script(step, body):
            for item in body.get("input", []):
                if (
                    item.get("type") == "message"
                    and item.get("role") == "assistant"
                    and not any(part.get("text") for part in item.get("content", []))
                ):
                    rejected.append("empty assistant replay")
            if rejected:
                return 400, {"error": {"message": "Tool calls interrupted by empty assistant"}}
            if step in (1, 3):
                offset = 0 if step == 1 else 4
                return sse_tool_batch(
                    [(f"workbench:{offset + i}", "read", {"path": str(material)}) for i in range(2)],
                    trailing_text="",
                )
            return sse_text_reply(f"done-{step}", "DONE")

        self.gateway.script_fn = script
        chat = await self.conversation("create", title="Mixed source")
        await self.submit(chat, "Read the synthetic material")
        snapshot = await self.snapshot(chat)
        task = await self.task({"snapshot_ref": snapshot["snapshot_ref"]}, tools=["read"])
        run = await self.wait(task, {"completed", "failed"})
        self.assertEqual(run["state"], "completed", rejected)
        self.assertTrue(run["data"]["context"]["seeded"])
        self.assertEqual(run["data"]["execution_facts"]["route"]["model"], "model-b")
        self.assertEqual(rejected, [])
        self.assertEqual(len(self.gateway.requests), 4)
        self.assertEqual(material.read_text(encoding="utf-8"), "SYNTHETIC-PROTOCOL")

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
