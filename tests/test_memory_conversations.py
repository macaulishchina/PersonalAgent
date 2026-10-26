"""Conversation evidence, durable capture and real Web/CLI-to-Mem0 acceptance."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from assistant_runtime.api import ConversationRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.store import Store
from assistant_runtime.core.conversation.plugin import Conversations
from assistant_runtime.core.memory.service import MemoryPlugin
from test_conversation_sessions import Backend, Context
from test_memory import FakeMemory


class ConversationMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.host = SimpleNamespace(
            store=self.store,
            config=Config(root=self.root, plugins=[]),
            backend=Backend(),
            assistant=None,
            memory=None,
        )
        self.conversations = Conversations(self.host)
        self.context = Context()
        await self.conversations.start(self.context)
        self.provider = FakeMemory()
        self.memory = MemoryPlugin(
            SimpleNamespace(memory_auto_capture=True, workspace="project"),
            self.store,
            self.provider,
            lambda: False,
        )
        self.host.memory = self.memory

    async def asyncTearDown(self):
        await self.context.close()
        self.store.db.close()
        self.temp.cleanup()

    async def call(self, action, **args):
        return await self.conversations.request(ConversationRequest(action, args))

    async def submit(self, chat, text, source="cli", request_id=None):
        ident = request_id or str(uuid.uuid4())
        await chat.submit(ident, text, [], source=source)
        async with asyncio.timeout(2):
            while not chat.backend.busy:
                await asyncio.sleep(0.005)
        return ident

    def collect(self, enabled=True):
        self.memory.records.collect_conversations(enabled, "project")
        return self.store.db.execute(
            "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id"
        ).fetchall()

    async def test_sessions_supplement_answer_duplicate_and_restart(self):
        web = self.conversations.main
        other = await self.call("create")
        cli = await self.conversations.get(other["id"])
        rid = await self.submit(web, "I prefer tea", "web")
        await web.submit(rid, "I prefer tea", [], source="web")
        cid = await self.submit(cli, "Use Python for this project")
        await self.call("supplement", session_id=cli.session, request_id=cid, text="Use SQLite")
        await self.call("answer", session_id=cli.session, request_id=cid, question_id="q", text="Python 3.12")
        self.assertEqual(self.collect(), [])
        await web.backend.finish()
        await cli.backend.finish()
        web.store.put(
            "task-report",
            web.session,
            text="Invented assistant fact",
            status="complete",
            turn_id=web.backend.last_turn,
        )
        jobs = self.collect()
        self.assertEqual(len(jobs), 2)
        sources = [self.store.transcripts.between(start, end) for _, start, end, _ in jobs]
        self.assertEqual([len(s) for s in sources], [1, 3])
        self.assertEqual({s["session_id"] for s in sources[0]}, {web.session})
        self.assertEqual({s["request_id"] for s in sources[1]}, {cid})
        self.assertEqual(sources[0][0]["entrance"], "web")
        self.assertEqual(sources[1][0]["entrance"], "cli")
        self.assertTrue(all(s["turn_id"] == cli.backend.last_turn for s in sources[1]))
        self.assertEqual(self.collect(), jobs)
        # Recovery before dispatch preserves the exact evidence and durable job IDs.
        from assistant_runtime.core.memory.records import MemoryRecords

        self.memory.records = MemoryRecords(self.store)
        self.assertEqual(self.collect(), jobs)
        for job in jobs:
            await self.memory.process_job(job)
        calls = self.provider.capture_calls
        for job in jobs:
            await self.memory.process_job(job)
        self.assertEqual(self.provider.capture_calls, calls)
        self.assertEqual(self.collect(), jobs)

    async def test_cancelled_unknown_disabled_and_forgotten_input_is_not_learned(self):
        chat = self.conversations.main
        await self.submit(chat, "cancelled")
        await chat.backend.finish("interrupted")
        await self.submit(chat, "uncertain")
        await chat.backend.finish("unknown")
        self.assertEqual(self.collect(), [])
        chat = await self.conversations.get((await self.call("create"))["id"])
        await self.submit(chat, "disabled")
        await chat.backend.finish()
        self.assertEqual(self.collect(False), [])
        self.assertEqual(self.collect(), [])
        await self.submit(chat, "do not relearn this")
        await chat.backend.finish()
        self.memory.invalidate()
        self.assertEqual(self.collect(), [])
        await self.submit(chat, "new preference")
        await chat.backend.finish()
        self.assertEqual(len(self.collect()), 1)

    async def test_failed_job_insert_rolls_back_evidence_and_can_retry(self):
        chat = self.conversations.main
        await self.submit(chat, "I prefer tea")
        await chat.backend.finish()
        self.store.db.execute(
            "CREATE TEMP TRIGGER reject_job BEFORE INSERT ON memory_jobs "
            "BEGIN SELECT RAISE(ABORT,'simulated write failure'); END"
        )
        with self.assertRaisesRegex(Exception, "simulated write failure"):
            self.collect()
        self.assertEqual(self.store.transcripts.last_id(), 0)
        self.store.db.execute("DROP TRIGGER reject_job")
        self.assertEqual(len(self.collect()), 1)
        self.assertEqual(self.store.transcripts.last_id(), 1)

    async def test_background_admission_checks_other_cli_sessions(self):
        from assistant_runtime.core.memory.declaration import build

        memory = build(self.host)
        self.assertFalse(memory.foreground())
        cli = await self.conversations.get((await self.call("create"))["id"])
        await self.submit(cli, "busy CLI")
        self.assertFalse(self.conversations.main.backend.busy)
        self.assertTrue(memory.foreground())
        await cli.backend.finish()
        self.assertFalse(memory.foreground())

    async def test_capture_policy_change_uses_boundary_and_rolls_back(self):
        chat = self.conversations.main
        await self.submit(chat, "input before disabling")
        await chat.backend.finish()
        change = await self.memory.prepare_configuration(
            SimpleNamespace(memory_auto_capture=False, workspace="project")
        )
        await change.commit()
        self.assertEqual(self.collect(), [])
        await change.rollback()
        self.assertTrue(self.memory.config.memory_auto_capture)
        self.assertEqual(len(self.collect()), 1)

    async def test_migration_does_not_backfill_and_batches_are_bounded(self):
        chat = self.conversations.main
        await self.submit(chat, "old history")
        await chat.backend.finish()
        self.store.db.execute("DELETE FROM settings WHERE key='memory.chat_start'")
        self.store.db.commit()
        from assistant_runtime.core.memory.records import MemoryRecords

        self.memory.records = MemoryRecords(self.store)
        self.assertEqual(self.collect(), [])
        ident = await self.submit(chat, "x" * 20000)
        await self.call("supplement", session_id=chat.session, request_id=ident, text="y" * 20000)
        await chat.backend.finish()
        jobs = self.collect()
        self.assertEqual(len(jobs), 2)
        for _, start, end, _ in jobs:
            self.assertLessEqual(
                sum(len(s["text"]) for s in self.store.transcripts.between(start, end)), 32000
            )
        self.memory.enqueue()
        self.assertEqual(self.collect(), jobs)  # Legacy completion callback cannot duplicate chat.

    async def test_provenance_survives_validated_extraction_and_new_workspace(self):
        chat = self.conversations.main
        await self.submit(chat, "I prefer tea", "web")
        await chat.backend.finish()
        (job,) = self.collect()
        (source,) = self.store.transcripts.between(job[1], job[2])
        self.provider.inference.return_value = [
            {
                "id": "tea",
                "title": "Tea",
                "text": "I prefer tea",
                "scope": "user",
                "source_id": source["id"],
                "evidence": "I prefer tea",
            }
        ]
        self.memory.config.workspace = "different-project"
        await self.memory.process_job(job)
        saved = (await self.provider.read("tea"))["source"]
        for key in ("session_id", "message_id", "request_id", "turn_id", "entrance", "workspace"):
            self.assertEqual(saved[key], source[key])


class ConversationMemoryNativeTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_web_cli_real_pi_mem0_restart_without_duplicate_extraction(self):
        from aiohttp import ClientSession, CookieJar
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.cli import request
        from assistant_runtime.core.cli.client import Chat
        from pi_gateway import FakeResponsesGateway, sse_text_reply
        from test_pi_skeleton import configuration

        cache = Path(os.environ.get("PA_TEST_MEMORY_CACHE", "models/memory-embedding")).resolve()
        if not cache.exists():
            self.skipTest("Requires prepared local memory embedding model")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            gateway = FakeResponsesGateway(lambda step, body: sse_text_reply(f"reply{step}", "OK"))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = Config.load(initialize(root / "instance", preset="slim"))
            config = replace(
                base,
                workspace=str(root),
                plugins=[*base.plugins, "memory", "web"],
                memory_enabled=True,
                main_model="model-a",
                main_reasoning_effort="low",
                model_api=configuration(gateway.base_url),
                web={"enable": True, "host": "127.0.0.1", "port": port},
            )
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            extractions = []
            model_calls = 0
            fact = ""

            async def complete(messages, schema):
                nonlocal fact, model_calls
                model_calls += 1
                if "facts" not in schema["properties"]:
                    return {"memory": [{"id": "0", "text": fact, "event": "ADD", "old_memory": ""}]}
                content = json.dumps(messages)
                if "Only durable decisions" in content:
                    return {"facts": [], "provenance": []}
                sources = None
                for message in messages:
                    text = message.get("content", "")
                    for i in range(len(text)):
                        if text[i : i + 2] == "[{":
                            try:
                                candidate, _ = json.JSONDecoder().raw_decode(text[i:])
                            except ValueError:
                                continue
                            if candidate and isinstance(candidate[0], dict) and "message_id" in candidate[0]:
                                sources = candidate
                                break
                self.assertIsNotNone(sources, messages)
                self.assertEqual(len({s["session_id"] for s in sources}), 1)
                extractions.append(sources)
                source = sources[0]
                fact = source["text"]
                return {
                    "facts": [fact],
                    "provenance": [{"fact": fact, "source_id": source["id"], "evidence": fact}],
                }

            async def wait(predicate, timeout=60):
                async with asyncio.timeout(timeout):
                    while not predicate():
                        await asyncio.sleep(0.05)

            async def boot(paused=False):
                host = AssistantService(config)
                host.memory.provider.cache = cache
                host.memory.provider.complete = complete
                host.memory.draining = paused
                task = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 25)
                return host, task

            async def invoke(action, arguments):
                return await request(
                    config, {"command": "conversation", "action": action, "arguments": arguments}
                )

            host, running = await boot(True)
            try:
                terminal = Chat(invoke, output=lambda *args: None)
                await terminal.select()
                first = terminal.session
                await terminal.command("I prefer oolong tea without sugar.")
                await terminal.command("/new second")
                second = terminal.session
                await terminal.command("I prefer concise written answers.")
                await terminal.command("/exit")
                url = f"http://127.0.0.1:{port}"
                async with ClientSession(
                    cookie_jar=CookieJar(unsafe=True), headers={"Origin": url}
                ) as client:
                    token = (config.state_dir / "web-access-token").read_text().strip()
                    response = await client.post(url + "/api/login", json={"token": token})
                    self.assertEqual(response.status, 200)
                    payload = {"request_id": str(uuid.uuid4()), "text": "I prefer a quiet office."}
                    for _ in range(2):
                        response = await client.post(url + "/api/messages", json=payload)
                        self.assertEqual(response.status, 202, await response.text())
                await wait(lambda: not host.conversations.maintenance_pending())
                self.assertEqual(len(gateway.requests), 3)
                web = host.conversation.session
                host.stop_event.set()
                await asyncio.wait_for(running, 25)
                # Completed history not yet collected is discovered after restart.
                host, running = await boot()
                await wait(
                    lambda: host.store.db.execute(
                        "SELECT count(*) FROM memory_jobs WHERE status='done'"
                    ).fetchone()[0]
                    == 3
                )
                self.assertEqual(len(extractions), 3)
                completed_calls = model_calls
                records = await host.memory.provider.export()
                self.assertEqual(len(records), 3)
                self.assertEqual({r["source"]["session_id"] for r in records}, {web, first, second})
                self.assertEqual({r["source"]["entrance"] for r in records}, {"web", "cli"})
                for record in records:
                    self.assertTrue(record["source"]["request_id"])
                    self.assertTrue(record["source"]["turn_id"])
                    self.assertTrue(record["source"]["message_id"])
                # Simulate parent interruption after Mem0 committed, before job acknowledgement.
                host.memory.draining = True
                await wait(lambda: not host.memory.maintenance_busy)
                host.store.db.execute("UPDATE memory_jobs SET status='running'")
                host.store.db.commit()
                host.stop_event.set()
                await asyncio.wait_for(running, 25)
                host, running = await boot()
                await wait(
                    lambda: host.store.db.execute(
                        "SELECT count(*) FROM memory_jobs WHERE status='done'"
                    ).fetchone()[0]
                    == 3
                )
                self.assertEqual(len(extractions), 3)
                self.assertEqual(model_calls, completed_calls)
                self.assertEqual(len(await host.memory.provider.export()), 3)
                self.assertEqual(len(gateway.requests), 3)
                self.assertFalse(host.memory.context.error)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 25)
                await gateway.aclose()
