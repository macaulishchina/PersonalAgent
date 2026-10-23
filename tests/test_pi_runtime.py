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
from unittest.mock import patch, AsyncMock

from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration
from assistant_runtime.config import Config
from assistant_runtime.assistant.service import AssistantService


class HostTest(unittest.IsolatedAsyncioTestCase):
    async def test_cli_headless_starts_real_web_models_and_metering(self):
        from aiohttp import ClientSession, CookieJar
        from assistant_runtime.cli import request

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
                'plugins=["models","pi","voice_disabled","pipewire","web"]\n'
                f'[web]\nenable=true\nhost="127.0.0.1"\nport={port}\n'
                "[model_api]\nenabled=true\n"
                f'gateway_url="{gateway.base_url}"\n'
                '[model_api.providers.local]\nprotocol="openai_responses"\n'
                f'base_url="{gateway.base_url}"\nkey_env="TEST_KEY"\nenabled=true\n'
                '[model_api.entries.model-a]\nprovider="local"\nmodel="model-a"\n'
                'execution_validated=true\nreasoning_efforts=["low"]\n'
            )
            (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            config = Config.load(path)
            with (root / "service.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "assistant_runtime.cli",
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
                        async with web.post(
                            origin + "/api/messages",
                            json={"request_id": str(uuid.uuid4()), "text": "Reply CLI_OK"},
                            headers={"Origin": origin},
                        ) as reply:
                            self.assertEqual(reply.status, 202)
                        async with asyncio.timeout(20):
                            while True:
                                async with web.get(origin + "/api/messages") as reply:
                                    body = await reply.json()
                                if not body["state"]["task_running"] and any(
                                    m["role"] == "assistant" and m["text"] == "CLI_OK"
                                    for m in body["messages"]
                                ):
                                    break
                                await asyncio.sleep(0.05)
                        self.assertEqual(body["state"]["ai_usage"]["requests"], 1)
                        self.assertEqual(body["state"]["voice"], "disabled")
                        self.assertEqual(len(gateway.requests), 1)
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
        from assistant_runtime.kernel.contracts import TaskRequest

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
                plugins=["models", "pi", "voice_disabled", "pipewire", "tasks", "notifications"],
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
            with patch.object(host.route, "ensure", new=AsyncMock()):
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
                    self.assertEqual(
                        [r["model"] for r in gateway.requests], ["model-a", "model-b", "model-b"]
                    )
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
                plugins=["models", "pi", "voice_disabled", "pipewire", "tasks", "notifications"],
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
            with patch.object(host.route, "ensure", new=AsyncMock()):
                run = asyncio.create_task(host.run())
                try:
                    await asyncio.wait_for(host.runtime.ready.wait(), 15)
                    request_id = str(uuid.uuid4())
                    first = await host.conversation.submit(
                        request_id, "Run the authorized synthetic task", []
                    )
                    again = await host.conversation.submit(
                        request_id, "Run the authorized synthetic task", []
                    )
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
                                        "recent": host.store.recent(),
                                        "chat": host.store.db.execute(
                                            "SELECT text FROM chat_messages"
                                        ).fetchall(),
                                    }
                                )
                            await asyncio.sleep(0.05)
                    self.assertEqual(target.read_text(), "completed once")
                    self.assertEqual(len(tasks), 1)
                    self.assertEqual(jobs[0]["data"]["spec"]["execution"]["model"], "model-b")
                    self.assertTrue(host.conversation.state()["backend_ready"])
                    self.assertFalse(host.voice.opened)
                    self.assertEqual(
                        host.store.db.execute("SELECT COUNT(*) FROM chat_result_receipts").fetchone()[0], 1
                    )
                    self.assertEqual(host.notifications.repo.unread_count(), 0)
                    self.assertEqual(counts, {"model-a": 2, "model-b": 2})
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
