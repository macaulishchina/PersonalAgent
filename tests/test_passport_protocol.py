"""Synthetic terminal/voice contracts; never contacts a real provider or device."""

import asyncio
import json
import logging
import sqlite3
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from assistant_runtime.api.ai_usage import UsageScope
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import RecognitionRequest, SynthesisRequest, SpeechError
from assistant_runtime.core.speech_io.service import InputStream
from assistant_runtime.plugins.aliyun_speech.plugin import AliyunSpeech
from assistant_runtime.plugins.ai_passport.connection import Connection
from assistant_runtime.plugins.ai_passport.repository import Devices
import test_conversation_sessions as sessions_fixture


class ProviderProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        self.fault = None

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            ident = None
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
                    self.asr = command["payload"]["task"] == "asr"
                    if self.fault:
                        await ws.send_json(
                            {
                                "header": {
                                    "task_id": ident,
                                    "event": "task-failed",
                                    "error_code": self.fault,
                                    "error_message": "secret must not escape",
                                }
                            }
                        )
                    else:
                        await ws.send_json({"header": {"task_id": ident, "event": "task-started"}})
                if action == "finish-task":
                    if self.asr:
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
            ).read_text()
        )["schema"]["properties"]
        settings = {k: v["default"] for k, v in config.items()}
        settings["endpoint"] = str(self.server.make_url("/speech")).replace("http:", "ws:")
        self.usage = SimpleNamespace(admit=AsyncMock(return_value={"id": "ticket"}), settle=AsyncMock())
        self.context = SimpleNamespace(
            config=settings,
            credential=lambda _: "synthetic-key",
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


class TerminalStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = []
        self.calls = []
        self.db = sqlite3.connect(":memory:")

        async def send(value):
            self.messages.append(value)

        self.ws = SimpleNamespace(send_json=send, send_bytes=send, close=AsyncMock())

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
            context=self.context, devices=Devices(self.db), settings={"auto_speak": True}
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


class RealPiTerminalTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_pi_with_synthetic_voice_and_duplicate_input(self):
        import tempfile
        import struct
        from aiohttp import ClientSession
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.config import Config
        from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
        import test_pi_skeleton as fixtures

        provider = ProviderProtocolTest("test_final_only_and_streamed_output_share_work")
        await provider.asyncSetUp()
        read_path = None

        def answer(step, body):
            if step == 1:
                return sse_tool_call("read-synthetic", "read", {"path": str(read_path)})
            self.assertIn("SYNTHETIC_PP_READ", json.dumps(body))
            return sse_text_reply("answer", "收到，测试完成。")

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
                    ai_passport={"public_url": "wss://test.invalid/pa/passport/stream"},
                )
                host = AssistantService(config)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 30)
                self.assertEqual(host.runtime.health()["ai_passport"]["state"], "ready")
                speech = host.runtime.plugins["aliyun_speech"].instance
                speech.settings = {**speech.settings, "endpoint": provider.context.config["endpoint"]}
                frontend = host.http_frontends["ai_passport"]
                endpoint = frontend.endpoint
                pair = await frontend.management("pair", {"name": "Synthetic PP"})
                headers = {"X-PA-Frontend-Key": endpoint["key"]}
                base = f"http://{endpoint['host']}:{endpoint['port']}"
                async with ClientSession() as client:
                    body = {
                        key: pair[key] for key in ("device_id", "pairing_token", "credential", "protocol")
                    }
                    async with client.post(base + "/pair", json=body, headers=headers) as response:
                        self.assertEqual(response.status, 200)
                    headers.update(
                        {"Authorization": "Bearer " + pair["credential"], "X-Device-Id": pair["device_id"]}
                    )
                    async with client.ws_connect(base + "/stream", headers=headers) as ws:
                        ident = str(uuid.uuid4())
                        completed = asyncio.Event()
                        heard = bytearray()
                        accepted = 0
                        message_seen = False
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
                                    self.assertEqual(data["status"], "complete", data)
                                    self.assertIn("收到，测试完成。", data["text"])
                                    message_seen = True
                                if kind == "playback.start":
                                    await ws.send_json(
                                        {
                                            "type": "playback.credit",
                                            "generation": data["generation"],
                                            "bytes": 8192,
                                        }
                                    )
                                if kind == "playback.end":
                                    await ws.send_json(
                                        {"type": "playback.completed", "generation": data["generation"]}
                                    )
                                if kind == "playback.receipt":
                                    completed.set()
                        self.assertTrue(message_seen)
                        self.assertTrue(heard)
                        self.assertGreaterEqual(accepted, 2)
                        self.assertEqual(len(gateway.requests), 2)
                        binding = await host.ai_usage.service.binding(ident)
                        snapshot = await host.ai_usage.service.snapshot(binding.work_id)
                        self.assertEqual(
                            snapshot["requests"], 4
                        )  # ASR + two Pi requests around a read + TTS.
                        self.assertEqual(snapshot["inflight"], 0)
                        self.assertFalse(host.runtime.plugins["speech_io"].instance.streams)
                host.stop_event.set()
                await asyncio.wait_for(running, 15)
                running = None
        finally:
            if running:
                host.stop_event.set()
                try:
                    await asyncio.wait_for(running, 15)
                except BaseException:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            await gateway.aclose()
            await provider.asyncTearDown()
