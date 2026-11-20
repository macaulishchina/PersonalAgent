"""Synthetic terminal/voice contracts; never contacts a real provider or device."""

import asyncio
import json
import logging
import sqlite3
import struct
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, WSServerHandshakeError, WSMsgType, web
from aiohttp.test_utils import TestServer
from assistant_runtime.api.ai_usage import UsageScope
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import RecognitionRequest, SynthesisRequest, SpeechError, SpeechRequest
from assistant_runtime.api.terminal import TerminalNotifications
from assistant_runtime.core.speech_io.service import InputStream
from assistant_runtime.plugins.aliyun_speech.plugin import AliyunSpeech
from assistant_runtime.plugins.ai_passport import protocol
from assistant_runtime.plugins.ai_passport.connection import Connection
from assistant_runtime.plugins.ai_passport.repository import Devices
from assistant_runtime.plugins.ai_passport.plugin import AiPassportPlugin
import test_conversation_sessions as sessions_fixture


class ProviderProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.synthesized = []
        self.fault = None
        self.fault_message = "secret must not escape"
        self.run_gate = None

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            ident = None
            asr = False
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    self.received.append(message.data)
                    await ws.send_json(
                        {
                            "header": {"task_id": ident, "event": "result-generated"},
                            "payload": {"output": {"sentence": {"text": "中间转写", "sentence_end": False}}},
                        }
                    )
                    continue
                if message.type != WSMsgType.TEXT:
                    break
                command = json.loads(message.data)
                ident = command["header"]["task_id"]
                action = command["header"]["action"]
                self.received.append(action)
                if action == "run-task":
                    if self.run_gate:
                        await self.run_gate.wait()
                    asr = command["payload"]["task"] == "asr"
                    if self.fault:
                        await ws.send_json(
                            {
                                "header": {
                                    "task_id": ident,
                                    "event": "task-failed",
                                    "error_code": self.fault,
                                    "error_message": self.fault_message,
                                }
                            }
                        )
                    else:
                        await ws.send_json({"header": {"task_id": ident, "event": "task-started"}})
                if action == "continue-task" and not asr:
                    self.synthesized.append(command["payload"]["input"]["text"])
                if action == "finish-task":
                    if asr:
                        await ws.send_json(
                            {
                                "header": {"task_id": ident, "event": "result-generated"},
                                "payload": {
                                    "output": {
                                        "sentence": {
                                            "text": "最终转写",
                                            "sentence_end": True,
                                            "begin_time": 0,
                                        }
                                    }
                                },
                            }
                        )
                    else:
                        await ws.send_bytes(b"\x00\x00" * 160)
                    await ws.send_json({"header": {"task_id": ident, "event": "task-finished"}})
            return ws

        app = web.Application()
        app.router.add_get("/speech", ws_handler)
        self.server = TestServer(app)
        await self.server.start_server()
        config = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "src/assistant_runtime/plugins/aliyun_speech/configuration.json"
            ).read_text(encoding="utf-8")
        )["schema"]["properties"]
        settings = {k: v["default"] for k, v in config.items()}
        settings["endpoint"] = str(self.server.make_url("/speech")).replace("http:", "ws:")
        self.usage = SimpleNamespace(admit=AsyncMock(return_value={"id": "ticket"}), settle=AsyncMock())
        self.context = SimpleNamespace(
            config=settings,
            credential=lambda _: "synthetic-key",
            log=logging.getLogger("test"),
            resource=lambda _: self.usage,
            own=lambda _: None,
            register=lambda *a, **kw: None,
            set_health=lambda *a: None,
        )
        self.provider = AliyunSpeech(self.context)
        await self.provider.start(self.context)
        self.scope = UsageScope("work", "terminal:test")

    async def asyncTearDown(self):
        await self.provider.close()
        await self.server.close()

    async def test_final_only_and_streamed_output_share_work(self):
        stream = await self.provider.request(RecognitionRequest(self.scope, 1))
        await stream.send(b"\x00\x00" * 320)
        self.assertEqual(await stream.finish(), "最终转写")
        await stream.close()
        output = await self.provider.request(SynthesisRequest(self.scope, "合成测试。"))
        self.assertEqual(b"".join([chunk async for chunk in output]), b"\x00\x00" * 160)
        self.assertEqual(self.received.count("run-task"), 2)
        self.assertEqual(self.usage.settle.await_args_list[-1].kwargs["state"], "completed")
        self.assertEqual(
            self.usage.admit.await_args_list[0].args[0], self.usage.admit.await_args_list[1].args[0]
        )
        self.assertFalse(self.provider.streams)

    async def test_per_terminal_voice_and_rate_are_literal_and_metered(self):
        from assistant_runtime.api.speech import SpeechRequest
        from assistant_runtime.core.speech_io.service import SpeechService

        requests = []
        original = self.provider.request

        async def invoke(request, **kwargs):
            requests.append(request)
            return await original(request)

        service = SpeechService(SimpleNamespace(config={"max_streams": 2}, invoke=invoke))
        try:
            output = await service.request(
                SpeechRequest("synthesize", self.scope, "测试。", voice="longxiaochun", rate=1.25)
            )
            self.assertTrue(b"".join([chunk async for chunk in output]))
            self.assertEqual(requests[0].voice, "longxiaochun")
            self.assertEqual(requests[0].rate, 1.25)
            for options in ({"voice": "bad\r\nvalue"}, {"rate": 0}, {"rate": True}, {"rate": float("nan")}):
                with self.assertRaises(SpeechError):
                    await service.request(SpeechRequest("synthesize", self.scope, "测试。", **options))
            self.assertEqual(len(requests), 1)
        finally:
            await service.close()

    async def test_close_before_first_output_iteration_settles_and_releases(self):
        stream = await self.provider.request(SynthesisRequest(self.scope, "test"))
        await stream.aclose()
        self.assertNotIn("continue-task", self.received)
        self.assertFalse(self.provider.streams)
        self.assertEqual(self.usage.settle.await_args.kwargs["state"], "unknown")

    async def test_rejection_is_not_retried_and_does_not_echo_provider_body(self):
        self.fault = "AllocationQuota.FreeTierOnly"
        with self.assertRaises(SpeechError) as exc:
            await self.provider.request(RecognitionRequest(self.scope, 1))
        self.assertIn(self.fault, str(exc.exception))
        self.assertNotIn("secret", str(exc.exception))
        self.assertEqual(self.received.count("run-task"), 1)
        self.assertFalse(self.provider.streams)

    async def test_provider_client_timeout_has_distinct_safe_code(self):
        self.fault = "CLIENT_ERROR"
        self.fault_message = "request timeout after 23 seconds."
        with self.assertLogs("test", level="WARNING") as logs:
            with self.assertRaisesRegex(SpeechError, "^speech_provider_timeout$"):
                await self.provider.request(RecognitionRequest(self.scope, 1))
        self.assertIn("audio_bytes=0 finish_sent=False provider_timeout=True", logs.output[0])
        self.assertNotIn(self.fault_message, logs.output[0])
        self.assertFalse(self.provider.streams)

    async def test_provider_can_offer_only_one_capability(self):
        self.provider.settings["capabilities"] = ["synthesize"]
        with self.assertRaisesRegex(SpeechError, "speech_invalid_action"):
            await self.provider.request(RecognitionRequest(self.scope))
        self.usage.admit.assert_not_awaited()

    async def test_100_synthetic_stream_cycles_release_all_streams(self):
        for _ in range(100):
            recognition = await self.provider.request(RecognitionRequest(self.scope, 1))
            await recognition.send(b"\x00\x00" * 320)
            self.assertEqual(await recognition.finish(), "最终转写")
            await recognition.close()
            playback = await self.provider.request(SynthesisRequest(self.scope, "测试。"))
            async for _chunk in playback:
                pass
            self.assertFalse(self.provider.streams)
        self.assertEqual(self.usage.admit.await_count, 200)
        self.assertEqual(self.usage.settle.await_count, 200)

    async def test_four_opening_streams_each_reserve_one_slot_and_cancellation_releases_it(self):
        self.run_gate = asyncio.Event()
        request = SynthesisRequest(self.scope, "测试")
        opening = [asyncio.create_task(self.provider.request(request)) for _ in range(4)]
        try:
            async with asyncio.timeout(2):
                while self.received.count("run-task") < 4:
                    await asyncio.sleep(0)
            with self.assertRaisesRegex(SpeechError, "speech_busy"):
                await self.provider.request(request)
            opening[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening[0]
            replacement = asyncio.create_task(self.provider.request(request))
            opening.append(replacement)
            async with asyncio.timeout(2):
                while self.received.count("run-task") < 5:
                    await asyncio.sleep(0)
            self.run_gate.set()
            streams = await asyncio.gather(*opening[1:])
            self.assertEqual(len(self.provider.streams), 4)
            self.assertFalse(self.provider.opening)
            await asyncio.gather(*(stream.aclose() for stream in streams))
            self.assertFalse(self.provider.streams)
        finally:
            self.run_gate.set()
            for task in opening:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*opening, return_exceptions=True)


class ConnectionLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.devices = Devices(self.db)
        self.ident, self.credential = "d" * 32, "synthetic-credential" * 3
        code = self.devices.begin(self.ident, self.credential)["code"]
        self.devices.activate(code, "PP", "persistent-session")

        async def invoke(request, **kwargs):
            self.assertIsInstance(request, ConversationRequest)
            self.assertEqual(request.arguments["session_id"], "persistent-session")
            if request.action == "state":
                return {"queue": []}
            return {"cursor": 0, "events": [], "items": []}

        self.context = SimpleNamespace(
            config={"max_connections": 1, "speech_mode": "off"},
            invoke=invoke,
            spawn=lambda coro, name: asyncio.create_task(coro),
            available=lambda _: False,
            log=logging.getLogger("test"),
        )
        self.plugin = AiPassportPlugin(self.context)
        self.plugin.devices = self.devices
        self.plugin.frontend = SimpleNamespace(key="synthetic-frontend")
        app = web.Application(middlewares=[self.plugin.guard])
        app.router.add_get("/stream", self.plugin.stream)
        self.server = TestServer(app)
        await self.server.start_server()
        self.client = ClientSession()
        self.sockets = []

    async def asyncTearDown(self):
        await asyncio.gather(*(ws.close() for ws in self.sockets))
        await self.client.close()
        await self.server.close()
        self.db.close()

    async def connect(self, credential=None, ident=None):
        ws = await self.client.ws_connect(
            self.server.make_url("/stream"),
            headers={
                "X-PA-Frontend-Key": "synthetic-frontend",
                "X-Device-Id": ident or self.ident,
                "Authorization": "Bearer " + (credential or self.credential),
            },
        )
        self.sockets.append(ws)
        return ws

    async def test_unicode_is_measured_and_sent_as_the_same_utf8_frame(self):
        socket = await self.connect()
        await socket.receive_json()  # Wait until the handshake has completed.
        connection = self.plugin.connections[self.ident]
        body = "终" * 5000
        await connection.send("message", text=body)
        async with asyncio.timeout(2):
            while True:
                text = await socket.receive_str()
                if json.loads(text).get("text") == body:
                    break
        self.assertLessEqual(len(text.encode("utf-8")), 16384)
        with self.assertRaisesRegex(SpeechError, "terminal_message_limit"):
            await connection.send("message", text="终" * 5500)

    async def test_authenticated_reconnect_takes_ownership_and_old_cleanup_cannot_remove_it(self):
        first = await self.connect()
        self.assertEqual((await first.receive_json())["binding"], "persistent-session")
        old = self.plugin.connections[self.ident]
        second = await self.connect()
        self.assertEqual((await second.receive_json())["binding"], "persistent-session")
        current = self.plugin.connections[self.ident]
        self.assertIsNot(current, old)
        self.assertTrue(old.closed)
        async with asyncio.timeout(2):
            while (await first.receive()).type not in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
                pass
        self.assertIs(self.plugin.connections[self.ident], current)
        with self.assertRaises(WSServerHandshakeError) as rejected:
            await self.connect(credential="wrong")
        self.assertEqual(rejected.exception.status, 401)
        self.assertIs(self.plugin.connections[self.ident], current)
        other = "e" * 32
        code = self.devices.begin(other, self.credential)["code"]
        self.devices.activate(code, "Other", "other-session")
        with self.assertRaises(WSServerHandshakeError) as capacity:
            await self.connect(ident=other)
        self.assertEqual(capacity.exception.status, 503)
        self.assertIs(self.plugin.connections[self.ident], current)
        await second.close()

    async def test_cleanup_failure_closes_socket_and_does_not_leak_ownership(self):
        first = await self.connect()
        await first.receive_json()
        old = self.plugin.connections[self.ident]
        old.abort_input = AsyncMock(side_effect=RuntimeError("synthetic-cleanup-failure"))
        with self.assertLogs("test", level="WARNING"):
            second = await self.connect()
            await second.receive_json()
        self.assertTrue(old.closed)
        self.assertTrue(old.ws.closed)
        self.assertIsNot(self.plugin.connections[self.ident], old)
        await second.close()
        async with asyncio.timeout(2):
            while self.ident in self.plugin.connections:
                await asyncio.sleep(0)
        third = await self.connect()
        self.assertEqual((await third.receive_json())["binding"], "persistent-session")
        self.devices.revoke(self.ident)
        with self.assertRaises(WSServerHandshakeError) as revoked:
            await self.connect()
        self.assertEqual(revoked.exception.status, 401)
        await third.close()

    async def test_repeated_reconnect_waits_for_previous_cleanup_without_blocking_other_device(self):
        self.plugin.settings["max_connections"] = 2
        first = await self.connect()
        await first.receive_json()
        old = self.plugin.connections[self.ident]
        entered, release = asyncio.Event(), asyncio.Event()
        frame_entered, frame_release = asyncio.Event(), asyncio.Event()

        async def command(data):
            frame_entered.set()
            await frame_release.wait()

        async def abort(**kwargs):
            entered.set()
            await release.wait()

        old.command = AsyncMock(side_effect=command)
        old.abort_input = abort
        await first.send_json({"type": "test.frame"})
        await asyncio.wait_for(frame_entered.wait(), 2)
        replacement = asyncio.create_task(self.connect())
        try:
            async with asyncio.timeout(2):
                while not old.closed:
                    await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
            await first.send_json({"type": "test.late_frame"})
            with self.assertRaises(WSServerHandshakeError) as rejected:
                await self.connect()
            self.assertEqual(rejected.exception.status, 409)
            other = "e" * 32
            code = self.devices.begin(other, self.credential)["code"]
            self.devices.activate(code, "Other", "persistent-session")
            independent = await asyncio.wait_for(self.connect(ident=other), 2)
            self.assertEqual((await independent.receive_json())["type"], "hello")
            self.assertFalse(replacement.done())
            frame_release.set()
            await asyncio.wait_for(entered.wait(), 2)
            old.command.assert_awaited_once()
        finally:
            frame_release.set()
            release.set()
            result = await asyncio.gather(replacement, return_exceptions=True)
        self.assertEqual((await result[0].receive_json())["type"], "hello")
        self.assertTrue(old.ws.closed)


class EntranceReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = sessions_fixture.SessionsTest("test_idempotent_creation_policy_and_request_receipt")
        await self.fixture.asyncSetUp()
        self.registry = self.fixture.registry
        self.bindings = {}

        async def bind(ident, scope):
            self.bindings[ident] = scope

        async def binding(ident):
            return self.bindings.get(ident)

        self.fixture.host.ai_usage = SimpleNamespace(service=SimpleNamespace(bind=bind, binding=binding))
        self.session = (await self.registry.request(ConversationRequest("create", {"title": "PP"})))["id"]

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def call(self, action, ident, **args):
        return await self.registry.inputs.request(
            ConversationInputRequest(action, {"session_id": self.session, "request_id": ident, **args})
        )

    async def test_idempotent_commit_reconnect_and_cross_session_isolation(self):
        ident = str(uuid.uuid4())
        await self.call("prepare", ident, source="terminal:pp", mode="queue")
        first = await self.call("commit", ident, text="only once")
        self.assertEqual(first, await self.call("commit", ident, text="only once"))
        chat = await self.fixture.started(self.session)
        self.assertEqual(chat.backend.calls, ["only once"])
        with self.assertRaises(ValueError):
            await self.call("commit", ident, text="changed")
        other = (await self.registry.request(ConversationRequest("create", {"title": "other"})))["id"]
        receipt = await self.registry.inputs.request(
            ConversationInputRequest("receipt", {"session_id": other, "request_id": ident})
        )
        self.assertEqual(receipt["state"], "absent")
        # Emulate losing only the post-acceptance receipt write. Query the existing queue; never resubmit.
        with self.registry.inputs.db:
            self.registry.inputs.db.execute(
                "UPDATE conversation_inputs SET state='unknown' WHERE id=?", (ident,)
            )
        self.assertEqual((await self.call("receipt", ident))["state"], "accepted")
        self.assertEqual(len(chat.backend.calls), 1)

    async def test_review_survives_reopen_and_only_explicit_confirmation_dispatches(self):
        from assistant_runtime.core.conversation.inputs import EntranceInputs

        ident = str(uuid.uuid4())
        text = "这是需要确认的完整输入。" * 300
        await self.call("prepare", ident, source="terminal:pp", mode="queue")
        receipt = await self.call("review", ident, text=text)
        self.assertEqual(receipt["state"], "review")
        self.assertEqual(receipt["text_length"], len(text))
        self.assertNotIn("text", receipt)
        self.assertEqual(receipt, await self.call("review", ident, text=text))
        self.assertEqual(
            self.registry.inputs.db.execute("SELECT count(*) FROM conversation_queue").fetchone()[0], 0
        )
        path = self.registry.inputs.db.execute("PRAGMA database_list").fetchone()[2]
        reopened = sqlite3.connect(path)
        try:
            recovered = EntranceInputs(
                SimpleNamespace(sessions=SimpleNamespace(db=reopened, get=self.registry.sessions.get))
            )
            page = await recovered.request(
                ConversationInputRequest(
                    "transcript",
                    {
                        "session_id": self.session,
                        "request_id": ident,
                        "offset": 80,
                        "limit": 80,
                    },
                )
            )
            self.assertEqual(page["text"], text[80:160])
            self.assertEqual(page["next_offset"], 160)
            with self.assertRaisesRegex(ValueError, "confirmation"):
                await self.call("commit", ident, text=text)
            with self.assertRaisesRegex(ValueError, "replacement"):
                await self.call("confirm", ident, text="模型改写后的内容")
            with self.assertRaisesRegex(ValueError, "different text"):
                await self.call("review", ident, text=text + "different")
            accepted = await self.call("confirm", ident)
            self.assertEqual(accepted, await self.call("confirm", ident))
            chat = await self.fixture.started(self.session)
            self.assertEqual(chat.backend.calls, [text])
            self.assertIsNone(self.registry.inputs.get(ident, self.session)["draft"])
        finally:
            reopened.close()

    async def test_discard_clears_draft_and_other_sessions_cannot_read_or_confirm(self):
        ident = str(uuid.uuid4())
        await self.call("prepare", ident, source="terminal:pp", mode="queue")
        await self.call("review", ident, text="未发送的私人文字")
        other = (await self.registry.request(ConversationRequest("create", {"title": "other"})))["id"]
        for action in ("transcript", "confirm", "cancel"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                await self.registry.inputs.request(
                    ConversationInputRequest(action, {"session_id": other, "request_id": ident})
                )
        for bad in ({"offset": -1}, {"offset": True}, {"limit": 1000}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                await self.call("transcript", ident, **bad)
        await self.call("cancel", ident)
        self.assertIsNone(self.registry.inputs.get(ident, self.session)["draft"])
        with self.assertRaises(ValueError):
            await self.call("confirm", ident)
        self.assertEqual(
            self.registry.inputs.db.execute("SELECT count(*) FROM conversation_queue").fetchone()[0], 0
        )

    async def test_ended_recording_target_becomes_new_input(self):
        first = str(uuid.uuid4())
        await self.call("prepare", first, source="terminal:pp", mode="steer")
        await self.call("commit", first, text="first")
        chat = await self.fixture.started(self.session)
        second = str(uuid.uuid4())
        prepared = await self.call("prepare", second, source="terminal:pp", mode="steer")
        self.assertEqual(prepared["work_id"], first)
        await chat.backend.finish()
        await self.call("commit", second, text="follow up")
        await self.fixture.started(self.session, 2)
        self.assertEqual(chat.backend.calls, ["first", "follow up"])

    async def test_active_supplement_keeps_terminal_provenance_and_shared_work(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        await self.call("prepare", first, source="terminal:pp", mode="steer")
        await self.call("commit", first, text="first")
        chat = await self.fixture.started(self.session)
        prepared = await self.call("prepare", second, source="terminal:pp", mode="steer")
        self.assertEqual(prepared["work_id"], first)
        result = await self.call("commit", second, text="correction")
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(chat.backend.calls[-1][0], "supplement")
        self.assertEqual(chat.store.history(self.session)[-1]["source"], "terminal:pp")
        await self.call("commit", second, text="correction")
        self.assertEqual(len(chat.backend.calls), 2)

    async def test_full_message_pages_are_owned_and_read_without_reexecution(self):
        text = "完整结果。" * 200
        files = self.fixture.host.workfiles.results
        reference = await files.put("synthetic", "answer", "operator", {"text": text})
        self.fixture.host.runtime = SimpleNamespace(
            invoke=lambda request: files.request(request, owner="operator")
        )
        self.registry.main.store.put(
            "answer",
            self.session,
            text="节选",
            status="complete",
            result_ref=reference,
            presentation={"body": "excerpt", "result_offset": 0, "result_length": len(text)},
        )
        page = await self.registry.request(
            ConversationRequest(
                "message",
                {
                    "session_id": self.session,
                    "message_id": "answer",
                    "offset": 80,
                    "limit": 80,
                },
            )
        )
        self.assertEqual(page["text"], text[80:160])
        self.assertEqual(page["next_offset"], 160)
        other = (await self.registry.request(ConversationRequest("create", {"title": "other"})))["id"]
        with self.assertRaisesRegex(ValueError, "Unknown"):
            await self.registry.request(
                ConversationRequest("message", {"session_id": other, "message_id": "answer"})
            )

    async def test_no_model_prevents_paid_recording_preparation(self):
        chat = await self.registry.get(self.session)
        chat.backend.client.settings.enabled = False
        with self.assertRaises(ValueError):
            await self.call("prepare", str(uuid.uuid4()), source="terminal:pp", mode="queue")
        self.assertFalse(self.bindings)

    async def test_history_index_is_bounded_bidirectional_and_session_bound(self):
        store = self.registry.main.store
        for i in range(9):
            store.put(
                f"message-{i}",
                self.session,
                text="正文" * 10000,
                role="user" if i % 2 == 0 else "assistant",
                status="complete",
            )
        store.put("tool-detail", self.session, text="private-tool-output", kind="detail", status="complete")
        newest = await self.registry.request(
            ConversationRequest("history_index", {"session_id": self.session, "limit": 2})
        )
        self.assertEqual([i["id"] for i in newest["items"]], ["message-7", "message-8"])
        self.assertNotIn("text", newest["items"][0])
        self.assertEqual(len(newest["items"][0]["preview"]), 80)
        self.assertLess(len(json.dumps(newest).encode()), 2400)
        older = await self.registry.request(
            ConversationRequest(
                "history_index", {"session_id": self.session, "before": newest["before"], "limit": 2}
            )
        )
        self.assertEqual([i["id"] for i in older["items"]], ["message-5", "message-6"])
        forward = await self.registry.request(
            ConversationRequest(
                "history_index", {"session_id": self.session, "after": older["after"], "limit": 2}
            )
        )
        self.assertEqual([i["id"] for i in forward["items"]], ["message-7", "message-8"])
        self.assertIsNone(forward["after"])
        other = (await self.registry.request(ConversationRequest("create", {"title": "other"})))["id"]
        with self.assertRaisesRegex(ValueError, "cursor"):
            await self.registry.request(
                ConversationRequest("history_index", {"session_id": other, "before": newest["before"]})
            )
        with self.assertRaises(ValueError):
            await self.registry.request(
                ConversationRequest("history_index", {"session_id": self.session, "limit": 1000})
            )
        self.assertFalse(self.bindings)

    async def test_committed_message_wakes_only_with_identity_and_durable_cursor(self):
        store = self.registry.main.store
        self.fixture.context.events.clear()
        store.put("wake", self.session, text="正文", status="complete")
        kind, payload = self.fixture.context.events[-1]
        self.assertEqual(kind, "conversation.changed")
        self.assertEqual(payload, {"session_id": self.session, "cursor": store.cursor()})
        self.assertNotIn("正文", json.dumps(payload, ensure_ascii=False))
        previous = len(self.fixture.context.events)
        store.put("wake", self.session, text="正文", status="complete")
        self.assertEqual(len(self.fixture.context.events), previous)

    async def test_image_only_results_are_bounded_session_scoped_and_never_fetch_urls(self):
        import io
        from PIL import Image

        content = io.BytesIO()
        Image.new("RGB", (640, 480), "red").save(content, format="PNG")
        ref = await self.registry.main.media.add_image(content.getvalue(), "test.png", mime_type="image/png")
        self.registry.main.store.put(
            "photo",
            self.session,
            text="private tool body",
            kind="detail",
            attachments=[ref],
            status="complete",
        )
        index = await self.registry.request(
            ConversationRequest("history_index", {"session_id": self.session})
        )
        self.assertEqual(index["items"][-1]["kind"], "image")
        self.assertEqual(index["items"][-1]["preview"], "图片")
        self.assertIsInstance(index["items"][-1]["cursor"], str)
        page = await self.registry.request(
            ConversationRequest("message", {"session_id": self.session, "message_id": "photo", "limit": 80})
        )
        self.assertEqual(page["images"][0]["id"], ref["id"])
        self.assertEqual(page["role"], "assistant")
        self.assertEqual(page["total"], len("private tool body"))
        args = {"session_id": self.session, "message_id": "photo", "image_id": ref["id"]}
        thumb = await self.registry.request(ConversationRequest("thumbnail", args))
        self.assertEqual((thumb["width"], thumb["height"]), (128, 96))
        self.assertEqual(thumb["data"], b"\x00\xf8" * 128 * 96)
        self.assertLessEqual(thumb["bytes"], 30720)
        small = await self.registry.request(
            ConversationRequest("thumbnail", {**args, "width": 64, "height": 48})
        )
        self.assertEqual((small["width"], small["height"], small["bytes"]), (64, 48, 6144))
        raster = await self.registry.request(
            ConversationRequest("image_raster", {**args, "width": 216, "height": 194})
        )
        self.assertGreater(raster["width"], 160)
        self.assertLessEqual(raster["height"], 194)
        self.assertEqual(raster["data"], b"\x00\xf8" * raster["width"] * raster["height"])
        other = (await self.registry.request(ConversationRequest("create", {"title": "other"})))["id"]
        for changed in (
            {"session_id": other},
            {"image_id": "https://secret.invalid/private"},
            {"width": 161},
            {"height": 4096},
        ):
            with self.assertRaises(ValueError):
                await self.registry.request(ConversationRequest("thumbnail", {**args, **changed}))
        for changed in ({"session_id": other}, {"width": 217}, {"height": 195}, {"image_id": "missing"}):
            with self.assertRaises(ValueError):
                await self.registry.request(ConversationRequest("image_raster", {**args, **changed}))


class TerminalStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_identified_audio_is_accepted_before_delayed_ready_without_crossing_input_ids(self):
        ident, rejected = (uuid.uuid4() for _ in range(2))
        started, release_ready, ready_sent = (asyncio.Event() for _ in range(3))
        original_send = self.connection.send
        provider = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        stream = InputStream(provider, 1, lambda _: None)

        async def send(event, **fields):
            if event == "input.ready":
                started.set()
                await release_ready.wait()
            await original_send(event, **fields)
            if event == "input.ready":
                ready_sent.set()

        async def invoke(request, **_):
            if isinstance(request, SpeechRequest):
                return stream
            return {"state": "prepared" if request.action == "prepare" else "cancelled", "work_id": "work"}

        async def audio(key, sequence, pcm):
            prefix = protocol.INPUT_AUDIO_ID_TAG + key.bytes if key else b""
            await self.connection.receive(
                SimpleNamespace(type=WSMsgType.BINARY, data=prefix + struct.pack("!I", sequence) + pcm)
            )

        self.connection.send = send
        self.context.invoke = invoke
        await self.connection.command({"type": "input.begin", "request_id": str(ident), "audio_ids": True})
        await asyncio.wait_for(started.wait(), 1)
        try:
            await self.connection.command(
                {"type": "input.begin", "request_id": str(rejected), "audio_ids": True}
            )
            await audio(rejected, 0, b"\x00\x00" * 320)
            await audio(None, 0, b"\x00\x00" * 320)
            await audio(ident, 0, b"\x01\x00" * 1600)
            self.assertEqual((stream.sequence, stream.size), (1, 3200))
            provider.send.assert_awaited_once_with(b"\x01\x00" * 1600)
            self.assertFalse(ready_sent.is_set())
        finally:
            release_ready.set()
        await asyncio.wait_for(ready_sent.wait(), 1)
        await self.connection.command({"type": "input.cancel", "request_id": str(ident)})
        await audio(ident, 1, b"\x02\x00" * 320)
        self.assertEqual(provider.send.await_count, 1)
        provider.close.assert_awaited_once()

    async def test_busy_begin_and_legacy_cancel_are_bound_to_refused_attempt(self):
        active, refused = (str(uuid.uuid4()) for _ in range(2))
        stream = self.connection.input = AsyncMock()
        self.connection.input_id = active
        await self.connection.command({"type": "input.begin", "request_id": refused})
        self.assertEqual(
            self.messages[-1],
            {
                "type": "error",
                "code": "speech_busy",
                "request_id": refused,
                "operation": "input.begin",
            },
        )
        await self.connection.command({"type": "input.cancel", "request_id": refused})
        self.assertEqual(
            self.messages[-1], {"type": "input.receipt", "request_id": refused, "state": "cancelled"}
        )
        self.assertIs(self.connection.input, stream)
        self.assertEqual(self.connection.input_id, active)
        stream.close.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_async_provider_failure_drains_audio_and_releases_input_for_next_attempt(self):
        failed, following = (str(uuid.uuid4()) for _ in range(2))
        upstream = AsyncMock()
        upstream.wait_update.side_effect = SpeechError("speech_provider_timeout")
        stream = InputStream(upstream, 1, lambda _: None)
        fresh_provider = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        fresh = InputStream(fresh_provider, 1, lambda _: None)

        async def invoke(request, **_):
            if isinstance(request, SpeechRequest):
                return fresh
            if request.action == "prepare":
                return {"state": "prepared", "work_id": "work"}
            self.assertIn(request.action, {"cancel", "receipt"})
            return {"state": "cancelled"}

        self.context.invoke = invoke
        self.connection.input, self.connection.input_id = stream, failed
        await self.connection.watch_transcript(stream, failed)
        self.assertIsNone(self.connection.input_id)
        self.assertEqual(
            self.messages[-1],
            {
                "type": "error",
                "request_id": failed,
                "operation": "input.pcm",
                "code": "speech_provider_timeout",
            },
        )
        first_error_count = len(self.messages)
        await self.connection.receive(
            SimpleNamespace(type=WSMsgType.BINARY, data=struct.pack("!I", 1) + b"\0\0")
        )
        self.assertEqual(len(self.messages), first_error_count)
        await self.connection.command({"type": "input.cancel", "request_id": failed})
        self.assertEqual(self.messages[-1]["state"], "cancelled")
        upstream.close.assert_awaited_once()

        await self.connection.command({"type": "input.begin", "request_id": following})
        self.assertEqual(self.messages[-1]["type"], "input.ready")
        self.assertEqual(self.messages[-1]["request_id"], following)
        await self.connection.receive(
            SimpleNamespace(type=WSMsgType.BINARY, data=struct.pack("!I", 0) + b"\0\0")
        )
        fresh_provider.send.assert_awaited_once_with(b"\0\0")
        self.assertIs(self.connection.input, fresh)

    async def test_recognition_failure_cancels_prepared_input_before_reporting(self):
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        upstream = AsyncMock()
        upstream.finish.side_effect = SpeechError("speech_provider_timeout")
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\0\0")
        await self.connection.finish(stream, ident)
        self.assertIsNone(self.connection.input_id)
        self.assertEqual(self.connection.failed_input_id, ident)
        self.assertEqual([r.action for r in self.calls], ["cancel"])
        self.assertEqual(
            self.messages[-1],
            {
                "type": "error",
                "request_id": ident,
                "operation": "input.finish",
                "code": "speech_provider_timeout",
            },
        )
        upstream.close.assert_awaited_once()

    async def test_cache_index_is_a_small_completed_metadata_window_not_a_browsing_cursor(self):
        items = [
            {
                "id": "input",
                "role": "user",
                "cursor": "a",
                "status": "complete",
                "updated": 1,
                "preview": "private preview",
                "kind": "message",
            },
            {"id": "old", "role": "assistant", "cursor": "b", "status": "complete", "updated": 2},
            {"id": "live", "role": "assistant", "cursor": "c", "status": "streaming", "updated": 3},
        ]
        self.connection.cursor = 7
        await self.connection.display.cache_index({"items": items, "cursor": 99})
        self.assertEqual(self.connection.cursor, 7)
        index = self.messages[-1]
        self.assertEqual(index["type"], "cache.index")
        self.assertEqual([row["id"] for row in index["items"]], ["input", "old"])
        self.assertNotIn("preview", index["items"][0])
        self.assertEqual(self.calls, [])
        self.owner.settings["offline_cache"] = False
        await self.connection.display.cache_index({"items": items})
        self.assertEqual(len(self.messages), 1)

    async def test_cache_prefetch_uses_the_same_owned_read_and_preserves_presentation_metadata(self):
        async def invoke(request, **kwargs):
            self.calls.append(request)
            self.assertEqual(request.action, "message")
            self.assertEqual(
                request.arguments, {"session_id": "session", "message_id": "owned", "offset": 80, "limit": 80}
            )
            return {
                "message_id": "owned",
                "offset": 80,
                "total": 100,
                "next_offset": None,
                "text": "只读正文",
                "updated": 3,
                "role": "user",
                "status": "complete",
                "images": [],
            }

        self.context.invoke = invoke
        await self.connection.command(
            {"type": "message.page", "message_id": "owned", "offset": 80, "prefetch": True}
        )
        self.assertTrue(self.messages[-1]["prefetch"])
        self.assertEqual(self.messages[-1]["role"], "user")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, "prefetch"):
            await self.connection.command({"type": "message.page", "message_id": "owned", "prefetch": "true"})
        self.assertEqual(len(self.calls), 1)

    async def test_background_cache_read_failure_does_not_fail_foreground_or_retry(self):
        async def invoke(request, **kwargs):
            self.calls.append(request)
            raise ValueError("message is not in this session")

        self.context.invoke = invoke
        args = {"type": "message.page", "message_id": "not-owned", "offset": 0}
        await self.connection.command({**args, "prefetch": True})
        self.assertEqual(self.messages[-1]["type"], "cache.prefetch_unavailable")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, "not in this session"):
            await self.connection.command(args)

    async def test_completed_reply_refreshes_small_index_without_skipping_events(self):
        actions = []
        message = {"id": "new", "role": "assistant", "status": "complete", "text": "结果。", "updated": 1}

        async def invoke(request, **kwargs):
            actions.append(request.action)
            if request.action == "state":
                return {"queue": []}
            if request.action == "events":
                return {"events": [{"data": message}], "cursor": 7}
            self.assertEqual(request.action, "history_index")
            self.assertEqual(request.arguments["limit"], 3)
            return {"items": [{"id": "new", "role": "assistant", "cursor": "bounded"}], "cursor": 99}

        self.context.invoke = invoke
        task = self.connection.spawn(self.connection.watch(), "index-test")
        async with asyncio.timeout(1):
            while not any(m.get("type") == "history.index" for m in self.messages):
                await asyncio.sleep(0)
        pushed = next(m for m in self.messages if m.get("type") == "history.index")
        self.assertTrue(pushed["pushed"])
        self.assertEqual(pushed["items"][0]["id"], "new")
        self.assertEqual(self.connection.cursor, 7)
        self.assertEqual(actions, ["state", "events", "history_index"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_failed_capture_drains_late_pcm_without_overwriting_reason_or_accepting_unknown_id(self):
        ident, following, unknown = (str(uuid.uuid4()) for _ in range(3))
        upstream = AsyncMock()
        stream = InputStream(upstream, 1, lambda _: None)
        for sequence in range(50):
            await stream.accept(sequence, b"\0\0" * 320)
        upstream.reset_mock()
        self.connection.input, self.connection.input_id = stream, ident
        fresh = InputStream(
            SimpleNamespace(send=AsyncMock(), finish=AsyncMock(return_value="新输入"), close=AsyncMock()),
            1,
            lambda _: None,
        )
        frames = [
            (WSMsgType.BINARY, struct.pack("!I", 50) + b"\0\0" * 320),
            (WSMsgType.BINARY, struct.pack("!I", 51) + b"\0\0" * 320),
            (WSMsgType.TEXT, {"type": "input.finish", "request_id": ident, "frames": 52}),
            (WSMsgType.TEXT, {"type": "input.cancel", "request_id": ident}),
            (WSMsgType.TEXT, {"type": "input.finish", "request_id": unknown, "frames": 0}),
            (WSMsgType.TEXT, {"type": "input.begin", "request_id": following}),
            (WSMsgType.BINARY, struct.pack("!I", 0) + b"\0\0" * 320),
        ]
        socket = self.ws

        class Frames:
            send_str = staticmethod(socket.send_str)
            send_bytes = staticmethod(socket.send_bytes)
            close = staticmethod(socket.close)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not frames:
                    raise StopAsyncIteration
                kind, data = frames.pop(0)
                return SimpleNamespace(type=kind, data=json.dumps(data) if kind == WSMsgType.TEXT else data)

        async def invoke(request, **kwargs):
            self.calls.append(request)
            if isinstance(request, ConversationInputRequest):
                return (
                    {"state": "prepared", "work_id": "work"}
                    if request.action == "prepare"
                    else {"state": "cancelled"}
                )
            if isinstance(request, SpeechRequest):
                return fresh
            if request.action == "state":
                return {"queue": []}
            if request.action == "events":
                return {"cursor": 0, "events": []}
            return {"cursor": 0, "items": []}

        self.context.invoke = invoke
        self.connection.ws = Frames()
        await self.connection.run()
        await asyncio.sleep(0)
        errors = [m for m in self.messages if isinstance(m, dict) and m.get("type") == "error"]
        self.assertEqual(
            [(m["code"], m["request_id"]) for m in errors],
            [
                ("speech_input_limit", ident),
                ("speech_input_closed", ident),
                ("terminal_input_identity_mismatch", None),
            ],
        )
        ready = next(m for m in self.messages if isinstance(m, dict) and m.get("type") == "input.ready")
        self.assertEqual(ready["max_audio_bytes"], 32000)
        self.assertIsNone(self.connection.failed_input_id)
        self.assertEqual(fresh.size, 640)
        upstream.send.assert_not_awaited()
        upstream.finish.assert_not_awaited()
        self.assertFalse(
            any(
                isinstance(r, ConversationInputRequest) and r.action in {"commit", "confirm"}
                for r in self.calls
            )
        )

    async def test_restored_messages_are_explicit_and_never_start_speech(self):
        page = {
            "message_id": "prior",
            "role": "assistant",
            "text": "之前的回复。",
            "total": 6,
            "offset": 0,
            "next_offset": None,
            "updated": 1,
            "status": "complete",
            "images": [],
            "kind": "message",
        }
        self.context.invoke = AsyncMock(return_value=page)
        await self.connection.display.restore_message(
            {"id": "prior", "role": "assistant", "status": "complete"}
        )
        self.assertTrue(self.messages[-1]["restored"])
        self.assertEqual(self.messages[-1]["text"], "之前的回复。")
        self.assertIsNone(self.connection.play_task)
        self.assertEqual(self.calls, [])

    async def asyncSetUp(self):
        self.messages = []
        self.calls = []
        self.db = sqlite3.connect(":memory:")

        async def send(value):
            self.messages.append(value)

        async def send_text(value):
            await send(json.loads(value))

        self.ws = SimpleNamespace(send_str=send_text, send_bytes=send, close=AsyncMock())

        async def invoke(request, **kwargs):
            self.calls.append(request)
            if isinstance(request, ConversationInputRequest):
                return {"state": "prepared", "work_id": "work"}
            raise AssertionError(type(request))

        self.context = SimpleNamespace(
            invoke=invoke,
            spawn=lambda coro, _: asyncio.create_task(coro),
            log=logging.getLogger("test"),
            available=lambda _: False,
        )
        self.owner = SimpleNamespace(
            context=self.context, devices=Devices(self.db), settings={"speech_mode": "brief"}
        )
        self.owner.settings_for = lambda device: self.owner.settings
        self.owner.configuration_for = lambda device: self.owner.devices.configuration(
            device, self.owner.settings
        )
        self.connection = Connection(self.owner, "device", "session", self.ws)

    async def asyncTearDown(self):
        await self.connection.close()
        self.db.close()

    async def test_missing_tail_frame_never_finishes_or_submits(self):
        upstream = AsyncMock()
        upstream.finish.return_value = "never submit"
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\x00\x00")
        self.connection.input = stream
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        with self.assertRaises(SpeechError):
            await self.connection.command({"type": "input.finish", "request_id": ident, "frames": 2})
        upstream.finish.assert_not_awaited()
        self.assertFalse(self.calls)

    async def test_idle_connection_uses_change_signal_instead_of_busy_polling(self):
        queried = []

        async def invoke(request, **kwargs):
            queried.append(request.action)
            if request.action == "state":
                return {"queue": []}
            if request.action == "events":
                return {"events": [], "cursor": 0}
            raise AssertionError(request.action)

        self.context.invoke = invoke
        task = self.connection.spawn(self.connection.watch(), "test-watch")
        async with asyncio.timeout(1):
            while len(queried) < 2:
                await asyncio.sleep(0)
        await asyncio.sleep(0.6)
        self.assertEqual(queried, ["state", "events"])
        self.connection.changed.set()
        self.connection.changed.set()
        async with asyncio.timeout(1):
            while len(queried) < 4:
                await asyncio.sleep(0)
        self.assertEqual(queried, ["state", "events", "state", "events"])
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_controls_and_duplicate_begin_do_not_cancel_active_recording(self):
        stream = AsyncMock()
        ident = str(uuid.uuid4())
        self.connection.input, self.connection.input_id = stream, ident
        commands = [
            {"type": "image.chunk", "version": "missing", "offset": 0},
            {"type": "input.begin", "request_id": ident, "mode": "steer"},
            {"type": ["malformed"]},
        ]

        socket = self.ws

        class Frames:
            def __init__(self):
                self.send_str = socket.send_str
                self.send_bytes = socket.send_bytes
                self.close = socket.close

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not commands:
                    raise StopAsyncIteration
                return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(commands.pop(0)))

        async def invoke(request, **kwargs):
            if isinstance(request, ConversationInputRequest):
                return {"state": "cancelled"}
            if request.action == "state":
                return {"queue": []}
            if request.action == "events":
                return {"cursor": 0, "events": []}
            self.assertEqual(request.action, "history_index")
            return {"cursor": 0, "items": []}

        self.context.invoke = invoke
        self.connection.ws = Frames()
        await self.connection.run()
        self.assertIs(self.connection.input, stream)
        stream.close.assert_not_awaited()
        errors = [m for m in self.messages if isinstance(m, dict) and m.get("type") == "error"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(m["request_id"] is None for m in errors))

    async def test_confirmed_sentence_speaks_before_message_end_and_stop_is_permanent(self):
        spoken = []
        body = "第一句。后续还没结束"

        async def invoke(request, **kwargs):
            if isinstance(request, SpeechRequest):
                spoken.append(request.text)
                return self.speech_stream()
            offset = request.arguments.get("offset", 0)
            return {"text": body[offset : offset + request.arguments["limit"]], "work_id": "bound-work"}

        self.context.invoke = invoke
        self.connection.work = "bound-work"
        message = {
            "id": "answer",
            "updated": 1,
            "role": "assistant",
            "text": body,
            "status": "streaming",
            "presentation": {
                "text_ranges": [
                    {"start": 0, "end": len(body), "block": 0, "phase": "final", "evidence": "provider"}
                ]
            },
        }
        await self.connection.present(message, speak=True)
        await self.allow_playback(self.connection.generation)
        await self.wait_for_playback(lambda: any(isinstance(msg, bytes) for msg in self.messages))
        self.assertEqual(spoken, ["第一句。"])
        await self.connection.stop_playback()
        await self.connection.present({**message, "updated": 2, "status": "complete"}, speak=True)
        self.assertEqual(spoken, ["第一句。"])

    async def test_full_speech_is_paged_and_metadata_updates_do_not_repeat_it(self):
        self.owner.settings["speech_mode"] = "full"
        body = "**标题** ✨\n" + "长" * 230 + "，下一节 [说明](https://example.test/a) 正文。" + "尾" * 205
        spoken, sizes = [], []

        async def invoke(request, **kwargs):
            if isinstance(request, SpeechRequest):
                spoken.append(request.text)
                return self.speech_stream()
            sizes.append(request.arguments["limit"])
            offset = request.arguments["offset"]
            return {"text": body[offset : offset + request.arguments["limit"]], "work_id": "bound-work"}

        self.context.invoke = invoke
        self.connection.work = "bound-work"
        message = {
            "id": "full-answer",
            "updated": 1,
            "role": "assistant",
            "text": body,
            "status": "complete",
            "presentation": {"phase": "final"},
        }
        await self.connection.present(message, speak=True)
        generation = self.connection.generation
        await self.allow_playback(generation)
        await self.wait_for_playback(lambda: self.playback_messages("playback.end"))
        await self.connection.command({"type": "playback.completed", "generation": generation})
        await asyncio.wait_for(self.connection.play_task, 1)
        expected = "标题" + "长" * 230 + "，下一节说明正文。" + "尾" * 205
        self.assertEqual("".join(spoken).replace(" ", ""), expected)
        self.assertTrue(all(n <= 200 for n in sizes + list(map(len, spoken))))
        self.assertEqual(self.playback_messages("playback.end")[0]["next_offset"], len(body))
        self.assertEqual(self.playback_messages("message")[0]["text"], body[:160])
        await self.connection.present({**message, "updated": 2}, speak=True)
        self.assertEqual("".join(spoken).replace(" ", ""), expected)

    async def test_brief_long_sentence_and_symbol_only_reply(self):
        spoken = []
        body = "✨" + "长" * 260

        async def invoke(request, **_):
            if isinstance(request, SpeechRequest):
                spoken.append(request.text)
                return self.speech_stream()
            start = request.arguments["offset"]
            return {"text": body[start : start + request.arguments["limit"]], "work_id": "bound-work"}

        self.context.invoke = invoke
        self.connection.work = "bound-work"
        reply = {"id": "brief", "role": "assistant", "text": body, "status": "complete"}
        await self.connection.present(reply, speak=True)
        generation = self.connection.generation
        await self.allow_playback(generation)
        await self.wait_for_playback(lambda: self.playback_messages("playback.end"))
        await self.connection.command({"type": "playback.completed", "generation": generation})
        await asyncio.wait_for(self.connection.play_task, 1)
        self.assertEqual("".join(spoken), "长" * 200)
        self.assertEqual(self.playback_messages("playback.finished")[-1]["reason"], "brief_limit")
        body = "✨---\n**🔥**"
        await self.connection.present({**reply, "id": "symbols", "text": body}, speak=True)
        await asyncio.wait_for(self.connection.play_task, 1)
        self.assertEqual(len(self.playback_messages("playback.start")), 1)
        self.assertFalse(self.playback_messages("playback.unavailable"))

    async def test_commentary_is_not_spoken_even_if_message_ends_without_tools(self):
        self.connection.work = "bound-work"
        message = {
            "id": "commentary",
            "updated": 1,
            "role": "assistant",
            "text": "我去查一下。",
            "status": "complete",
            "presentation": {
                "phase": "final",
                "text_ranges": [
                    {"start": 0, "end": 6, "block": 0, "phase": "commentary", "evidence": "provider"}
                ],
            },
        }
        await self.connection.present(message, speak=True)
        self.assertIsNone(self.connection.play_task)

    async def test_phase_update_with_same_timestamp_and_failed_turn_stop(self):
        async def invoke(request, **kwargs):
            if isinstance(request, SpeechRequest):
                return self.speech_stream()
            return {"text": "回答。", "work_id": "work"}

        self.context.invoke = invoke
        self.connection.work = "work"
        message = {
            "id": "phase",
            "updated": 1,
            "role": "assistant",
            "status": "streaming",
            "text": "回答。",
            "presentation": {
                "text_ranges": [{"start": 0, "end": 3, "block": 0, "phase": "unknown", "evidence": "pending"}]
            },
        }
        await self.connection.present(message, speak=True)
        self.assertIsNone(self.connection.play_task)
        final = {
            **message,
            "presentation": {
                "text_ranges": [{"start": 0, "end": 3, "block": 0, "phase": "final", "evidence": "provider"}]
            },
        }
        await self.connection.present(final, speak=True)
        await self.allow_playback(self.connection.generation)
        await self.connection.present({**final, "status": "unknown"}, speak=True)
        self.assertIsNone(self.connection.voice)
        self.assertEqual(
            self.db.execute("SELECT state FROM passport_playbacks WHERE message='phase'").fetchone()[0],
            "stopped",
        )

    async def test_image_chunks_are_bounded_versioned_and_replaced(self):
        import base64

        async def invoke(request, **kwargs):
            self.assertEqual(request.arguments["session_id"], "session")
            return {
                "image_id": "image",
                "version": "one",
                "width": 160,
                "height": 96,
                "format": "rgb565le",
                "data": b"\x00\xf8" * 160 * 96,
                "bytes": 30720,
            }

        self.context.invoke = invoke
        await self.connection.command({"type": "image.open", "message_id": "photo", "image_id": "image"})
        self.assertNotIn("data", self.messages[-1])
        await self.connection.command({"type": "image.chunk", "version": "one", "offset": 0})
        self.assertEqual(len(base64.b64decode(self.messages[-1]["data"])), 2048)
        with self.assertRaises(ValueError):
            await self.connection.command({"type": "image.chunk", "version": "stale", "offset": 0})
        await self.connection.command({"type": "image.close"})
        with self.assertRaises(ValueError):
            await self.connection.command({"type": "image.chunk", "version": "one", "offset": 0})

    async def test_hd_image_rows_are_bounded_checked_and_scoped_to_the_active_request(self):
        import hashlib

        pixels = bytes(range(256)) * 206
        pixels = pixels[: 216 * 122 * 2]
        self.context.invoke = AsyncMock(
            return_value={
                "image_id": "photo",
                "version": hashlib.sha256(pixels).hexdigest(),
                "width": 216,
                "height": 122,
                "format": "rgb565le",
                "data": pixels,
                "bytes": len(pixels),
            }
        )
        open_image = {
            "type": "image.stream.open",
            "request_id": 4,
            "message_id": "row",
            "image_id": "photo",
            "width": 216,
            "height": 194,
        }
        await self.connection.command(open_image)
        self.assertEqual(self.context.invoke.call_args.args[0].action, "image_raster")
        self.assertNotIn("data", self.messages[-1])
        await self.connection.command({**open_image, "request_id": 5})
        await self.connection.command({"type": "image.stream.close", "request_id": 4})
        await self.connection.command({"type": "image.stream.next", "request_id": 4, "row": 0})
        self.assertEqual(self.connection.display.stream[0], 5)
        with self.assertRaises(ValueError):
            await self.connection.command({"type": "image.stream.next", "request_id": 5, "row": 8})

        sent = self.connection.ws.send_bytes = AsyncMock()
        for row in range(0, 122, 8):
            await self.connection.command({"type": "image.stream.next", "request_id": 5, "row": row})
            frame = sent.await_args.args[0]
            tag, request, start, count, digest = struct.unpack("!4sIHH8s", frame[:20])
            self.assertEqual((tag, request, start, count), (b"\xff" * 4, 5, row, min(8, 122 - row)))
            self.assertEqual(frame[20:], pixels[row * 432 : (row + count) * 432])
            self.assertEqual(digest, hashlib.sha256(frame[20:]).digest()[:8])
            self.assertLessEqual(len(frame), 3476)
        self.assertIsNone(self.connection.display.stream)

    async def test_over_limit_transcript_waits_for_explicit_confirmation(self):
        async def invoke(request, **kwargs):
            self.calls.append(request)
            return {
                "state": "review" if request.action in {"review", "receipt"} else "accepted",
                "work_id": "work",
            }

        self.context.invoke = invoke
        upstream = AsyncMock()
        upstream.finish.return_value = "尚未说完的完整已识别部分。"
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\x00\x00")
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        await self.connection.finish(stream, ident, True)
        self.assertEqual([r.action for r in self.calls], ["review"])
        self.assertEqual(self.messages[-1]["type"], "input.confirmation")
        await self.connection.command({"type": "input.confirm", "request_id": ident})
        self.assertEqual([r.action for r in self.calls], ["review", "confirm"])
        self.assertEqual(self.calls[0].arguments["text"], "尚未说完的完整已识别部分。")
        self.assertNotIn("text", self.calls[1].arguments)

    async def test_long_transcript_projection_is_bounded_and_commit_keeps_all_text(self):
        upstream = AsyncMock()
        text = "一段很长的输入。" * 900
        upstream.finish.return_value = text
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\x00\x00")
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        await self.connection.finish(stream, ident)
        shown = next(i for i in self.messages if i["type"] == "transcript")
        self.assertEqual(shown["text"], text[-240:])
        self.assertEqual(shown["offset"], len(text) - 240)
        self.assertEqual(shown["total"], len(text))
        self.assertEqual(next(r for r in self.calls if r.action == "commit").arguments["text"], text)

    async def test_reconnect_restores_review_without_cancel_or_retranscribing(self):
        async def invoke(request, **kwargs):
            self.calls.append(request)
            if request.action == "transcript":
                return {
                    "state": "review",
                    "text": "待确认的文字",
                    "offset": 0,
                    "next_offset": None,
                    "total": 6,
                }
            return {"state": "review", "work_id": "work", "text_length": 6}

        self.context.invoke = invoke
        ident = str(uuid.uuid4())
        await self.connection.command({"type": "input.receipt", "request_id": ident})
        self.assertEqual(self.connection.input_id, ident)
        await self.connection.command({"type": "input.transcript", "request_id": ident, "offset": 0})
        self.assertEqual(self.messages[-1]["type"], "input.transcript")
        await self.connection.close()
        self.assertEqual([r.action for r in self.calls], ["receipt", "transcript", "receipt"])
        self.assertNotIn("cancel", [r.action for r in self.calls])

    async def test_task_notice_does_not_interrupt_pending_confirmation(self):
        import time

        async def invoke(request, **kwargs):
            self.assertIsInstance(request, TerminalNotifications)
            self.assertEqual(request.action, "list")
            return {
                "items": [
                    {
                        "id": "notice",
                        "revision": 1,
                        "payload": {"title": "后台结果"},
                        "unread": True,
                        "expired": False,
                        "updated": time.time(),
                    }
                ],
                "before": None,
                "quiet": False,
            }

        self.context.available = lambda _: True
        self.context.invoke = invoke
        self.connection.confirmation = True
        self.connection.last_notices = []
        self.connection.start_playback = AsyncMock()
        await self.connection.notices()
        self.connection.start_playback.assert_not_awaited()
        self.assertEqual(self.messages[-1]["type"], "notifications")

    async def test_discard_confirmation_does_not_commit_or_accept_stale_confirmation(self):
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        self.connection.confirmation = "not submitted"
        await self.connection.command({"type": "input.cancel", "request_id": ident})
        self.assertEqual([r.action for r in self.calls], ["cancel", "receipt"])
        self.assertIsNone(self.connection.confirmation)
        with self.assertRaisesRegex(SpeechError, "terminal_input_identity_mismatch"):
            await self.connection.command({"type": "input.confirm", "request_id": ident})
        self.assertNotIn("commit", [r.action for r in self.calls])

    async def test_cancel_before_recognition_task_starts_releases_the_input_slot(self):
        upstream = AsyncMock()
        released = []
        stream = InputStream(upstream, 1, released.append)
        await stream.accept(0, b"\0\0")
        ident = str(uuid.uuid4())
        self.connection.input, self.connection.input_id = stream, ident
        await self.connection.command({"type": "input.finish", "request_id": ident, "frames": 1})
        await self.connection.abort_input()
        upstream.finish.assert_not_awaited()
        upstream.close.assert_awaited_once()
        self.assertEqual(released, [stream])
        self.assertEqual([r.action for r in self.calls], ["cancel"])

    async def test_radio_disconnect_does_not_cancel_in_flight_commit(self):
        entered, allow_commit = asyncio.Event(), asyncio.Event()

        async def invoke(request, **kwargs):
            self.calls.append(request)
            if request.action == "commit":
                entered.set()
                await allow_commit.wait()
            return {"state": "accepted"}

        self.context.invoke = invoke
        upstream = AsyncMock()
        upstream.finish.return_value = "一条新输入"
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\0\0")
        ident = str(uuid.uuid4())
        self.connection.input, self.connection.input_id = stream, ident
        await self.connection.command({"type": "input.finish", "request_id": ident, "frames": 1})
        await asyncio.wait_for(entered.wait(), 2)
        close_task = asyncio.create_task(self.connection.close())
        try:
            while self.connection.closing is None:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(self.connection.finish_task.cancelling())
        finally:
            allow_commit.set()
            await asyncio.wait_for(close_task, 2)
        self.assertEqual([r.action for r in self.calls], ["commit", "receipt"])

    async def test_cancel_while_finalizing_discards_late_transcript(self):
        upstream = AsyncMock()
        started = asyncio.Event()

        async def slow_finish():
            started.set()
            await asyncio.Event().wait()

        upstream.finish.side_effect = slow_finish
        stream = InputStream(upstream, 1, lambda _: None)
        await stream.accept(0, b"\x00\x00")
        ident = str(uuid.uuid4())
        self.connection.input_id = ident
        self.connection.finish_task = self.connection.spawn(self.connection.finish(stream, ident), "test")
        await started.wait()
        await self.connection.abort_input()
        self.assertTrue(self.connection.finish_task.cancelled())
        self.assertTrue(stream.closed)
        self.assertNotIn("commit", [r.action for r in self.calls])

    async def test_device_cannot_choose_session_or_unbounded_credit(self):
        with self.assertRaises(ValueError):
            await self.connection.command(
                {"type": "input.receipt", "request_id": str(uuid.uuid4()), "session_id": "web"}
            )
        with self.assertRaises(ValueError):
            await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8194})
        await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8192})
        with self.assertRaises(ValueError):
            await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 2})
        await self.connection.stop_playback()
        await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8192})
        self.assertEqual(self.connection.credit, 0)

    async def test_interruption_cancels_audio_and_never_business_execution(self):
        async def forever():
            await asyncio.Event().wait()

        task = self.connection.spawn(forever(), "playback")
        self.connection.play_task = task
        await self.connection.stop_playback()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.calls)
        self.assertEqual(self.messages[-1]["type"], "playback.stop")

    @staticmethod
    def speech_stream():
        stream = AsyncMock()
        stream.__aiter__.return_value = [b"\x02\x00"]
        return stream

    async def allow_playback(self, generation):
        await self.wait_for_playback(lambda: self.playback_messages("playback.start"))
        await self.connection.command({"type": "playback.credit", "generation": generation, "bytes": 8192})

    def start_sentence_playback(self, *, single_slot=False, block_prefetched=False, fail_next=False):
        sentences = ("首" * 120 + "。", "次" * 120 + "。")
        text = "\n".join(sentences)
        finish_current = asyncio.Event()
        syntheses = []

        class Stream:
            def __init__(self, number):
                self.number = number
                self.reads = 0
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.number == 1 and self.reads == 1:
                    await finish_current.wait()
                elif self.number == 2 and block_prefetched:
                    await asyncio.Event().wait()
                if self.number == 2 and fail_next:
                    raise SpeechError("speech_provider_rejected:CLIENT_ERROR")
                if self.reads >= 2:
                    raise StopAsyncIteration
                self.reads += 1
                return bytes((self.number, 0)) * 240

            async def aclose(self):
                self.closed = True

        streams = [Stream(1), Stream(2)]

        async def invoke(request, **_):
            if isinstance(request, SpeechRequest):
                syntheses.append(request.text)
                self.assertEqual(request.scope, UsageScope("work", "terminal:device"))
                index = sentences.index(request.text)
                if index == 1 and single_slot and not streams[0].closed:
                    raise SpeechError("speech_busy")
                return streams[index]
            offset = request.arguments["offset"]
            return {"text": text[offset : offset + request.arguments["limit"]], "work_id": "work"}

        self.context.invoke = invoke
        voice = {
            "id": "reply",
            "ranges": [{"start": 0, "end": len(text)}],
            "complete": True,
            "offset": 0,
            "spoken": 0,
            "settings": {"speech_mode": "full"},
        }
        self.owner.devices.claim_playback("device", "reply", 0)
        self.connection.voice = voice
        self.connection.play_task = self.connection.spawn(self.connection.speak_foreground(voice), "test")
        return streams, syntheses, finish_current

    async def wait_for_playback(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.001)

    def playback_messages(self, kind):
        return [msg for msg in self.messages if isinstance(msg, dict) and msg["type"] == kind]

    async def test_prefetch_reads_one_frame_and_joins_segments_without_a_device_receipt(self):
        streams, syntheses, finish_current = self.start_sentence_playback()
        await self.allow_playback(0)
        await self.wait_for_playback(lambda: streams[1].reads == 1)
        self.assertFalse(streams[0].closed)
        self.assertEqual(len(syntheses), 2)
        self.assertEqual(len(self.playback_messages("playback.start")), 1)
        finish_current.set()
        await self.wait_for_playback(lambda: self.playback_messages("playback.end"))
        self.assertEqual([msg[4] for msg in self.messages if isinstance(msg, bytes)], [1, 1, 2, 2])
        await self.connection.command({"type": "playback.completed", "generation": 0})
        await asyncio.wait_for(self.connection.play_task, 2)
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(len(self.playback_messages("playback.finished")), 1)

    async def test_failed_second_segment_drains_receipt_and_does_not_regenerate(self):
        streams, syntheses, finish_current = self.start_sentence_playback(fail_next=True)
        await self.allow_playback(0)
        await self.wait_for_playback(lambda: streams[1].closed)
        finish_current.set()
        await self.wait_for_playback(lambda: self.playback_messages("playback.end"))
        self.assertFalse(self.playback_messages("playback.unavailable"))
        self.assertEqual([msg[4] for msg in self.messages if isinstance(msg, bytes)], [1, 1])
        await self.connection.command({"type": "playback.completed", "generation": 0})
        await asyncio.wait_for(self.connection.play_task, 2)
        self.assertEqual(len(syntheses), 2)
        self.assertFalse(self.playback_messages("playback.receipt")[0]["played"])
        self.assertEqual(
            self.playback_messages("playback.unavailable")[0]["code"], "speech_provider_rejected:CLIENT_ERROR"
        )

    async def test_stop_closes_the_current_and_prefetched_stream_before_accepting_late_receipts(self):
        streams, _, _ = self.start_sentence_playback()
        await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8192})
        await self.wait_for_playback(lambda: streams[1].reads == 1)
        await self.connection.command({"type": "playback.stop"})
        await self.connection.command({"type": "playback.completed", "generation": 0})
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(self.connection.generation, 1)
        self.assertEqual(len(self.playback_messages("playback.start")), 1)
        self.assertFalse(self.playback_messages("playback.finished"))
        self.assertEqual([msg[4] for msg in self.messages if isinstance(msg, bytes)], [1])

    async def test_disconnect_cancels_prefetch_still_waiting_for_first_audio(self):
        streams, syntheses, _ = self.start_sentence_playback(block_prefetched=True)
        await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8192})
        await self.wait_for_playback(lambda: len(syntheses) == 2)
        await self.connection.close()
        self.assertTrue(all(s.closed for s in streams))
        self.assertEqual(streams[1].reads, 0)
        self.assertEqual(len(self.playback_messages("playback.start")), 1)

    async def test_prefetch_busy_waits_for_a_slot_without_interrupting_current_sentence(self):
        streams, syntheses, finish_current = self.start_sentence_playback(single_slot=True)
        await self.connection.command({"type": "playback.credit", "generation": 0, "bytes": 8192})
        await self.wait_for_playback(lambda: len(syntheses) == 2)
        self.assertEqual(streams[1].reads, 0)
        finish_current.set()
        await self.wait_for_playback(lambda: self.playback_messages("playback.end"))
        await self.connection.command({"type": "playback.completed", "generation": 0})
        self.assertEqual(len(self.playback_messages("playback.start")), 1)
        self.assertEqual(len(syntheses), 3)
        self.assertEqual(syntheses[1], syntheses[2])
        self.assertFalse(self.playback_messages("playback.unavailable"))


