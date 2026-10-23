"""Production host + main Pi + delegated Pi + real task MCP; synthetic model transport."""

import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService


class HostTest(unittest.IsolatedAsyncioTestCase):
    async def test_cli_headless_starts_real_web_models_and_metering(self):
        from aiohttp import ClientSession, CookieJar, FormData
        from assistant_runtime.app.cli import request

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            gateway = FakeResponsesGateway(lambda step, body: sse_text_reply("cli", "CLI_OK"))
            path = root / "config.toml"
            path.write_text(
                f"root={json.dumps(folder)}\nworkspace={json.dumps(folder)}\n"
                'profile="headless"\nbackend="pi"\nmain_model="model-a"\n'
                'main_reasoning_effort="low"\nmemory_enabled=false\n'
                'plugins=["ai_usage", "workfiles", "devices", "assistant", "conversation", "execution", "models","pi","voice_disabled","web"]\n'
                f'[web]\nenable=true\nhost="127.0.0.1"\nport={port}\n'
                "[model_api]\nenabled=true\n"
                f'gateway_url="{gateway.base_url}"\n'
                '[model_api.providers.local]\nprotocol="openai_responses"\n'
                f'base_url="{gateway.base_url}"\nkey_env="TEST_KEY"\nenabled=true\n'
                '[model_api.entries.model-a]\nprovider="local"\nmodel="model-a"\n'
                'execution_validated=true\nimages=true\nreasoning_efforts=["low"]\n'
            )
            (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            config = Config.load(path)
            with (root / "service.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "assistant_runtime.app.cli",
                    "--config",
                    str(path),
                    "run",
                    stdout=log,
                    stderr=log,
                )
                try:
                    async with asyncio.timeout(20):
                        while True:
                            self.assertIsNone(process.returncode, (root / "service.log").read_text())
                            try:
                                status = await request(config, {"command": "status"})
                                if status.get("plugins", {}).get("web", {}).get("state") == "ready":
                                    break
                            except (OSError, RuntimeError):
                                pass
                            await asyncio.sleep(0.05)
                    self.assertEqual(status["plugins"]["pi"]["state"], "ready")
                    self.assertEqual(status["plugins"]["ai_usage"]["state"], "ready")
                    origin = f"http://127.0.0.1:{port}"
                    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as web:
                        async with web.get(origin + "/api/session") as reply:
                            self.assertEqual(reply.status, 401)
                        async with web.post(
                            origin + "/api/login",
                            json={"token": (root / "state/web-access-token").read_text().strip()},
                            headers={"Origin": origin},
                        ) as reply:
                            self.assertEqual(reply.status, 200)
                        import base64

                        image = FormData()
                        image.add_field(
                            "image",
                            base64.b64decode(
                                "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP8z0AaYCJR/aiGUQ1DSAMAQC4BH2bjRnMAAAAASUVORK5CYII="
                            ),
                            filename="synthetic.png",
                            content_type="image/png",
                        )
                        async with web.post(
                            origin + "/api/uploads", data=image, headers={"Origin": origin}
                        ) as reply:
                            self.assertEqual(reply.status, 201)
                            uploaded = await reply.json()
                        async with web.post(
                            origin + "/api/messages",
                            json={
                                "request_id": str(uuid.uuid4()),
                                "text": "Reply CLI_OK",
                                "attachments": [uploaded["id"]],
                            },
                            headers={"Origin": origin},
                        ) as reply:
                            self.assertEqual(reply.status, 202)
                        async with asyncio.timeout(20):
                            while True:
                                async with web.get(origin + "/api/messages") as reply:
                                    body = await reply.json()
                                self.assertFalse(
                                    [
                                        m
                                        for m in body["messages"]
                                        if m["role"] == "assistant" and m["status"] == "failed"
                                    ],
                                    body,
                                )
                                if not body["state"]["task_running"] and any(
                                    m["role"] == "assistant" and m["text"] == "CLI_OK"
                                    for m in body["messages"]
                                ):
                                    break
                                await asyncio.sleep(0.05)
                        self.assertEqual(body["state"]["ai_usage"]["requests"], 1)
                        self.assertEqual(body["state"]["voice"], "disabled")
                        self.assertEqual(len(gateway.requests), 1)
                        self.assertIn("data:image/jpeg;base64,", json.dumps(gateway.requests[0]))
                finally:
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                    await gateway.aclose()

    async def test_real_task_fallback_preserves_shared_budget_and_executes_once(self):
        from assistant_runtime.api.contracts import TaskRequest

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "fallback.txt"

            def script(step, body):
                if body["model"] == "model-a":
                    return (402, {"error": {"message": "Synthetic exhausted credit"}})
                if step == 2:
                    return sse_tool_call("write", "write", {"path": str(target), "content": "once"})
                return sse_text_reply("done", "Completed")

            gateway = FakeResponsesGateway(script)
            (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            cfg = Config(
                root=root,
                workspace=folder,
                main_model="model-b",
                main_reasoning_effort="low",
                plugins=[
                    "ai_usage",
                    "workfiles",
                    "devices",
                    "assistant",
                    "conversation",
                    "execution",
                    "models",
                    "pi",
                    "voice_disabled",
                    "tasks",
                    "notifications",
                ],
                memory_enabled=False,
                model_api=configuration(gateway.base_url),
                tasks={
                    "model_policy": {
                        "profiles": {
                            name: {"model": "model-a", "reasoning_effort": "low"}
                            for name in ("fast", "balanced", "deep", "ultra")
                        },
                        "fallbacks": {"model-a": [{"model": "model-b", "reasoning_effort": "low"}]},
                    }
                },
            )
            host = AssistantService(cfg)
            serving = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 15)
                definition = await host.runtime.invoke(
                    TaskRequest(
                        "create",
                        {
                            "request_id": str(uuid.uuid4()),
                            "spec": {
                                "title": "Synthetic fallback",
                                "prompt": "write and verify " + str(target),
                                "notify_completion": False,
                            },
                        },
                    )
                )
                repo = host.runtime.plugins["tasks"].repo
                async with asyncio.timeout(20):
                    while True:
                        runs = repo.runs(definition["id"])
                        if runs and runs[0]["state"] in {"completed", "failed", "unknown"}:
                            break
                        await asyncio.sleep(0.05)
                run = runs[0]
                self.assertEqual(run["state"], "completed", run)
                self.assertEqual(target.read_text(), "once")
                self.assertEqual([r["model"] for r in gateway.requests], ["model-a", "model-b", "model-b"])
                self.assertEqual(run["data"]["ai_usage"]["requests"], 3)
                calls = await host.ai_usage.service.call(
                    "query", {"work_id": run["data"]["ai_usage"]["work_id"]}
                )
                self.assertEqual([r["http_status"] for r in reversed(calls["items"])], [402, 200, 200])

            finally:
                host.stop_event.set()
                await asyncio.wait_for(serving, 15)
                await gateway.aclose()

    async def test_web_text_delegates_other_model_and_preserves_result_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "result.txt"
            counts = {}
            background_done = threading.Event()
            task_request = str(uuid.uuid4())

            def script(step, body):
                model = body["model"]
                counts[model] = counts.get(model, 0) + 1
                if model == "model-a" and counts[model] == 1:
                    return sse_tool_call(
                        "discover",
                        "tool_search",
                        {"query": "create task request_id title prompt", "limit": 8},
                    )
                if model == "model-a" and counts[model] == 2:
                    return sse_tool_call(
                        "delegate",
                        "mcp__tasks__create_task",
                        {
                            "request_id": task_request,
                            "feedback_owner": "conversation",
                            "title": "Synthetic delegated task",
                            "prompt": "Write synthetic result to " + str(target),
                            "execution": {
                                "model": "model-b",
                                "reasoning_effort": "low",
                                "reason": "Known synthetic file write, verified by reading the saved file",
                                "model_source": "user",
                                "effort_source": "user",
                            },
                        },
                    )
                if model == "model-b" and counts[model] == 1:
                    return sse_tool_call("write", "write", {"path": str(target), "content": "completed once"})
                if model == "model-a":
                    background_done.wait(15)
                return sse_text_reply("final" + str(step), "已完成" if model == "model-b" else "已委派")

            gateway = FakeResponsesGateway(script)
            (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            cfg = Config(
                root=root,
                workspace=folder,
                plugins=[
                    "ai_usage",
                    "workfiles",
                    "devices",
                    "assistant",
                    "conversation",
                    "execution",
                    "models",
                    "pi",
                    "voice_disabled",
                    "tasks",
                    "notifications",
                ],
                memory_enabled=False,
                model_api=configuration(gateway.base_url),
                main_model="model-a",
                main_reasoning_effort="low",
                tasks={
                    "concurrency": 4,
                    "model_policy": {
                        "profiles": {
                            name: {"model": "model-b", "reasoning_effort": effort}
                            for name, effort in (
                                ("fast", "low"),
                                ("balanced", "medium"),
                                ("deep", "high"),
                                ("ultra", "max"),
                            )
                        }
                    },
                },
                notifications={"channels": ["chat"], "voice_enabled": False},
            )
            host = AssistantService(cfg)
            # No device is configured. Device observation needs no external command in this test.
            run = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 15)
                request_id = str(uuid.uuid4())
                first = await host.conversation.submit(request_id, "Run the authorized synthetic task", [])
                again = await host.conversation.submit(request_id, "Run the authorized synthetic task", [])
                self.assertEqual(first["message"]["id"], again["message"]["id"])
                started = asyncio.get_running_loop().time()
                async with asyncio.timeout(40):
                    while True:
                        if host.backend.last_error:
                            raise AssertionError(host.backend.last_error)
                        if host.conversation.sync_error:
                            raise AssertionError(host.conversation.sync_error)
                        tasks = host.runtime.plugins["tasks"].repo.definitions()
                        if tasks:
                            jobs = host.runtime.plugins["tasks"].repo.runs(tasks[0]["id"])
                            if (
                                jobs
                                and jobs[0]["state"] == "completed"
                                and host.store.db.execute(
                                    "SELECT COUNT(*) FROM chat_result_receipts"
                                ).fetchone()[0]
                            ):
                                background_done.set()
                                if not host.backend.busy:
                                    break
                        if host.runtime.fatal.is_set():
                            raise AssertionError("Host failed")
                        if asyncio.get_running_loop().time() - started > 20:
                            raise AssertionError(
                                {
                                    "counts": counts,
                                    "tool_results": [
                                        x
                                        for r in gateway.requests
                                        for x in r.get("input", [])
                                        if x.get("type") == "function_call_output"
                                    ],
                                    "requests": host.store.db.execute(
                                        "SELECT state FROM chat_requests"
                                    ).fetchall(),
                                    "tasks": tasks,
                                    "recent": host.store.executions.recent(),
                                    "chat": host.store.db.execute(
                                        "SELECT text FROM chat_messages"
                                    ).fetchall(),
                                }
                            )
                        await asyncio.sleep(0.05)
                self.assertEqual(target.read_text(), "completed once")
                self.assertEqual(len(tasks), 1)
                receipt = host.backend.executions.records.get(jobs[0]["data"]["execution_id"])
                self.assertEqual(receipt["state"], "completed")
                self.assertEqual(receipt["data"]["source"], "task")
                self.assertTrue(any(r["execution_id"] for r in host.store.executions.recent()))
                self.assertEqual(jobs[0]["data"]["spec"]["execution"]["model"], "model-b")
                self.assertTrue(host.conversation.state()["backend_ready"])
                self.assertFalse(host.assistant.voice.opened)
                self.assertEqual(
                    host.store.db.execute("SELECT COUNT(*) FROM chat_result_receipts").fetchone()[0], 1
                )
                self.assertEqual(host.notifications.repo.unread_count(), 0)
                self.assertEqual(counts, {"model-a": 3, "model-b": 2})
                child_scope = await host.ai_usage.service.binding(jobs[0]["id"])
                self.assertEqual(child_scope.work_id, host.backend.client.last_work_id)
                summary = await host.ai_usage.service.snapshot(child_scope.work_id)
                self.assertEqual(summary["requests"], len(gateway.requests))
                calls = await host.ai_usage.service.call("query", {"work_id": child_scope.work_id})
                self.assertEqual({r["caller"] for r in calls["items"]}, {"foreground", jobs[0]["id"]})
            finally:
                background_done.set()
                host.stop_event.set()
                await asyncio.wait_for(run, 15)
                await gateway.aclose()

    async def test_installed_extension_tool_reaches_pi_and_retains_work_identity(self):
        from types import SimpleNamespace
        from assistant_runtime.api import Manifest, PluginDefinition, Tool

        observed = []

        class Sample:
            def __init__(self, context):
                self.context = context

            async def start(self):
                pass

            async def save(self, arguments):
                observed.append(self.context.identity)
                self.context.set("saved", arguments["text"])
                return {"saved": arguments["text"]}

            def tools(self):
                schema = {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                }
                return (
                    Tool(
                        "save_sample",
                        "Save a synthetic sample",
                        schema,
                        {"type": "object"},
                        self.save,
                        read_only=False,
                        idempotent=True,
                    ),
                )

        definition = PluginDefinition(Manifest("sample"), Sample, lambda value: value)
        entry = SimpleNamespace(name="sample", load=lambda: definition)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def discover_extension(step, body):
                if step == 1:
                    return sse_tool_call("discover", "tool_search", {"query": "save_sample", "limit": 1})
                if step == 2:
                    return sse_tool_call(
                        "sample-call", "mcp__extensions__save_sample", {"text": "saved once"}
                    )
                return sse_text_reply("sample-done", "SAMPLE_DONE")

            gateway = FakeResponsesGateway(discover_extension)
            self.addCleanup(gateway.close)
            (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            config = Config(
                root=root,
                workspace=folder,
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
                plugins=[
                    "ai_usage",
                    "workfiles",
                    "devices",
                    "assistant",
                    "conversation",
                    "execution",
                    "models",
                    "pi",
                    "voice_disabled",
                    "sample",
                ],
            )
            with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
                host = AssistantService(config)
            serving = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 15)
                await host.conversation.submit(str(uuid.uuid4()), "Save the authorized synthetic sample", [])
                try:
                    async with asyncio.timeout(30):
                        while host.backend.busy or not observed:
                            await asyncio.sleep(0.05)
                except TimeoutError:
                    self.fail(
                        str(
                            {
                                "error": host.backend.last_error,
                                "progress": host.backend.progress,
                                "records": host.store.executions.recent(),
                                "tool_results": [
                                    item
                                    for item in gateway.requests[-1].get("input", [])
                                    if item.get("type") == "function_call_output"
                                ],
                            }
                        )
                    )
                self.assertEqual(host.store.get("plugin.sample.saved"), "saved once")
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0].work_id, host.backend.client.last_work_id)
                self.assertIsNone(observed[0].task_run)
                self.assertFalse(hasattr(host.runtime.plugins["sample"].instance.context, "runtime"))
                self.assertEqual(len(gateway.requests), 3)
                self.assertNotIn("save_sample", str(gateway.requests[0]["tools"]))
                self.assertIn("save_sample", str(gateway.requests[1]["tools"]))
            finally:
                host.stop_event.set()
                await asyncio.wait_for(serving, 10)
