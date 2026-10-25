"""Real Pi, native private IPC and detached manager acceptance in isolated instances."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from assistant_runtime.app.config import Config
from assistant_runtime.app.initialize import initialize
from assistant_runtime.app.host import AssistantService
from assistant_runtime.app.cli import request
from assistant_runtime.core.cli.client import Chat
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration, shell_call


class ConversationNativeTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_model_setup_activates_slim_without_restarting(self):
        from dataclasses import asdict
        from unittest.mock import patch
        import os
        import tomlkit

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            root = Path(folder).resolve()
            gateway = FakeResponsesGateway(lambda step, body: sse_text_reply("first-setup", "READY"))
            config = Config.load(initialize(root / "instance", preset="slim"))
            host = AssistantService(config)
            running = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                self.assertFalse(host.backend.client.started)
                candidate = asdict(config)
                candidate.update(
                    root=str(config.root),
                    main_model="model-a",
                    main_reasoning_effort="low",
                    model_api=configuration(gateway.base_url),
                )
                revision = "first-setup"
                generation = config.state_dir / "configuration/generations" / revision
                generation.mkdir(parents=True)
                entry = generation / "config.toml"
                entry.write_text(tomlkit.dumps(candidate), encoding="utf-8")
                (generation / "resources.json").write_text("{}", encoding="utf-8")
                # A missing credential fails safely and preserves the unconfigured running instance.
                with self.assertRaises(ValueError):
                    await host.configuration.apply(entry, revision)
                self.assertFalse(host.backend.client.started)
                self.assertFalse(host.config.model_api["enabled"])
                (config.root / ".env").write_text(
                    "ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n", encoding="utf-8"
                )
                result = await host.configuration.apply(entry, revision)
                self.assertEqual(result["phase"], "applied", result)
                terminal = Chat(
                    lambda action, arguments: request(
                        config, {"command": "conversation", "action": action, "arguments": arguments}
                    ),
                    output=lambda text: None,
                )
                await terminal.select()
                await terminal.command("FIRST_MODEL_SETUP")
                async with asyncio.timeout(20):
                    while (
                        await request(
                            config,
                            {
                                "command": "conversation",
                                "action": "queue",
                                "arguments": {"session_id": terminal.session},
                            },
                        )
                    )["requests"]:
                        await asyncio.sleep(0.02)
                self.assertEqual(len(gateway.requests), 1)
                self.assertFalse(running.done())
                self.assertNotIn("assistant", host.runtime.plugins)
                self.assertNotIn("devices", host.runtime.plugins)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                gateway.close()

    async def test_model_update_keeps_native_conversation_context_and_process(self):
        from dataclasses import asdict
        from copy import deepcopy
        from unittest.mock import patch
        import os
        import tomlkit
        import threading

        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ):
            root = Path(folder).resolve()
            proceed = threading.Event()

            def response(step, body):
                if step == 2:
                    if not proceed.wait(20):
                        raise TimeoutError("configuration boundary test did not release response")
                return sse_text_reply("answer" + str(step), "OK")

            gateway = FakeResponsesGateway(response)
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                workspace=str(root),
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
            )
            (config.root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            host = AssistantService(config)
            running = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                terminal = Chat(
                    lambda action, arguments: request(
                        config, {"command": "conversation", "action": action, "arguments": arguments}
                    ),
                    output=lambda text: None,
                )
                await terminal.select()
                ident = terminal.session

                async def idle():
                    async with asyncio.timeout(20):
                        while (
                            await request(
                                config,
                                {
                                    "command": "conversation",
                                    "action": "queue",
                                    "arguments": {"session_id": ident},
                                },
                            )
                        )["requests"]:
                            await asyncio.sleep(0.02)

                await terminal.command("BEFORE_CONFIGURATION_UPDATE")
                await idle()
                chat = await host.conversations.get(ident)
                execution_id = chat.backend.execution_session_id
                candidate = asdict(host.config)
                candidate["root"] = str(config.root)
                candidate["model_api"]["entries"]["model-b"] = deepcopy(
                    candidate["model_api"]["entries"]["model-a"]
                )
                candidate["model_api"]["entries"]["model-b"]["model"] = "model-b"
                candidate["main_model"] = "model-b"
                revision = "native-configuration-update"
                generation = config.state_dir / "configuration/generations" / revision
                generation.mkdir(parents=True)
                entry = generation / "config.toml"
                entry.write_text(tomlkit.dumps(candidate), encoding="utf-8")
                (generation / "resources.json").write_text("{}", encoding="utf-8")
                await terminal.command("DURING_CONFIGURATION_UPDATE")
                async with asyncio.timeout(10):
                    while len(gateway.requests) < 2:
                        await asyncio.sleep(0.02)
                waiting = await host.configuration.apply(entry, revision)
                self.assertEqual(waiting["phase"], "waiting", waiting)
                self.assertEqual(host.config.main_model, "model-a")
                proceed.set()
                await idle()
                result = await host.configuration.apply(entry, revision)
                self.assertEqual(result["phase"], "applied", result)
                await terminal.command("AFTER_CONFIGURATION_UPDATE")
                await idle()
                self.assertEqual(chat.backend.execution_session_id, execution_id)
                self.assertEqual(len(gateway.requests), 3)
                self.assertEqual(gateway.requests[1]["model"], "model-a")
                self.assertIn("BEFORE_CONFIGURATION_UPDATE", json.dumps(gateway.requests[2]))
                self.assertIn("DURING_CONFIGURATION_UPDATE", json.dumps(gateway.requests[2]))
                self.assertIn("AFTER_CONFIGURATION_UPDATE", json.dumps(gateway.requests[2]))
                self.assertEqual(gateway.requests[2]["model"], "model-b")
                self.assertFalse(running.done())
            finally:
                proceed.set()
                host.stop_event.set()
                await asyncio.wait_for(running, 15)
                gateway.close()

    async def test_real_tools_question_shared_admission_supplement_and_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "result.txt"

            def script(step, body):
                if step == 1:
                    return sse_tool_call("write", "write", {"path": str(target), "content": "verified"})
                if step == 2:
                    return sse_tool_call(
                        "ask", "request_user_input", {"questions": [{"id": "q", "question": "Where?"}]}
                    )
                if step == 4:
                    return shell_call("wait", "sleep 30", "Start-Sleep -Seconds 30")
                return sse_text_reply("reply" + str(step), "DONE")

            gateway = FakeResponsesGateway(script)
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                workspace=str(root),
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
                execution={"concurrency": 1, "queue_size": 8},
            )
            (config.root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            host = AssistantService(config)
            running = asyncio.create_task(host.run())

            async def call(action, **arguments):
                return await request(
                    config, {"command": "conversation", "action": action, "arguments": arguments}
                )

            async def wait(predicate):
                async with asyncio.timeout(20):
                    while not predicate():
                        await asyncio.sleep(0.02)

            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                a, b = [(await call("create"))["id"] for _ in range(2)]
                first, blocked, sleeping = [str(uuid.uuid4()) for _ in range(3)]
                await call("submit", session_id=a, request_id=first, text="Write then ask")
                ca = await host.conversations.get(a)
                await wait(lambda: ca.backend.pending_question)
                self.assertEqual(target.read_text(), "verified")
                question = str(ca.backend.pending_question["id"])
                await call("submit", session_id=b, request_id=blocked, text="MUST_NOT_DISPATCH")
                cb = await host.conversations.get(b)
                await wait(lambda: cb.backend.busy)
                self.assertEqual(len(gateway.requests), 2)
                await call("cancel", session_id=b, request_id=blocked)
                await wait(lambda: not cb.queue())
                self.assertEqual(cb.store.get("cli:" + blocked)["status"], "cancelled")
                with self.assertRaises(Exception):
                    await call("answer", session_id=a, request_id=first, question_id="stale", text="bad")
                self.assertIsNotNone(ca.backend.pending_question)
                await call(
                    "answer", session_id=a, request_id=first, question_id=question, text="SCRATCH_ANSWER"
                )
                await wait(lambda: not ca.queue())
                self.assertIn("SCRATCH_ANSWER", json.dumps(gateway.requests[-1]))
                self.assertEqual(len(gateway.requests), 3)
                await call("submit", session_id=a, request_id=sleeping, text="Wait for cancellation")
                await wait(lambda: bool(ca.backend.command_items.get(ca.backend.active_turn)))
                await call("supplement", session_id=a, request_id=sleeping, text="EXPLICIT_SUPPLEMENT")
                await call("cancel", session_id=a, request_id=sleeping)
                await wait(lambda: not ca.queue())
                state = host.store.db.execute(
                    "SELECT state FROM conversation_queue WHERE id=?", (sleeping,)
                ).fetchone()[0]
                self.assertEqual(state, "interrupted")
                self.assertNotIn("MUST_NOT_DISPATCH", json.dumps(gateway.requests))
                self.assertEqual(len(gateway.requests), 4)
                history = await call("history", session_id=a)
                self.assertTrue(any(m["text"] == "EXPLICIT_SUPPLEMENT" for m in history["messages"]))
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                await gateway.aclose()

    async def test_real_pi_multi_turn_isolation_queue_cli_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = FakeResponsesGateway(
                lambda step, body: sse_text_reply(f"message{step}", f"REPLY_{step}")
            )
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                workspace=str(root),
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
            )
            (config.root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            host = AssistantService(config)
            running = asyncio.create_task(host.run())

            async def invoke(action, arguments):
                return await request(
                    config, {"command": "conversation", "action": action, "arguments": arguments}
                )

            async def idle(session):
                async with asyncio.timeout(30):
                    while (await invoke("queue", {"session_id": session}))["requests"]:
                        await asyncio.sleep(0.03)

            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                self.assertIsNone(host.assistant)
                terminal = Chat(invoke, output=lambda *args: None)
                await terminal.select()
                a = terminal.session
                await terminal.command("SESSION_A_FIRST")
                await terminal.command("SESSION_A_SECOND")
                await terminal.command("/new B")
                b = terminal.session
                await terminal.command("SESSION_B_ONLY")
                self.assertFalse(await terminal.command("/exit"))
                await asyncio.gather(idle(a), idle(b))
                self.assertEqual(len(gateway.requests), 3)
                a_second = next(body for body in gateway.requests if "SESSION_A_SECOND" in json.dumps(body))
                b_first = next(body for body in gateway.requests if "SESSION_B_ONLY" in json.dumps(body))
                self.assertIn("SESSION_A_FIRST", json.dumps(a_second))
                self.assertNotIn("SESSION_B_ONLY", json.dumps(a_second))
                self.assertNotIn("SESSION_A_FIRST", json.dumps(b_first))
                self.assertEqual(host.conversations.sessions.bound("web"), host.conversation.session)
                self.assertNotIn(host.conversation.session, {a, b})
                self.assertEqual(len(host.backend.client.workers), 3)
                records = host.store.db.execute("SELECT state,data FROM agent_runs").fetchall()
                self.assertEqual(len(records), 3)
                self.assertTrue(
                    all(
                        state == "completed" and json.loads(data)["source"] == "conversation"
                        for state, data in records
                    )
                )
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                host = AssistantService(config)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                again = Chat(invoke, output=lambda *args: None)
                await again.select()
                self.assertEqual(again.session, b)
                await again.command("/switch " + a)
                await again.command("SESSION_A_AFTER_RESTART")
                await idle(a)
                self.assertEqual(len(gateway.requests), 4)
                self.assertIn("SESSION_A_SECOND", json.dumps(gateway.requests[-1]))
                self.assertNotIn("SESSION_B_ONLY", json.dumps(gateway.requests[-1]))
                history = await invoke("history", {"session_id": a})
                self.assertEqual(len([m for m in history["messages"] if m["role"] == "user"]), 3)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                await gateway.aclose()
