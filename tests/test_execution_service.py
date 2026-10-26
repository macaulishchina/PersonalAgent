"""Single execution identity, cancellation races and real Pi integration."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.api.runs import ExecutionRequest
from assistant_runtime.app.store import Store
from assistant_runtime.core.execution.service import ExecutionService
from assistant_runtime.core.workfiles import WorkFiles
from assistant_runtime.kernel.runtime import Manifest, Runtime


class Usage:
    def __init__(self):
        self.scopes = {}

    async def bind(self, ident, scope):
        self.scopes[ident] = scope

    async def binding(self, ident):
        return self.scopes.get(ident)


class Client:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True)
        self.calls = []
        self.responses = []
        self.starting = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, message):
        self.responses.append(message)

    async def respond(self, ident, value):
        self.responses.append((ident, value))

    async def request(self, command, args, timeout=30):
        self.calls.append((command, args))
        if command == "models.list":
            return {
                "data": [
                    {
                        "id": "test",
                        "model": "test",
                        "supported_reasoning_efforts": [{"reasoning_effort": "low"}],
                    }
                ]
            }
        if command == "session.create":
            return {"session": {"id": uuid.uuid4().hex}}
        if command == "run.start":
            self.starting.set()
            await self.release.wait()
            return {"run": {"id": "turn"}}
        return {}


class ExecutionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.client, self.usage = Client(), Usage()
        self.files = WorkFiles(self.root, self.store.db)
        self.service = ExecutionService(
            self.client,
            self.store.db,
            self.files,
            self.usage,
            default_model="test",
            default_effort="low",
            servers=lambda: {},
            concurrency=1,
            queue_size=1,
        )
        self.runtime = Runtime(self.store)
        self.runtime.add(SimpleNamespace(manifest=Manifest("execution"), start=self.service.start))
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    async def submit(self, **extra):
        args = {"request_id": str(uuid.uuid4()), "text": "synthetic", **extra}
        return await self.service.request(ExecutionRequest("submit", args)), args

    async def event(self, ident, kind, **data):
        row = self.service.records.get(ident)
        return await self.service.event(
            {"kind": kind, "data": {"session_id": row["data"]["session_id"], **data}}
        )

    async def test_duplicate_conflict_queue_bound_and_queued_cancel(self):
        first, args = await self.submit()
        await self.client.starting.wait()
        duplicate = await self.service.submit(args)
        self.assertEqual(first["id"], duplicate["id"])
        with self.assertRaisesRegex(ValueError, "different content"):
            await self.service.submit({**args, "text": "changed"})
        queued, _ = await self.submit()
        with self.assertRaisesRegex(RuntimeError, "queue is full"):
            await self.submit()
        result = await self.service.request(ExecutionRequest("cancel", {"id": queued["id"]}))
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(len([c for c in self.client.calls if c[0] == "run.start"]), 1)

    async def test_parent_budget_identity_is_shared_and_cross_caller_query_rejected(self):
        token = current_usage.set(UsageScope("parent-work", "parent", "task"))
        try:
            row, _ = await self.submit()
            self.assertEqual(self.usage.scopes[row["id"]].work_id, "parent-work")
            self.assertEqual(self.usage.scopes[row["id"]].caller, row["id"])
        finally:
            current_usage.reset(token)
        token = current_usage.set(UsageScope("other-work", "other"))
        try:
            with self.assertRaisesRegex(ValueError, "another caller"):
                await self.service.request(ExecutionRequest("get", {"id": row["id"]}))
        finally:
            current_usage.reset(token)

    async def test_dispatch_cancel_not_lost_to_run_started_and_late_completion_ignored(self):
        row, _ = await self.submit()
        await self.client.starting.wait()
        ident = row["id"]
        self.assertEqual(
            (await self.service.request(ExecutionRequest("cancel", {"id": ident})))["state"], "cancelling"
        )
        await self.event(ident, "run.started", run={"id": "turn"})
        self.assertEqual(self.service.records.get(ident)["state"], "cancelling")
        self.client.release.set()
        async with asyncio.timeout(2):
            while not any(c[0] == "run.cancel" for c in self.client.calls):
                await asyncio.sleep(0.001)
        await self.event(ident, "run.completed", run={"id": "turn", "status": "interrupted"})
        await self.event(ident, "run.completed", run={"id": "turn", "status": "completed"})
        self.assertEqual(self.service.records.get(ident)["state"], "cancelled")

    async def test_question_identity_and_terminal_result_do_not_require_chat(self):
        row, _ = await self.submit()
        await self.client.starting.wait()
        ident = row["id"]
        await self.event(ident, "run.started", run={"id": "turn"})
        self.client.release.set()
        session = self.service.records.get(ident)["data"]["session_id"]
        await self.service.event(
            {
                "id": 8,
                "kind": "input.requested",
                "data": {
                    "session_id": session,
                    "run_id": "turn",
                    "questions": [{"id": "q", "question": "Which input?"}],
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            await self.service.request(
                ExecutionRequest("answer", {"id": ident, "question_id": 9, "text": "one"})
            )
        await self.service.request(ExecutionRequest("answer", {"id": ident, "question_id": 8, "text": "one"}))
        self.assertEqual(self.client.responses, [(8, {"answers": {"q": {"answers": ["one"]}}})])
        await self.event(
            ident,
            "run.completed",
            run={
                "id": "turn",
                "status": "completed",
                "items": [
                    {"type": "message", "text": "progress", "phase": "commentary"},
                    {"type": "message", "text": "RESULT"},
                ],
            },
        )
        result = await self.service.request(ExecutionRequest("get", {"id": ident}))
        self.assertEqual((result["state"], result["result"]), ("completed", "RESULT"))
        self.assertEqual(result["questions"], [])
        self.assertIsNone(result["question_id"])
        events = await self.service.request(ExecutionRequest("events", {"id": ident}))
        self.assertTrue(events["items"])
        self.assertEqual(self.service.records.events(ident, events["cursor"])["items"], [])

    async def test_recovery_never_dispatches_old_queued_or_active_runs(self):
        first, _ = await self.submit()
        await self.client.starting.wait()
        queued, _ = await self.submit()
        await self.runtime.close()
        self.assertEqual(self.service.records.get(first["id"])["state"], "unknown")
        self.assertEqual(self.service.records.get(queued["id"])["state"], "interrupted")
        self.service.records.recover()
        self.assertFalse(self.service.records.active())
        self.assertEqual(len([c for c in self.client.calls if c[0] == "run.start"]), 1)

    async def test_lost_start_response_cancels_owned_session_without_resubmission(self):
        original = self.client.request

        async def lost_response(command, args, timeout=30):
            result = await original(command, args, timeout)
            if command == "run.start":
                raise ConnectionError("response lost")
            return result

        self.client.request = lost_response
        self.client.release.set()
        row, args = await self.submit()
        async with asyncio.timeout(2):
            while self.service.records.get(row["id"])["state"] != "unknown":
                await asyncio.sleep(0.001)
            while not any(c[0] == "session.close" for c in self.client.calls):
                await asyncio.sleep(0.001)
        self.assertTrue(any(c[0] == "run.cancel" for c in self.client.calls))
        self.assertEqual((await self.service.submit(args))["state"], "unknown")
        self.assertEqual(len([c for c in self.client.calls if c[0] == "run.start"]), 1)

    async def test_resolved_selection_mismatch_fails_before_paid_dispatch(self):
        from unittest.mock import patch

        original = self.client.request

        async def request(command, args, timeout=30):
            result = await original(command, args, timeout)
            if command == "session.create":
                result.update(model="unexpected-model", reasoning_effort="high")
            return result

        with patch.object(self.client, "request", request):
            row, _ = await self.submit()
            await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertEqual(self.service.records.get(row["id"])["state"], "failed")
        self.assertFalse(any(c[0] == "run.start" for c in self.client.calls))
        self.assertTrue(any(c[0] == "session.close" for c in self.client.calls))

    async def test_unsupported_executor_question_returns_explicit_error(self):
        row, _ = await self.submit()
        await self.client.starting.wait()
        session = self.service.records.get(row["id"])["data"]["session_id"]
        handled = await self.service.event(
            {"id": 99, "kind": "future.requested", "data": {"session_id": session}}
        )
        self.assertTrue(handled)
        self.assertEqual(self.client.responses[-1]["error"]["code"], -32601)
        self.assertEqual(self.client.responses[-1]["id"], 99)

    async def test_shutdown_during_admission_cannot_enqueue_after_close(self):
        from unittest.mock import patch

        entered, release = asyncio.Event(), asyncio.Event()
        original = self.usage.bind

        async def bind(ident, scope):
            entered.set()
            await release.wait()
            await original(ident, scope)

        with patch.object(self.usage, "bind", bind):
            pending = asyncio.create_task(self.submit())
            await entered.wait()
            await self.service.close()
            release.set()
            with self.assertRaisesRegex(RuntimeError, "stopping"):
                await pending
        self.assertFalse(self.service.records.active())
        self.assertTrue(self.service.queue.empty())
        self.assertFalse(any(c[0] == "session.create" for c in self.client.calls))

    async def test_disconnect_during_session_creation_never_starts_run(self):
        from unittest.mock import patch

        entered, release = asyncio.Event(), asyncio.Event()
        original = self.client.request

        async def request(command, args, timeout=30):
            if command == "session.create":
                entered.set()
                await release.wait()
            return await original(command, args, timeout)

        with patch.object(self.client, "request", request):
            row, _ = await self.submit()
            await entered.wait()
            await self.service.disconnected()
            release.set()
            await asyncio.wait_for(self.service.queue.join(), 2)
        self.assertEqual(self.service.records.get(row["id"])["state"], "unknown")
        self.assertFalse(any(c[0] == "run.start" for c in self.client.calls))
        self.assertTrue(any(c[0] == "session.close" for c in self.client.calls))

    async def test_disconnect_settles_active_and_queued_without_replay(self):
        active, _ = await self.submit()
        await self.client.starting.wait()
        queued, _ = await self.submit()
        await self.service.disconnected()
        self.assertEqual(self.service.records.get(active["id"])["state"], "unknown")
        self.assertEqual(self.service.records.get(queued["id"])["state"], "interrupted")
        self.assertEqual(
            self.service.records.get(active["id"])["data"]["error"]["code"], "executor_disconnected"
        )
        with self.assertRaisesRegex(RuntimeError, "stopping"):
            await self.submit()
        self.client.release.set()
        await asyncio.sleep(0)
        self.assertEqual(len([c for c in self.client.calls if c[0] == "run.start"]), 1)

    async def test_disabled_model_and_forged_identity_rejected_before_dispatch(self):
        self.client.settings.enabled = False
        with self.assertRaisesRegex(RuntimeError, "configuration required"):
            await self.submit()
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            await self.submit(caller="forged")
        self.assertFalse(self.client.calls)


class ExecutionIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_pi_execution_without_foreground_or_device_services(self):
        from assistant_runtime.app.config import Config
        from assistant_runtime.core.pi.plugin import PiPlugin
        from assistant_runtime.core.ai_usage.service import UsageService
        from pi_gateway import FakeResponsesGateway, sse_text_reply
        from test_pi_skeleton import configuration

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = FakeResponsesGateway(lambda step, body: sse_text_reply("done", "BASE_OK"))
            (root / ".env").write_text("TEST_KEY=synthetic-key\n")
            config = Config(
                root=root,
                workspace=str(root),
                plugins=["ai_usage", "models", "pi"],
                model_api=configuration(gateway.base_url),
            )
            store = Store(root / "execution.sqlite3")
            usage = UsageService(root / "usage")
            pi = PiPlugin(config, store, usage=usage, foreground=False)
            backend = pi.backend
            service = ExecutionService(
                backend.client,
                store.db,
                WorkFiles(root, store.db),
                usage,
                default_model="model-a",
                default_effort="low",
                servers=lambda: {},
            )
            runtime = Runtime(store)

            async def start_usage(context):
                context.own(usage.close)
                await usage.start()

            runtime.add(SimpleNamespace(manifest=Manifest("ai_usage"), start=start_usage))
            runtime.add(pi)
            runtime.add(SimpleNamespace(manifest=Manifest("execution", ("pi",)), start=service.start))
            try:
                backend.executions = service
                await runtime.start()
                self.assertIsNone(backend.session_id)
                self.assertIsNone(backend.execution_session_id)
                self.assertNotIn("executions", store.__dict__)
                self.assertFalse(gateway.requests)
                with self.assertRaisesRegex(RuntimeError, "Conversation capability"):
                    await backend.submit("not a conversation")
                row = await runtime.invoke(
                    ExecutionRequest("submit", {"request_id": str(uuid.uuid4()), "text": "Return BASE_OK"})
                )
                async with asyncio.timeout(20):
                    while row["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.05)
                        row = await runtime.invoke(ExecutionRequest("get", {"id": row["id"]}))
                self.assertEqual(row["state"], "completed", row)
                self.assertIn("BASE_OK", row["result"])
                self.assertTrue(row["usage"])
                self.assertNotIn("executions", store.__dict__)
                tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertFalse(tables & {"messages", "tasks", "job_definitions", "notifications"})
            finally:
                await runtime.close()
                store.db.close()
                await gateway.aclose()

    async def test_slim_execution_inherits_external_mcp_and_background_context_scope(self):
        import sys
        from dataclasses import replace
        from assistant_runtime.api import ExecutionRequest, CallIdentity
        from assistant_runtime.api.plugins import current_call
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.initialize import initialize
        from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
        from test_pi_skeleton import configuration

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "external.py"
            script.write_text(
                "from fastmcp import FastMCP\nm=FastMCP('sample')\n@m.tool()\ndef ping()->str:return 'EXTERNAL_OK'\nm.run(transport='stdio',show_banner=False)\n"
            )

            def model(step, body):
                if step == 1:
                    return sse_tool_call(
                        "discover", "tool_search", {"query": "external_sample ping", "limit": 1}
                    )
                if step == 2:
                    return sse_tool_call("external", "mcp__external_sample__ping", {})
                self.assertIn("EXTERNAL_OK", str(body))
                return sse_text_reply("done", "EXTERNAL_OK")

            gateway = FakeResponsesGateway(model)
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                model_api=configuration(gateway.base_url),
                main_model="model-a",
                main_reasoning_effort="low",
            )
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            host = AssistantService(config)
            host.mcp_registry.configure(
                "sample",
                {
                    "transport": {"type": "stdio", "command": sys.executable, "args": [str(script)]},
                    "contexts": ["foreground"],
                    "approval": "approve",
                },
                0,
            )
            running = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                token = current_call.set(CallIdentity("bound", "work", task_run="task-run"))
                try:
                    scoped = host.runtime.plugins["execution"].servers()
                    self.assertFalse(scoped["external_sample"]["enabled"])
                    self.assertIn("--task-run", scoped["tasks"]["args"])
                finally:
                    current_call.reset(token)
                execution = await host.runtime.invoke(
                    ExecutionRequest(
                        "submit",
                        {
                            "request_id": str(uuid.uuid4()),
                            "text": "Synthetic external MCP",
                            "tools": ["mcp__external_sample__ping"],
                            "skills": [],
                        },
                    )
                )
                async with asyncio.timeout(25):
                    while execution["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.05)
                        execution = await host.runtime.invoke(
                            ExecutionRequest("get", {"id": execution["id"]})
                        )
                self.assertEqual(execution["state"], "completed", execution)
                self.assertEqual(len(gateway.requests), 3)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                await gateway.aclose()

    async def test_real_pi_submission_through_shared_cli_transport_and_restart_receipt(self):
        from assistant_runtime.api.system import system

        from assistant_runtime.core.plugin_catalog import platforms

        if system.name == "windows" and "windows" not in platforms("voice_disabled"):
            from assistant_runtime.app.initialize import initialize

            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / "standard"
                with self.assertRaisesRegex(ValueError, "voice_disabled.*slim"):
                    initialize(root, preset="standard")
                self.assertFalse(root.exists())
            return
        await self.exercise_host("standard")

    async def test_slim_preset_real_pi_cli_clarification_artifact_cancel_and_restart(self):
        await self.exercise_host("slim")

    async def exercise_host(self, preset):
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.cli import request
        from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
        from test_pi_skeleton import configuration

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def script(step, body):
                if step == 1:
                    return sse_tool_call(
                        "ask", "request_user_input", {"questions": [{"id": "q", "question": "Which report?"}]}
                    )
                if step == 2:
                    return sse_tool_call(
                        "write", "write", {"path": "../artifacts/report.txt", "content": "SYNTHETIC"}
                    )
                return sse_text_reply(f"message{step}", "RUN_OK")

            gateway = FakeResponsesGateway(script)
            (root / ".env").write_text("TEST_KEY=synthetic-key\n")
            from dataclasses import replace
            from assistant_runtime.app.initialize import initialize

            config = replace(
                Config.load(initialize(root / "instance", preset=preset)),
                workspace=str(root),
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
                web={"enable": False},
            )
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            config = replace(config, instance_skills=["selected", "omitted"])
            for name in config.instance_skills:
                folder = config.root / "config/skills" / name
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: SKILL_{name.upper()}_MARKER\n---\nSynthetic skill.\n"
                )
            host = AssistantService(config)
            task = asyncio.create_task(host.run())
            args = {"request_id": str(uuid.uuid4()), "text": "Reply RUN_OK. Do not use tools."}
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                submitted = await request(
                    config, {"command": "executions", "action": "submit", "arguments": args}
                )
                async with asyncio.timeout(30):
                    while True:
                        row = await request(
                            config,
                            {"command": "executions", "action": "get", "arguments": {"id": submitted["id"]}},
                        )
                        if row["state"] == "waiting_user":
                            await request(
                                config,
                                {
                                    "command": "executions",
                                    "action": "answer",
                                    "arguments": {
                                        "id": row["id"],
                                        "question_id": row["question_id"],
                                        "text": "Synthetic report",
                                    },
                                },
                            )
                        if row["state"] in {"completed", "failed", "unknown"}:
                            break
                        await asyncio.sleep(0.05)
                self.assertEqual(row["state"], "completed", row)
                self.assertIn("RUN_OK", row["result"])
                self.assertTrue(row["usage"])
                self.assertEqual(row["artifacts"], [{"name": "report.txt", "bytes": 9}])
                duplicate = await request(
                    config, {"command": "executions", "action": "submit", "arguments": args}
                )
                self.assertEqual(duplicate["state"], "completed")
                self.assertFalse(host.backend.busy)
                if preset == "standard":
                    self.assertFalse(host.store.executions.recent())
                else:
                    self.assertFalse(host.store.executions.recent())
                    self.assertIn("conversation", host.runtime.plugins)
                    self.assertIsNone(host.assistant)
                    self.assertNotIn("devices", host.runtime.plugins)
                from unittest.mock import AsyncMock, patch

                rejected = asyncio.Event()

                async def reject(message):
                    self.assertEqual(message["id"], 98765)
                    rejected.set()

                with patch.object(host.backend.client, "send", AsyncMock(side_effect=reject)):
                    await host.backend.client.events.put(
                        {
                            "id": 98765,
                            "kind": "input.requested",
                            "data": {
                                "session_id": "expired-execution",
                                "questions": [{"id": "late", "question": "Late question"}],
                            },
                        }
                    )
                    await asyncio.wait_for(rejected.wait(), 2)
                self.assertIsNone(host.backend.pending_question)
                gateway.script_fn = lambda step, body: sse_tool_call("wait", "bash", {"command": "sleep 30"})
                waiting = await request(
                    config,
                    {
                        "command": "executions",
                        "action": "submit",
                        "arguments": {"request_id": str(uuid.uuid4()), "text": "Synthetic cancellation"},
                    },
                )
                async with asyncio.timeout(20):
                    while True:
                        events = await request(
                            config,
                            {"command": "executions", "action": "events", "arguments": {"id": waiting["id"]}},
                        )
                        if any(
                            e["kind"] == "activity.started"
                            and e["data"].get("item", {}).get("type") == "command"
                            for e in events["items"]
                        ):
                            break
                        await asyncio.sleep(0.05)
                cancelled = await request(
                    config, {"command": "executions", "action": "cancel", "arguments": {"id": waiting["id"]}}
                )
                self.assertEqual(cancelled["state"], "cancelled", cancelled)

                # Same public resource and execution contracts in both presets.
                import base64
                import json

                resource = await request(
                    config,
                    {
                        "command": "files",
                        "action": "put",
                        "arguments": {
                            "name": "source.txt",
                            "data": base64.b64encode(b"INPUT_SNAPSHOT_OK").decode(),
                        },
                    },
                )
                input_ids = [resource["id"]]
                if preset == "standard":
                    import io
                    from PIL import Image

                    image_data = io.BytesIO()
                    Image.new("RGB", (4, 4), "red").save(image_data, format="PNG")
                    image_ref = await request(
                        config,
                        {
                            "command": "files",
                            "action": "put",
                            "arguments": {
                                "name": "red.png",
                                "kind": "image",
                                "data": base64.b64encode(image_data.getvalue()).decode(),
                            },
                        },
                    )
                    input_ids.append(image_ref["id"])
                scoped_id = str(uuid.uuid4())
                input_path = (
                    host.workfiles.root
                    / "executions"
                    / scoped_id
                    / "scratch"
                    / ("input-" + resource["id"] + ".txt")
                )
                forbidden = root / "must-not-exist.txt"
                first_step = gateway.step
                first_request = len(gateway.requests)

                def scoped_script(step, body):
                    if step == first_step + 1:
                        return sse_tool_call("read-input", "read", {"path": str(input_path)})
                    if step == first_step + 2:
                        return sse_tool_call(
                            "outside-scope", "write", {"path": str(forbidden), "content": "NO"}
                        )
                    return sse_text_reply("scope-result", "SCOPE_OK")

                gateway.script_fn = scoped_script
                scoped = await request(
                    config,
                    {
                        "command": "executions",
                        "action": "submit",
                        "arguments": {
                            "request_id": scoped_id,
                            "text": "Inspect the registered input.",
                            "inputs": input_ids,
                            "tools": ["read"],
                            "skills": ["selected"],
                        },
                    },
                )
                async with asyncio.timeout(25):
                    while scoped["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.05)
                        scoped = await request(
                            config, {"command": "executions", "action": "get", "arguments": {"id": scoped_id}}
                        )
                self.assertEqual(scoped["state"], "completed", scoped)
                self.assertFalse(forbidden.exists())
                self.assertEqual(scoped["inputs"][0]["sha256"], resource["sha256"])
                calls = gateway.requests[first_request:]
                self.assertEqual([t["name"] for t in calls[0].get("tools", [])], ["read"])
                self.assertIn("INPUT_SNAPSHOT_OK", json.dumps(calls[-1]))
                self.assertIn("SKILL_SELECTED_MARKER", json.dumps(calls[0]))
                self.assertNotIn("SKILL_OMITTED_MARKER", json.dumps(calls[0]))
                if preset == "standard":
                    self.assertIn("input_image", json.dumps(calls[0]))
            finally:
                host.stop_event.set()
                await asyncio.wait_for(task, 20)
                await gateway.aclose()
            reopened = Store(config.state_dir / "assistant.sqlite3")
            try:
                from assistant_runtime.core.execution.records import RunRecords

                records = RunRecords(reopened.db)
                records.recover()
                self.assertEqual(records.get(submitted["id"])["state"], "completed")
            finally:
                reopened.db.close()
