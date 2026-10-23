"""Real Pi subprocess acceptance against a private synthetic model endpoint."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from pi_gateway import FakeResponsesGateway, sse_tool_call, sse_text_reply
from assistant_runtime.app.config import Config
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.pi.rpc import PiClient, RpcError
from assistant_runtime.core.pi.backend import PiBackend
from assistant_runtime.core.speech.disabled import DisabledVoice
from assistant_runtime.app.store import Store


def configuration(url):
    return {
        "enabled": True,
        "gateway_url": url,
        "memory": "model-a",
        "providers": {
            "local": {"protocol": "openai_responses", "base_url": url, "key_env": "TEST_KEY", "enabled": True}
        },
        "entries": {
            name: {
                "provider": "local",
                "model": name,
                "execution_validated": True,
                "images": True,
                "structured": True,
                "reasoning_efforts": ["none", "low", "medium", "high", "max"],
                "default_reasoning_effort": "low",
                "context_window": 32768,
            }
            for name in ("model-a", "model-b")
        },
    }


class PiSkeletonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gateway = FakeResponsesGateway(lambda step, body: sse_text_reply(f"msg{step}", "已完成"))
        self.settings = ModelSettings.parse(configuration(self.gateway.base_url))
        from assistant_runtime.core.ai_usage.service import UsageService

        self.usage = UsageService(self.root / "ai-usage")
        await self.usage.start()
        self.client = PiClient(
            cwd=str(self.root),
            settings=self.settings,
            environment={"ASSISTANT_MODEL_GATEWAY_KEY": "synthetic-key"},
            runtime_dir=self.root / "executions",
            usage=self.usage,
        )
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.usage.close()
        await self.gateway.aclose()
        self.temp.cleanup()

    async def session(self, **kwargs):
        result = await self.client.request(
            "session.create", {"model": "model-a", "config": {"model_reasoning_effort": "low"}, **kwargs}
        )
        return result["session"]["id"]

    async def completed(self):
        async with asyncio.timeout(20):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "run.completed":
                    return event["data"]["run"]

    async def test_real_model_effort_auth_and_restart_resume(self):
        ident = await self.session()
        for model, effort in (("model-a", "low"), ("model-b", "high")):
            result = await self.client.request(
                "run.start",
                {
                    "session_id": ident,
                    "model": model,
                    "effort": effort,
                    "input": [{"type": "text", "text": "synthetic prompt"}],
                },
            )
            self.assertEqual(result["model"], model)
            self.assertEqual((await self.completed())["status"], "completed")
        self.assertEqual([r["model"] for r in self.gateway.requests], ["model-a", "model-b"])
        self.assertEqual([r["reasoning"]["effort"] for r in self.gateway.requests], ["low", "high"])
        self.assertEqual(
            {k.lower(): v for k, v in self.gateway.headers[0].items()}["authorization"],
            "Bearer synthetic-key",
        )
        await self.client.close()
        await self.client.start()
        await self.client.request("session.resume", {"session_id": ident})
        state = await self.client.request("session.read", {"session_id": ident, "include_runs": True})
        self.assertEqual(len(state["session"]["runs"]), 2)
        self.gateway.script_fn = lambda step, body: sse_text_reply("continued", "done")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "continue"}]}
        )
        await self.completed()
        self.assertIn("synthetic prompt", json.dumps(self.gateway.requests[-1]))

    async def test_changed_alias_binding_cannot_resume_supplier_private_context(self):
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        await self.completed()
        self.settings.entries["model-a"]["model"] = "different-upstream"
        with self.assertRaisesRegex(RpcError, "binding changed"):
            await self.client.request("session.resume", {"session_id": ident})
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_configured_max_effort_is_not_silently_clamped(self):
        ident = await self.session()
        await self.client.request(
            "run.start",
            {
                "session_id": ident,
                "model": "model-a",
                "effort": "max",
                "input": [{"type": "text", "text": "synthetic max effort"}],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed")
        self.assertEqual(self.gateway.requests[0]["reasoning"]["effort"], "max")

    async def test_none_is_explicit_even_when_model_default_is_low(self):
        ident = await self.session()
        await self.client.request(
            "run.start",
            {
                "session_id": ident,
                "effort": "none",
                "input": [{"type": "text", "text": "synthetic no reasoning"}],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed")
        self.assertEqual(self.gateway.requests[0]["reasoning"]["effort"], "none")

    async def test_real_file_operation_and_tool_trace(self):
        target = self.root / "created.txt"
        self.gateway.script_fn = (
            lambda step, body: sse_tool_call("write1", "write", {"path": str(target), "content": "verified"})
            if step == 1
            else sse_text_reply("final", "文件已保存")
        )
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic file test"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(target.read_text(), "verified")
        self.assertTrue(any(i["type"] == "file" for i in run["items"]))

    async def test_runaway_tool_loop_stops_before_next_upstream_call(self):
        from dataclasses import replace

        self.usage.settings = replace(self.usage.settings, max_requests=3)
        self.usage.ledger.settings = self.usage.settings
        self.gateway.script_fn = lambda step, body: sse_tool_call(
            f"repeat{step}", "bash", {"command": "printf checked"}
        )
        ident = await self.session(budget_id="scheduled-run")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "check once"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "ai_usage_blocked")
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual(run["ai_usage"]["requests"], 3)
        # A new model session, continuation or host restart cannot renew the same run.
        await self.client.close()
        await self.client.start()
        other = await self.session(model="model-b", budget_id="scheduled-run")
        await self.client.request(
            "run.start", {"session_id": other, "input": [{"type": "text", "text": "continue"}]}
        )
        self.assertEqual((await self.completed())["error"]["code"], "ai_usage_blocked")
        self.assertEqual(len(self.gateway.requests), 3)

    async def test_auxiliary_timeout_does_not_block_original_execution(self):
        from assistant_runtime.api.ai_usage import UsageScope

        scope = UsageScope("jev-timeout", "foreground")
        ticket = await self.usage.admit(scope, service="decision", provider="typesafe")
        await self.usage.settle(ticket["id"], state="unknown")
        ident = await self.session(budget_id=scope.work_id)
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "continue original logic"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["ai_usage"]["requests"], 2)
        self.assertEqual(run["ai_usage"]["unknown_requests"], 1)

    async def test_http_rejection_is_structured_and_never_retried(self):
        self.gateway.script_fn = lambda step, body: (402, {"error": {"message": "Insufficient Balance"}})
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["status"], 402)
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertFalse(run["ai_usage"]["inflight"])

    async def test_truncated_stream_is_unknown_and_cannot_restart_dispatch(self):
        self.gateway.script_fn = lambda step, body: sse_text_reply("truncated", "partial")[:-1]
        ident = await self.session(budget_id="truncated-run")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        self.assertEqual((await self.completed())["status"], "unknown")
        await self.client.close()
        await self.client.start()
        with self.assertRaises(RpcError):
            await self.client.request("session.resume", {"session_id": ident})
        other = await self.session(budget_id="truncated-run")
        await self.client.request(
            "run.start", {"session_id": other, "input": [{"type": "text", "text": "continue"}]}
        )
        self.assertEqual((await self.completed())["status"], "failed")
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_missing_usage_is_known_completion_with_unknown_usage(self):
        def script(step, body):
            events = sse_text_reply("missing-usage", "synthetic")
            events[-1]["response"].pop("usage")
            return events

        self.gateway.script_fn = script
        ident = await self.session(budget_id="unknown-charge")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["ai_usage"]["unknown_usage"], 1)
        other = await self.session(budget_id="unknown-charge")
        await self.client.request(
            "run.start", {"session_id": other, "input": [{"type": "text", "text": "retry"}]}
        )
        await self.completed()
        self.assertEqual(len(self.gateway.requests), 2)

    async def test_token_limit_reserves_before_upstream_submission(self):
        from dataclasses import replace

        self.usage.settings = replace(self.usage.settings, work_limits={"max_input_tokens": 1})
        self.usage.ledger.settings = self.usage.settings
        ident = await self.session(budget_id="tokens")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic"}]}
        )
        self.assertEqual((await self.completed())["error"]["code"], "ai_usage_blocked")
        self.assertEqual(len(self.gateway.requests), 0)

    async def test_compaction_failure_is_explicit_but_never_overwrites_unknown_execution(self):
        ident = await self.session()
        worker = self.client.workers[ident]
        await worker._handle_message({"type": "compaction_end", "errorMessage": "private upstream detail"})
        await worker._finalize_turn("summary-failed", "completed")
        result = await self.completed()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "context_compaction_failed")
        self.assertNotIn("private upstream", str(result))
        worker.compaction_error = True
        await worker._finalize_turn("disconnected", "unknown")
        self.assertEqual((await self.completed())["status"], "unknown")

    async def test_large_tool_result_is_saved_and_bounded_without_reexecution(self):
        self.gateway.script_fn = lambda step, body: (
            sse_tool_call("large", "bash", {"command": "printf '%12000s' x"})
            if step == 1
            else sse_text_reply("done", "done")
        )
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "test"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed")
        body = self.gateway.requests[1]
        results = [item for item in body["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(len(results), 1)
        preview = json.dumps(results[0], ensure_ascii=False)
        self.assertLess(len(preview), 7500)
        self.assertIn("Partial tool result preview", preview)
        files = list(self.root.glob("tool-results/*.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text())
        self.assertGreater(len(saved["content"][0]["text"]), 11000)
        self.assertFalse(saved["is_error"])
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.gateway.requests), 2)

    async def test_lost_compaction_can_be_retried_with_independent_timeout_and_budget(self):
        from dataclasses import replace

        self.client.settings = replace(self.settings, timeout_seconds=1, compaction_timeout_seconds=3)
        ident = await self.session(budget_id="compact-recover")
        folder = Path(self.client.workers[ident].meta["agent_dir"])
        settings = json.loads((folder / "settings.json").read_text())
        settings["compaction"]["keepRecentTokens"] = 100
        (folder / "settings.json").write_text(json.dumps(settings))
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "context " * 100}]}
        )
        await self.completed()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "more context " * 100}]}
        )
        await self.completed()
        worker = self.client.workers[ident]

        def incomplete(step, body):
            # Authoritative stream start, no completion: lost summary only.
            return [{"type": "response.created", "response": {"id": "lost", "status": "in_progress"}}]

        self.gateway.script_fn = incomplete
        with self.assertRaises(RpcError):
            await worker.command({"type": "compact"})
        snapshot = await self.usage.snapshot("compact-recover")
        self.assertEqual(snapshot["unknown_requests"], 1)
        self.assertEqual(snapshot["unknown_execution_requests"], 0)

        def slow_summary(step, body):
            import time

            time.sleep(1.3)  # Beyond normal deadline, within separate compression deadline.
            return sse_text_reply("summary", "Preserved original task context")

        self.gateway.script_fn = slow_summary
        await worker.command({"type": "compact"})
        snapshot = await self.usage.snapshot("compact-recover")
        self.assertEqual(snapshot["compactions"], 2)
        self.assertEqual(snapshot["requests"], 4)
        self.gateway.script_fn = lambda step, body: sse_text_reply("continued", "done")
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "continue"}]}
        )
        self.assertEqual((await self.completed())["status"], "completed")

    async def test_repeated_native_compaction_is_bounded(self):
        from dataclasses import replace

        self.usage.settings = replace(self.usage.settings, work_limits={"max_compactions": 1})
        self.usage.ledger.settings = self.usage.settings
        ident = await self.session(budget_id="compaction-run")
        folder = Path(self.client.workers[ident].meta["agent_dir"])
        settings = json.loads((folder / "settings.json").read_text())
        settings["compaction"]["keepRecentTokens"] = 100
        (folder / "settings.json").write_text(json.dumps(settings))
        for number in range(3):
            await self.client.request(
                "run.start",
                {"session_id": ident, "input": [{"type": "text", "text": "synthetic context " * 100}]},
            )
            await self.completed()
            if number == 0:
                continue
            worker = self.client.workers[ident]
            try:
                await worker.command({"type": "compact"})
            except RpcError:
                if number == 1:
                    raise
        budget = await self.usage.snapshot("compaction-run")
        self.assertEqual(budget["compactions"], 1)
        self.assertEqual(budget["blocked"], "usage_limit:work:max_compactions")
        self.assertEqual(len(self.gateway.requests), 4)

    async def test_native_image_and_explicit_skill_inventory_reach_provider(self):
        import base64

        for name in ("AGENTS.md", "CLAUDE.md"):
            (self.root / name).write_text("CODING_AI_CONTEXT_MUST_NOT_LEAK")
        coding = self.root / ".agents/skills/hsdd-epic"
        coding.mkdir(parents=True)
        (coding / "SKILL.md").write_text(
            "---\nname: hsdd-epic\ndescription: CODING_AI_SKILL_MUST_NOT_LEAK\n---\n"
        )
        skill = self.root / "skills" / "synthetic-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: synthetic-skill\ndescription: Synthetic local validation steps.\n---\nRead back the synthetic result.\n"
        )
        from assistant_runtime.app.bootstrap import assistant_skill_paths

        bundled = assistant_skill_paths(Config(root=self.root, plugins=["media"]))
        self.client.skill_paths = (*bundled, skill)
        path = self.root / "test.png"
        path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP8z0AaYCJR/aiGUQ1DSAMAQC4BH2bjRnMAAAAASUVORK5CYII="
            )
        )
        ident = await self.session()
        await self.client.request(
            "run.start",
            {
                "session_id": ident,
                "input": [
                    {"type": "text", "text": "synthetic image"},
                    {"type": "localImage", "path": str(path)},
                ],
            },
        )
        self.assertEqual((await self.completed())["status"], "completed")
        encoded = json.dumps(self.gateway.requests[0])
        self.assertIn("data:image/png;base64,", encoded)
        self.assertIn("synthetic-skill", encoded)
        self.assertIn("media-acquisition", encoded)
        self.assertNotIn("CODING_AI_CONTEXT_MUST_NOT_LEAK", encoded)
        self.assertNotIn("CODING_AI_SKILL_MUST_NOT_LEAK", encoded)
        self.assertNotIn("hsdd-epic", encoded)

    async def test_missing_model_images_and_invalid_command_fail_before_submit(self):
        ident = await self.session()
        with self.assertRaises(ValueError):
            await self.client.request(
                "run.start", {"session_id": ident, "model": "not-configured", "input": []}
            )
        with self.assertRaises(ValueError):
            await self.client.request("invented.operation", {})
        self.settings.entries["model-a"]["images"] = False
        with self.assertRaisesRegex(ValueError, "images"):
            await self.client.request(
                "run.start",
                {"session_id": ident, "input": [{"type": "image", "data": "AA==", "mimeType": "image/png"}]},
            )
        self.assertEqual(self.gateway.requests, [])
        self.assertEqual(len((await self.client.request("models.list", {}))["data"]), 2)

    async def test_real_clarification_and_task_goal(self):
        def script(step, body):
            if step == 1:
                return sse_tool_call(
                    "ask", "request_user_input", {"questions": [{"id": "q", "question": "保存位置？"}]}
                )
            if step == 2:
                return sse_tool_call("goal", "update_task_goal", {"status": "complete", "summary": "已完成"})
            return sse_text_reply("final", "已完成")

        self.gateway.script_fn = script
        ident = await self.session(long_term=True)
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic task"}]}
        )
        async with asyncio.timeout(15):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "input.requested":
                    break
        await self.client.respond(event["id"], {"answers": {"q": {"answers": ["scratch"]}}})
        self.assertEqual((await self.completed())["status"], "completed")
        self.assertEqual(
            (await self.client.request("goal.get", {"session_id": ident}))["goal"]["status"], "complete"
        )
        self.assertIn("scratch", json.dumps(self.gateway.requests[-1]))

    async def test_cancel_real_shell_waits_for_confirmed_end(self):
        self.gateway.script_fn = lambda step, body: sse_tool_call("sleep", "bash", {"command": "sleep 30"})
        ident = await self.session()
        result = await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic cancellation"}]}
        )
        async with asyncio.timeout(10):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "activity.started" and event["data"]["item"]["type"] == "command":
                    break
        await self.client.request("run.cancel", {"session_id": ident, "run_id": result["run"]["id"]})
        self.assertEqual((await self.completed())["status"], "interrupted")
        self.assertEqual((await self.client.request("operations.list", {"session_id": ident}))["data"], [])

    async def test_full_event_queue_cannot_block_shutdown_or_leave_owned_process(self):
        self.gateway.script_fn = lambda step, body: sse_tool_call("wait", "bash", {"command": "sleep 30"})
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic shutdown"}]}
        )
        async with asyncio.timeout(10):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "activity.started" and event["data"]["item"]["type"] == "command":
                    break
        process = self.client.workers[ident].process
        while not self.client.events.full():
            self.client.events.put_nowait({"kind": "synthetic", "data": {}})
        await asyncio.wait_for(self.client.close(), 3)
        self.assertIsNotNone(process.returncode)
        meta = json.loads((self.root / "executions" / ident / "metadata.json").read_text())
        self.assertEqual(meta["runs"][-1]["status"], "unknown")
        self.assertTrue(meta["pending_operations"])

    async def test_process_death_is_unknown_and_does_not_replay(self):
        self.gateway.script_fn = lambda step, body: sse_tool_call("sleep", "bash", {"command": "sleep 30"})
        ident = await self.session()
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic interruption"}]}
        )
        async with asyncio.timeout(10):
            while True:
                event = await self.client.events.get()
                if event["kind"] == "activity.started" and event["data"]["item"]["type"] == "command":
                    break
        self.client.workers[ident].process.kill()
        self.assertEqual((await self.completed())["status"], "unknown")
        with self.assertRaises(RpcError):
            await self.client.request("session.resume", {"session_id": ident})
        self.assertEqual(len(self.gateway.requests), 1)

    async def test_mcp_schema_and_real_call(self):
        script = self.root / "sample_server.py"
        script.write_text(
            "from fastmcp import FastMCP\nm=FastMCP('sample')\n@m.tool()\ndef ping(target: str)->str: return 'pong:'+target\nif __name__=='__main__': m.run(transport='stdio',show_banner=False)\n"
        )
        ident = await self.session(
            config={
                "model_reasoning_effort": "low",
                "mcp_servers": {
                    "sample": {"command": sys.executable, "args": [str(script)], "enabled": True}
                },
            }
        )
        inventory = await self.client.request("mcp.inspect", {"session_id": ident, "serverName": "sample"})
        self.assertIn("target", inventory["data"][0]["tools"]["ping"]["inputSchema"]["properties"])

        def discover_and_call(step, body):
            if step == 1:
                return sse_tool_call("search", "tool_search", {"query": "sample ping", "limit": 1})
            if step == 2:
                return sse_tool_call("ping", "mcp__sample__ping", {"target": "test"})
            return sse_text_reply("done", "pong:test")

        self.gateway.script_fn = discover_and_call
        await self.client.request(
            "run.start", {"session_id": ident, "input": [{"type": "text", "text": "synthetic mcp test"}]}
        )
        result = await self.completed()
        self.assertTrue(any(i["type"] == "mcp" and "pong:test" in json.dumps(i) for i in result["items"]))

    async def test_backend_ignores_old_codex_identity_and_restores_only_pi(self):
        store = Store(self.root / "state/assistant.sqlite3")
        store.set("thread_id", "old-codex")
        store.transcripts.append("user", "old dialogue must not be injected")
        (self.root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
        cfg = Config(
            root=self.root,
            workspace=str(self.root),
            plugins=["models"],
            main_model="model-a",
            main_reasoning_effort="low",
            model_api=configuration(self.gateway.base_url),
        )
        from scripts.reset_execution_state import reset

        isolated_execution = self.root / "private-runner"
        backend = PiBackend(
            cfg, store, AsyncMock(), AsyncMock(), usage=self.usage, runtime_dir=isolated_execution
        )
        with self.assertRaisesRegex(RuntimeError, "Legacy execution state"):
            await backend.start()
        await backend.close()
        reset(self.root, apply=True, confirmed_stopped=True)
        backend = PiBackend(
            cfg, store, AsyncMock(), AsyncMock(), usage=self.usage, runtime_dir=isolated_execution
        )
        try:
            await backend.start()
            done = asyncio.Event()

            async def completed(*args):
                done.set()

            backend.on_conversation_done = completed
            await backend.submit("new conversation")
            await asyncio.wait_for(done.wait(), 15)
            self.assertNotIn("old dialogue", json.dumps(self.gateway.requests))
            self.assertNotEqual(backend.execution_session_id, "old-codex")
            self.assertTrue((isolated_execution / backend.execution_session_id / "metadata.json").is_file())
            self.assertFalse((cfg.state_dir / "executions").exists())
        finally:
            await backend.close()
            store.db.close()

    async def test_voice_is_explicitly_disabled(self):
        voice = DisabledVoice()
        with self.assertRaises(RuntimeError):
            await voice.open()
        with self.assertRaises(RuntimeError):
            await voice.say("test")
        self.assertFalse(voice.ready.is_set())
        self.assertFalse(voice.speech_completed)