@unittest.skipUnless(__import__("sys").platform == "linux", "Requires Linux-certified speech_io")
class RealPiTerminalTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_pi_with_synthetic_voice_and_duplicate_input(self):
        import tempfile
        import struct
        import threading
        from aiohttp import ClientSession
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.config import Config
        from assistant_runtime.core.conversation.sessions import Sessions
        from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
        import test_pi_skeleton as fixtures

        provider = ProviderProtocolTest("test_final_only_and_streamed_output_share_work")
        await provider.asyncSetUp()
        read_path = None
        release_final = threading.Event()
        pending_final = threading.Event()
        reply = "收到，测试完成。\n可以继续。"

        def answer(step, body):
            payload = json.dumps(body, ensure_ascii=False)
            self.assertIn("用户可在终端录音", payload)
            self.assertIn("一到三个短句", payload)
            self.assertIn("每项只写一个动作", payload)
            self.assertNotIn("当前语音未启用", payload)
            if step == 1:
                return sse_tool_call("read-synthetic", "read", {"path": str(read_path)})
            self.assertIn("SYNTHETIC_PP_READ", payload)
            events = sse_text_reply("answer", reply)
            for event in events:
                if event.get("item", {}).get("type") == "message":
                    event["item"]["phase"] = "final_answer"

            def streaming():
                for event in events:
                    yield event
                    if event["type"] == "response.output_text.delta":
                        pending_final.set()
                        if not release_final.wait(15):
                            raise AssertionError(
                                "Pi did not forward the confirmed sentence before message completion"
                            )

            return streaming()

        gateway = FakeResponsesGateway(answer)
        host = running = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                read_path = root / "synthetic.txt"
                read_path.write_text("SYNTHETIC_PP_READ")
                (root / ".env").write_text("TEST_KEY=synthetic-key\nDASHSCOPE_API_KEY=synthetic-key\n")
                config = Config(
                    root=root,
                    workspace=directory,
                    plugins=[
                        "ai_usage",
                        "models",
                        "workfiles",
                        "pi",
                        "execution",
                        "conversation",
                        "speech_io",
                        "aliyun_speech",
                        "ai_passport",
                    ],
                    main_model="model-a",
                    main_reasoning_effort="auto",
                    model_api=fixtures.configuration(gateway.base_url),
                )
                host = AssistantService(config)
                sessions = Sessions(host.store)
                legacy_policy = {"capture_memory": False, "instructions": "Retain my existing preference."}
                legacy = sessions.create("Previously paired PP", policy=legacy_policy)["id"]
                devices = Devices(host.store.db)
                legacy_code = devices.begin(uuid.uuid4().hex, "legacy-credential")["code"]
                devices.activate(legacy_code, "Previously paired PP", legacy)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 30)
                self.assertEqual(host.runtime.health()["ai_passport"]["state"], "ready")
                self.assertEqual(sessions.policy(legacy), {**legacy_policy, "presentation": "voice_terminal"})
                self.assertEqual(sessions.policy(sessions.bound("web")), {})
                speech = host.runtime.plugins["aliyun_speech"].instance
                speech.settings = {**speech.settings, "endpoint": provider.context.config["endpoint"]}
                frontend = host.http_frontends["ai_passport"]
                endpoint = frontend.endpoint
                headers = {"X-PA-Frontend-Key": endpoint["key"]}
                base = f"http://{endpoint['host']}:{endpoint['port']}"
                async with ClientSession() as client:
                    body = {
                        "device_id": uuid.uuid4().hex,
                        "credential": "synthetic-device-key-" * 3,
                        "protocol": 2,
                    }
                    async with client.post(base + "/pair", json=body, headers=headers) as response:
                        self.assertEqual(response.status, 200)
                        challenge = await response.json()
                    pair = await host.runtime.plugins["ai_passport"].instance.configuration_action(
                        "pair", {"name": "Synthetic PP", "code": challenge["code"]}
                    )
                    self.assertEqual(
                        sessions.policy(pair["session_id"]),
                        {"capture_memory": True, "presentation": "voice_terminal"},
                    )
                    headers.update(
                        {"Authorization": "Bearer " + body["credential"], "X-Device-Id": pair["device_id"]}
                    )

                    async def receive_kind(socket, kind):
                        async with asyncio.timeout(10):
                            while True:
                                frame = await socket.receive()
                                self.assertEqual(frame.type, WSMsgType.TEXT, frame)
                                data = json.loads(frame.data)
                                self.assertNotEqual(data["type"], "error", data)
                                if data["type"] == kind:
                                    return data

                    draft = str(uuid.uuid4())
                    async with client.ws_connect(base + "/stream", headers=headers) as socket:
                        await socket.send_json({"type": "input.begin", "request_id": draft, "mode": "steer"})
                        await receive_kind(socket, "input.ready")
                        await socket.send_bytes(struct.pack("!I", 0) + b"\x00\x00" * 320)
                        await socket.send_json(
                            {
                                "type": "input.finish",
                                "request_id": draft,
                                "frames": 1,
                                "confirmation_required": True,
                            }
                        )
                        await receive_kind(socket, "input.confirmation")
                        self.assertFalse(gateway.requests)
                    terminal = host.runtime.plugins["ai_passport"].instance
                    async with asyncio.timeout(5):
                        while terminal.connections:
                            await asyncio.sleep(0.001)
                    async with client.ws_connect(base + "/stream", headers=headers) as ws:
                        await ws.send_json({"type": "input.receipt", "request_id": draft})
                        receipt = await receive_kind(ws, "input.receipt")
                        self.assertEqual(receipt["state"], "review")
                        await ws.send_json({"type": "input.transcript", "request_id": draft, "offset": 0})
                        page = await receive_kind(ws, "input.transcript")
                        self.assertEqual(page["text"], "最终转写")
                        self.assertFalse(gateway.requests)
                        await ws.send_json({"type": "input.cancel", "request_id": draft})
                        receipt = await receive_kind(ws, "input.receipt")
                        self.assertEqual(receipt["state"], "cancelled")
                        ident = str(uuid.uuid4())
                        completed = asyncio.Event()
                        heard = bytearray()
                        accepted = 0
                        played_segments = 0
                        message_seen = False
                        early_speech = False
                        await ws.send_json({"type": "input.begin", "request_id": ident, "mode": "steer"})
                        async with asyncio.timeout(45):
                            while not completed.is_set():
                                event = await ws.receive()
                                if event.type == WSMsgType.BINARY:
                                    generation = struct.unpack("!I", event.data[:4])[0]
                                    heard.extend(event.data[4:])
                                    await ws.send_json(
                                        {
                                            "type": "playback.credit",
                                            "generation": generation,
                                            "bytes": len(event.data) - 4,
                                        }
                                    )
                                    continue
                                self.assertEqual(event.type, WSMsgType.TEXT)
                                data = json.loads(event.data)
                                kind = data["type"]
                                self.assertNotEqual(kind, "error", data)
                                if kind == "input.ready":
                                    await ws.send_bytes(struct.pack("!I", 0) + b"\x00\x00" * 320)
                                    await ws.send_json(
                                        {"type": "input.finish", "request_id": ident, "frames": 1}
                                    )
                                if kind == "input.receipt" and data["state"] == "accepted":
                                    accepted += 1
                                    if accepted == 1:
                                        await ws.send_json(
                                            {"type": "input.begin", "request_id": ident, "mode": "steer"}
                                        )
                                if kind == "message":
                                    self.assertIn(data["status"], {"streaming", "complete"}, data)
                                    self.assertIn(reply, data["text"])
                                    message_seen |= data["status"] == "complete"
                                if kind == "playback.start":
                                    early_speech |= (
                                        pending_final.is_set()
                                        and not release_final.is_set()
                                        and not message_seen
                                    )
                                    release_final.set()
                                    await ws.send_json(
                                        {
                                            "type": "playback.credit",
                                            "generation": data["generation"],
                                            "bytes": 8192,
                                        }
                                    )
                                if kind == "playback.end":
                                    played_segments += 1
                                    await ws.send_json(
                                        {"type": "playback.completed", "generation": data["generation"]}
                                    )
                                if kind == "playback.finished":
                                    completed.set()
                        self.assertTrue(message_seen)
                        self.assertTrue(early_speech)
                        self.assertEqual(played_segments, 1)
                        self.assertEqual(len(heard), 320)
                        self.assertEqual(provider.synthesized, ["收到，测试完成。 可以继续。"])
                        self.assertGreaterEqual(accepted, 2)
                        self.assertEqual(len(gateway.requests), 2)
                        binding = await host.ai_usage.service.binding(ident)
                        snapshot = await host.ai_usage.service.snapshot(binding.work_id)
                        # ASR + two Pi requests around a read + one shared TTS request.
                        self.assertEqual(snapshot["requests"], 4)
                        self.assertEqual(snapshot["inflight"], 0)
                        self.assertFalse(host.runtime.plugins["speech_io"].instance.streams)
                        history = await host.runtime.invoke(
                            ConversationRequest("history", {"session_id": pair["session_id"]})
                        )
                        ranges = [
                            r
                            for m in history["messages"]
                            for r in (m.get("presentation") or {}).get("text_ranges", [])
                        ]
                        self.assertTrue(
                            any(r["phase"] == "final" and r["evidence"] == "provider" for r in ranges), ranges
                        )
                host.stop_event.set()
                await asyncio.wait_for(running, 15)
                running = None
        finally:
            release_final.set()
            if running:
                host.stop_event.set()
                try:
                    await asyncio.wait_for(running, 15)
                except BaseException:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            await gateway.aclose()
            await provider.asyncTearDown()
