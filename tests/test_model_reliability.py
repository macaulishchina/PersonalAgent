"""Real Pi adapter and isolated HTTP failures; no supplier or device calls."""

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from assistant_runtime.api.ai_usage import UsageError, UsageScope
from assistant_runtime.api.models import ModelCallError
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.models.client import ModelClient
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.pi.accounts import NativeAccounts
from assistant_runtime.core.pi.rpc import PiClient
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration


class ReliabilityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".env").write_text("TEST_KEY=synthetic-key\n")
        self.gateway = FakeResponsesGateway(lambda n, b: sse_text_reply(str(n), "OK"))
        self.settings = ModelSettings.parse(configuration(self.gateway.base_url))
        self.accounts = NativeAccounts(self.root, self.settings)
        self.client = ModelClient(self.settings, accounts=self.accounts)
        self.usage = UsageService(self.root / "usage")
        await self.usage.start()
        self.scope = UsageScope("synthetic-work", "test")
        self.pi = None

    async def asyncTearDown(self):
        if self.pi:
            await self.pi.close()
        await self.client.close()
        await self.usage.close()
        await self.gateway.aclose()
        self.temp.cleanup()

    async def infer(self):
        return await self.client.complete(
            {
                "model": "model-a",
                "reasoning": {"effort": "low"},
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "synthetic"}]}],
                "max_output_tokens": 0,
            },
            session_id="test",
            usage=self.usage,
            scope=self.scope,
            bounds={"input_tokens": 32768, "output_tokens": 4096},
        )

    async def test_transient_then_success_each_attempt_metered(self):
        self.gateway.script_fn = (
            lambda n, b: (503, {"error": {"code": "overloaded", "message": "temporarily busy"}})
            if n == 1
            else sse_text_reply("done", "OK")
        )
        result = await self.infer()
        self.assertEqual(result["text"], "OK")
        self.assertEqual(len(self.gateway.requests), 2)
        self.assertEqual(len(result["request_attempts"]), 2)
        snapshot = await self.usage.snapshot(self.scope.work_id)
        self.assertEqual(snapshot["requests"], 2)
        self.assertEqual(snapshot["inflight"], 0)

    async def test_quota_never_retried_error_retained_and_key_redacted(self):
        self.gateway.script_fn = lambda n, b: (
            429,
            {
                "error": {
                    "code": "insufficient_quota",
                    "message": "quota exhausted synthetic-key sk-secret-123456789",
                }
            },
        )
        with self.assertRaises(ModelCallError) as caught:
            await self.infer()
        failure = caught.exception.failure
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertEqual(failure["category"], "quota")
        self.assertEqual(failure["provider_code"], "insufficient_quota")
        self.assertNotIn("synthetic-key", failure["message"])
        self.assertNotIn("sk-secret", failure["message"])

    async def test_unclassified_200_failure_retries_once_retains_unknown_cost(self):
        self.gateway.script_fn = lambda n, b: [
            {
                "type": "response.failed",
                "response": {
                    "id": "unknown",
                    "status": "failed",
                    "error": {"message": "synthetic uncategorized"},
                },
            }
        ]
        with self.assertRaises(ModelCallError):
            await self.infer()
        self.assertEqual(len(self.gateway.requests), 2)
        snapshot = await self.usage.snapshot(self.scope.work_id)
        self.assertEqual(snapshot["unknown_requests"], 2)
        self.assertEqual(snapshot["inflight"], 0)

    async def test_partial_output_never_retried(self):
        self.gateway.script_fn = lambda n, b: sse_text_reply("partial", "Already output")[:-1]
        with self.assertRaises(ModelCallError) as caught:
            await self.infer()
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertTrue(caught.exception.failure["output_started"])
        self.assertEqual(caught.exception.failure["stop_reason"], "partial_output")

    async def test_diagnostic_lifecycle_has_metadata_not_content(self):
        with self.assertLogs("assistant_runtime.core.pi.accounts", level="DEBUG") as captured:
            result = await self.infer()
        self.assertEqual(result["text"], "OK")
        self.assertEqual(len(self.gateway.requests), 1)
        records = {r.msg: getattr(r, "diagnostic", {}) for r in captured.records}
        self.assertIn("model.request.started", records)
        self.assertIn("model.response.headers", records)
        self.assertIn("model.request.completed", records)
        self.assertEqual(records["model.request.completed"]["terminal_event"], "response.completed")
        import json

        self.assertNotIn("synthetic-key", json.dumps(records))
        self.assertNotIn('"input"', json.dumps(records))

    async def test_partial_stream_preserves_original_error_and_event(self):
        self.gateway.script_fn = lambda n, b: sse_text_reply("diagnostic-partial", "partial")[:-1]
        with self.assertRaises(ModelCallError) as caught:
            await self.infer()
        problem = caught.exception.failure
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertEqual(problem["response_id"], "resp_diagnostic-partial")
        self.assertIn("terminal response event", problem["native_error"])
        self.assertEqual(problem["stop_reason"], "partial_output")
        self.assertEqual(problem["status"], 200)
        self.assertNotIn("供应商未提供", problem["message"])

    async def test_sse_error_root_fields_preserved_and_redacted(self):
        def script(n, body):
            return [
                *sse_text_reply("before-error", "partial")[:-1],
                {
                    "type": "error",
                    "code": "gateway_stream_error",
                    "message": "upstream failed synthetic-key Bearer private-token-value",
                },
            ]

        self.gateway.script_fn = script
        with self.assertRaises(ModelCallError) as caught:
            await self.infer()
        problem = caught.exception.failure
        self.assertEqual(len(self.gateway.requests), 1)
        # The SDK consumes root SSE error events before provider callbacks.
        # Retain its original diagnostic rather than inventing an observed event.
        self.assertEqual(problem["sdk_terminal"], "error")
        self.assertIn("upstream failed", problem["native_error"])
        self.assertIn("upstream failed", problem["message"])
        import json

        self.assertNotIn("synthetic-key", json.dumps(problem))
        self.assertNotIn("private-token-value", json.dumps(problem))

    async def test_next_attempt_must_pass_usage_gate(self):
        self.usage.settings = replace(self.usage.settings, max_requests=1)
        self.usage.ledger.settings = self.usage.settings
        self.gateway.script_fn = lambda n, b: (503, {"error": {"code": "overloaded", "message": "busy"}})
        with self.assertRaises(UsageError):
            await self.infer()
        self.assertEqual(len(self.gateway.requests), 1)
        self.assertEqual((await self.usage.snapshot(self.scope.work_id))["inflight"], 0)

    async def test_cancel_during_retry_wait_does_not_send_again(self):
        self.gateway.script_fn = lambda n, b: (503, {"error": {"code": "overloaded", "message": "busy"}})
        task = asyncio.create_task(self.infer())
        async with asyncio.timeout(10):
            while (await self.usage.snapshot(self.scope.work_id)).get(
                "requests", 0
            ) < 1 or not self.gateway.requests:
                await asyncio.sleep(0.01)
            while (await self.usage.snapshot(self.scope.work_id)).get("inflight"):
                await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.65)
        self.assertEqual(len(self.gateway.requests), 1)

    async def execution(self):
        self.pi = PiClient(
            cwd=str(self.root),
            settings=self.settings,
            environment={"PA_EXECUTION_TOKEN": "synthetic"},
            runtime_dir=self.root / "executions",
            usage=self.usage,
            accounts=self.accounts,
        )
        await self.pi.start()
        return (
            await self.pi.request(
                "session.create", {"model": "model-a", "config": {"model_reasoning_effort": "low"}}
            )
        )["session"]["id"]

    async def completed(self):
        async with asyncio.timeout(30):
            while True:
                event = await self.pi.events.get()
                if event["kind"] == "run.completed":
                    return event["data"]["run"]

    async def test_real_agent_transient_retry_preserves_tool_results(self):
        target = self.root / "write-count.txt"
        self.gateway.script_fn = lambda n, b: (
            sse_tool_call("write", "write", {"path": str(target), "content": "once"})
            if n == 1
            else (503, {"error": {"code": "overloaded", "message": "busy"}})
            if n == 2
            else sse_text_reply("done", "OK")
        )
        session = await self.execution()
        await self.pi.request(
            "run.start", {"session_id": session, "input": [{"type": "text", "text": "synthetic write"}]}
        )
        run = await self.completed()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(target.read_text(), "once")
        self.assertEqual(len(self.gateway.requests), 3)
        self.assertEqual(run["ai_usage"]["requests"], 3)
        writes = [i for i in run["items"] if i.get("type") == "file"]
        self.assertEqual(len(writes), 1)

    async def test_manual_recovery_keeps_budget_and_rejects_stale_target(self):
        self.gateway.script_fn = (
            lambda n, b: (
                401,
                {"error": {"code": "invalid_api_key", "message": "synthetic credential rejected"}},
            )
            if n == 1
            else sse_text_reply("recovered", "OK")
        )
        session = await self.execution()
        await self.pi.request(
            "run.start", {"session_id": session, "input": [{"type": "text", "text": "synthetic question"}]}
        )
        first = await self.completed()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure"]["category"], "authentication")
        await self.pi.request(
            "run.start",
            {"session_id": session, "retry_of": first["id"], "input": [{"type": "text", "text": "continue"}]},
        )
        second = await self.completed()
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["budget_id"], first["budget_id"])
        self.assertEqual(second["ai_usage"]["requests"], 2)
        with self.assertRaisesRegex(Exception, "checkpoint"):
            await self.pi.request(
                "run.start",
                {
                    "session_id": session,
                    "retry_of": first["id"],
                    "input": [{"type": "text", "text": "continue"}],
                },
            )
        self.assertEqual(len(self.gateway.requests), 2)

    async def test_conversation_recovery_is_bound_idempotent_and_keeps_history(self):
        import uuid
        from assistant_runtime.api.conversation import ConversationRequest
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService

        self.gateway.script_fn = (
            lambda n, b: (
                401,
                {"error": {"code": "invalid_api_key", "message": "synthetic authentication failure"}},
            )
            if n == 1
            else sse_text_reply("done", "RECOVERED")
        )
        config = replace(
            Config.load(initialize(self.root / "instance", preset="slim")),
            workspace=str(self.root),
            main_model="model-a",
            main_reasoning_effort="low",
            model_api=configuration(self.gateway.base_url),
        )
        (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 20)
            session = host.conversation.session

            async def call(action, **args):
                return await host.runtime.invoke(ConversationRequest(action, {"session_id": session, **args}))

            async def idle():
                async with asyncio.timeout(30):
                    while (await call("queue"))["requests"]:
                        await asyncio.sleep(0.03)

            await call("submit", request_id=str(uuid.uuid4()), text="synthetic original question")
            await idle()
            history = (await call("history"))["messages"]
            failed = next(m for m in history if m.get("failure"))
            self.assertEqual(failed["failure"]["category"], "authentication")
            turn = failed["turn_id"]
            ident = str(uuid.uuid4())
            await asyncio.gather(
                call("retry", request_id=ident, turn_id=turn), call("retry", request_id=ident, turn_id=turn)
            )
            await idle()
            await call("retry", request_id=ident, turn_id=turn)
            self.assertEqual(len(self.gateway.requests), 2)
            history = (await call("history"))["messages"]
            self.assertTrue(any(m["text"] == "synthetic original question" for m in history))
            self.assertTrue(any(m["text"] == "RECOVERED" for m in history))
            worker = host.backend.client.workers[host.backend.execution_session_id]
            self.assertEqual(worker.meta["runs"][-1]["budget_id"], worker.meta["runs"][-2]["budget_id"])
            other = (await host.runtime.invoke(ConversationRequest("create", {"title": "other"})))["id"]
            with self.assertRaises(ValueError):
                await host.runtime.invoke(
                    ConversationRequest(
                        "retry", {"session_id": other, "request_id": str(uuid.uuid4()), "turn_id": turn}
                    )
                )
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 20)

    async def test_native_helper_delay_abort_and_mixed_limits(self):
        from assistant_runtime.platforms.executables import pi_node
        from assistant_runtime.core.pi.accounts import installation

        package = await installation()
        helper = package.parent / "pi-ai/dist/utils/provider-retry.js"
        if not helper.is_file():
            helper = package / "node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.js"
        policy = Path(__file__).resolve().parents[1] / "src/assistant_runtime/core/pi/reliability.mjs"
        script = """
import assert from 'node:assert/strict';
const {retryProviderRequest} = await import(process.argv[1]);
const {reliableRequest} = await import(process.argv[2]);
async function run(sequence, {delay='1',abort=false,denyAt=0}={}) {
 const controller=new AbortController(); let calls=0, gates=0; const settled=[];
 const work=reliableRequest({retryProviderRequest, signal:controller.signal,
   policy:{max_retries:2,unknown_retries:1,max_delay_seconds:1},emit:async()=>{},
   gate:async event=>{if(event.type==='attempt_start'){gates++;if(gates===denyAt)throw new Error('denied');}
     else {settled.push(event.attempt);if(abort)setTimeout(()=>controller.abort(),5);}},
   attempt:async e=>{calls++;e.terminal=true;e.headers=new Headers({'retry-after-ms':delay});
    const category=sequence[Math.min(calls-1,sequence.length-1)];
    return {terminal:{type:'error',error:{content:[]}},failure:{category,code:'model_'+category,generation_settled:true}};}
 });
 let result;try {result=await work;}catch(e){result={aborted:controller.signal.aborted};}
 return {calls,gates,settled,result};
}
assert.equal((await run(['transient','unknown','transient'])).calls,3);
assert.equal((await run(['unknown'])).calls,2);
assert.equal((await run(['transient'],{delay:'2000'})).result.failure.stop_reason,'retry_delay_exceeded');
const cancelled=await run(['transient'],{delay:'500',abort:true});assert.equal(cancelled.calls,1);assert.equal(cancelled.result.aborted,true);
console.log('native policy boundaries passed');
"""
        process = await asyncio.create_subprocess_exec(
            pi_node(),
            "--input-type=module",
            "-e",
            script,
            helper.as_uri(),
            policy.as_uri(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertIn(b"passed", stdout)

    async def test_model_error_evidence_survives_real_host_ipc_and_mcp(self):
        import uuid
        from fastmcp import Client
        from assistant_runtime.api.models import ModelRequest
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.core.models.tools import create_server
        from assistant_runtime.mcp.client import LocalRuntimeClient
        from assistant_runtime.mcp.outcomes import ToolOutcomes

        self.gateway.script_fn = lambda n, b: (
            401,
            {"error": {"code": "invalid_api_key", "message": "synthetic credential rejected"}},
        )
        config = replace(
            Config.load(initialize(self.root / "ipc-instance", preset="slim")),
            workspace=str(self.root),
            main_model="model-a",
            main_reasoning_effort="low",
            model_api=configuration(self.gateway.base_url),
        )
        (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 20)
            remote = LocalRuntimeClient(config.socket)
            with self.assertRaises(ModelCallError) as caught:
                await remote.invoke(
                    ModelRequest(
                        "infer",
                        {
                            "request_id": str(uuid.uuid4()),
                            "model": "model-a",
                            "text": "synthetic auxiliary input",
                        },
                    )
                )
            self.assertEqual(caught.exception.failure["category"], "authentication")
            self.assertEqual(caught.exception.failure["provider_code"], "invalid_api_key")
            server = create_server(remote)
            server.add_middleware(ToolOutcomes())
            async with Client(server) as client:
                result = await client.call_tool(
                    "infer_with_model",
                    {
                        "request_id": str(uuid.uuid4()),
                        "model": "model-a",
                        "text": "synthetic MCP auxiliary input",
                    },
                    raise_on_error=False,
                )
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["tool_outcome"]["outcome"], "failed")
                self.assertEqual(
                    result.structured_content["received_result"]["failure"]["provider_code"],
                    "invalid_api_key",
                )
                self.assertEqual(len(self.gateway.requests), 2)
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 20)

    async def test_total_deadline_includes_native_backoff_without_reset(self):
        import time

        await self.infer()  # Warm the real SDK without including import time in the deadline.
        before = len(self.gateway.requests)
        self.client.settings = replace(self.settings, timeout_seconds=0.3)
        self.gateway.script_fn = lambda n, b: (503, {"error": {"code": "overloaded", "message": "busy"}})
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await self.infer()
        self.assertLess(time.monotonic() - started, 0.8)
        await asyncio.sleep(0.65)
        self.assertEqual(len(self.gateway.requests), before + 1)
        self.assertEqual((await self.usage.snapshot(self.scope.work_id))["inflight"], 0)
