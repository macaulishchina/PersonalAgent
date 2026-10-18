"""Conversation consistency under duplicate delivery, correction and recovery."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from assistant_runtime.config import Config
from assistant_runtime.conversation.service import Conversation
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.plugins.codex.conversation import task_event
from assistant_runtime.store import Store


class Owned:
    def __init__(self):
        self.tasks = []
        self.cleanups = []

    def spawn(self, coro, label=""):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    def own(self, callback):
        self.cleanups.append(callback)

    async def close(self):
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for c in reversed(self.cleanups):
            await c()


def fixture(root):
    store = Store(root / "assistant.sqlite3")
    backend = SimpleNamespace(
        session_id="session",
        thread_id="provider-thread",
        active_turn=None,
        last_turn=None,
        busy=False,
        client=SimpleNamespace(process=object(), request=AsyncMock(return_value={})),
        submit=AsyncMock(),
        import_voice_messages=AsyncMock(),
        restored_conversation_events=lambda: [],
    )
    host = SimpleNamespace(
        store=store,
        config=Config(root=root, profile="headless"),
        backend=backend,
        voice=SimpleNamespace(
            opened=False,
            epoch=1,
            append_context=AsyncMock(),
            instruct=AsyncMock(),
            say=AsyncMock(),
            finish_execution=Mock(),
            execution_started=Mock(),
            silence_web_reply=Mock(),
        ),
        mode="standby",
        player=SimpleNamespace(playing=False),
        quiet_active=lambda: False,
        memory=None,
        runtime=SimpleNamespace(invoke=AsyncMock()),
    )
    host.conversation = Conversation(host)
    return host


class ConversationStoreTest(unittest.TestCase):
    def test_stream_final_correction_order_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Store(Path(temp) / "db")
            s = ConversationStore(base)
            a = s.put("voice:1", "s", role="user", source="voice", text="请到")
            cursor = s.cursor()
            s.put("reply:1", "s", text="好的")
            s.put("voice:1", "s", role="user", source="voice", text="迟到", status="complete")
            self.assertEqual([m["id"] for m in s.history("s")], ["voice:1", "reply:1"])
            self.assertEqual(s.get("voice:1")["ordinal"], a["ordinal"])
            replay = s.changes("s", cursor)
            self.assertEqual(replay[-1]["data"]["text"], "迟到")
            last = s.cursor()
            s.put("voice:1", "s", role="user", source="voice", text="迟到", status="complete")
            self.assertEqual(s.cursor(), last)
            self.assertIsNone(s.changes("s", last + 100))
            base.db.close()

    def test_restart_marks_unfinished_unknown_and_preserves_final_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            base = Store(path)
            s = ConversationStore(base)
            s.put("a", "s", text="完成", status="complete")
            s.put("b", "s", text="部分回答")
            base.db.close()
            base = Store(path)
            s = ConversationStore(base)
            s.recover("s")
            self.assertEqual(s.get("a")["status"], "complete")
            self.assertEqual(s.get("b")["status"], "unknown")
            self.assertEqual(s.get("b")["text"], "部分回答")
            base.db.close()


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = fixture(Path(self.temp.name))
        self.context = Owned()
        await self.host.conversation.start(self.context)

    async def asyncTearDown(self):
        await self.context.close()
        self.host.store.db.close()
        self.temp.cleanup()

    async def test_duplicate_web_request_dispatches_once_and_conflict_rejected(self):
        c = self.host.conversation
        ident = str(uuid.uuid4())
        results = await asyncio.gather(c.submit(ident, "只做一次", []), c.submit(ident, "只做一次", []))
        await asyncio.sleep(0.05)
        self.host.backend.submit.assert_awaited_once_with("只做一次", images=[])
        self.assertEqual(results[0]["message"]["id"], results[1]["message"]["id"])
        with self.assertRaises(ValueError):
            await c.submit(ident, "不同任务", [])

    async def test_uncertain_dispatch_is_not_automatically_replayed(self):
        self.host.backend.submit.side_effect = TimeoutError()
        c = self.host.conversation
        ident = str(uuid.uuid4())
        await c.submit(ident, "创建文件", [])
        await asyncio.sleep(0.05)
        again = await c.submit(ident, "创建文件", [])
        self.assertEqual(again["state"], "unknown")
        self.host.backend.submit.assert_awaited_once()

    async def test_voice_deltas_and_final_correction_share_one_item(self):
        c = self.host.conversation
        c.media_event(
            {"kind": "message", "message_id": "a", "role": "user", "text": "请记", "status": "streaming"}, 2
        )
        c.media_event({"kind": "message_delta", "message_id": "a", "delta": "住", "status": "streaming"}, 2)
        c.media_event(
            {"kind": "message", "message_id": "a", "role": "user", "text": "请记录", "status": "complete"}, 2
        )
        await asyncio.sleep(0.05)
        rows = c.store.history(c.session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "请记录")
        messages = self.host.backend.import_voice_messages.await_args.args[0]
        self.assertEqual(messages[0]["text"], "请记录")
        self.assertEqual(messages[0]["role"], "user")

    async def test_provider_prose_is_a_message_for_both_voice_and_web_tasks(self):
        c = self.host.conversation
        c.web_turns.add("web")
        for turn in ("voice", "web"):
            c.provider_event({"kind": "message_delta", "message_id": turn, "turn_id": turn, "delta": "检查"})
            self.assertEqual(c.store.get(f"task:session:{turn}")["kind"], "message")
            c.provider_event(
                task_event(
                    "item/completed",
                    {"turnId": turn, "item": {"type": "agentMessage", "id": turn, "text": "检查完成。"}},
                )
            )
        rows = c.store.history(c.session)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(m["kind"] == "message" and m["text"] == "检查完成。" for m in rows))

    async def test_restore_repairs_display_without_reordering_or_task_dispatch(self):
        c = self.host.conversation
        events = [
            task_event(
                "item/completed",
                {"turnId": "voice", "item": {"type": "agentMessage", "id": "prose", "text": "我检查一下。"}},
            ),
            task_event(
                "item/completed",
                {
                    "turnId": "voice",
                    "item": {
                        "type": "mcpToolCall",
                        "id": "tool",
                        "tool": "device",
                        "arguments": {"action": "room", "value": "living"},
                    },
                },
            ),
        ]
        for ident, text in (("prose", "我检查一下。"), ("tool", "调用工具\ndevice")):
            c.store.put(
                f"task:session:{ident}",
                c.session,
                kind="detail",
                text=text,
                turn_id="voice",
                status="complete",
                updated_at=123,
            )
        before = [(m["id"], m["ordinal"], m["updated"]) for m in c.store.history(c.session)]
        self.host.backend.restored_conversation_events = lambda: events
        await self.context.close()
        self.context = Owned()
        await c.start(self.context)
        rows = c.store.history(c.session)
        self.assertEqual(before, [(m["id"], m["ordinal"], m["updated"]) for m in rows])
        self.assertEqual(rows[0]["kind"], "message")
        self.assertEqual(rows[1]["kind"], "detail")
        self.assertEqual(rows[1]["text"], "调用工具\nselect_audio_output")
        self.host.backend.submit.assert_not_awaited()
        cursor = c.store.cursor()
        await self.context.close()
        self.context = Owned()
        await c.start(self.context)
        self.assertEqual(c.store.cursor(), cursor)

    async def test_old_mcp_metadata_is_only_used_for_display_not_new_capabilities(self):
        item = {"type": "mcpToolCall", "id": "old", "tool": "memory"}
        event = task_event(
            "item/completed",
            {"item": item | {"arguments": '{"action":"remember","arguments":{"id":"existing"}}'}},
        )
        self.assertEqual(event["text"], "调用工具\nupdate_memory")
        self.assertNotIn("arguments", event)
        for arguments in ("malformed", {"action": []}, []):
            event = task_event("item/completed", {"item": item | {"arguments": arguments}})
            self.assertEqual(event["text"], "调用工具\nmemory")

    async def test_web_result_is_quiet_context_voice_result_is_spoken(self):
        c = self.host.conversation
        self.host.voice.opened = True
        c.web_turns.add("web")
        c.provider_event({"kind": "turn_started", "turn_id": "web"})
        await c.task_done("web", "图片里是蓝色圆形", "completed")
        self.host.voice.append_context.assert_awaited_once()
        self.host.voice.instruct.assert_not_awaited()
        c.provider_event({"kind": "turn_started", "turn_id": "voice"})
        await c.task_done("voice", "真实结果", "completed")
        self.host.voice.say.assert_awaited_once_with("真实结果", execution_id="voice")
        self.host.voice.instruct.assert_not_awaited()

    async def test_web_question_is_visible_without_voice_and_result_sync_error_is_visible(self):
        c = self.host.conversation
        self.host.voice.opened = True
        c.web_turns.add("web")
        c.provider_event({"kind": "turn_started", "turn_id": "web"})
        await c.provider_notice("需要补充哪个文件名？", "web")
        self.host.voice.say.assert_not_awaited()
        self.assertEqual(c.store.history(c.session)[-1]["text"], "需要补充哪个文件名？")
        self.host.voice.append_context.side_effect = TimeoutError()
        await c.task_done("web", "已完成", "completed")
        self.assertIsNotNone(c.state()["sync_error"])

    async def test_stop_and_cancel_remain_distinct(self):
        await self.host.conversation.control("stop")
        await self.host.conversation.control("cancel")
        self.assertEqual(
            [c.args[0].kind for c in self.host.runtime.invoke.await_args_list], ["stop", "cancel"]
        )
