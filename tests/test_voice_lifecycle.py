"""Foreground identity, once-only delivery and short attention under late events."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
from collections import deque
from types import SimpleNamespace

import test_conversation as fixtures
from assistant_runtime.assistant.commands import parse_command
from assistant_runtime.assistant.dialogue import Dialogue
from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.config import Config
from assistant_runtime.conversation.feedback import spoken_result
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store


class ForegroundTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db")
        self.backend = CodexBackend(Config(root=Path(self.temp.name)), self.store, AsyncMock(), AsyncMock())
        self.backend.thread_id = "main"
        self.backend.on_conversation_done = AsyncMock()
        self.reader = asyncio.create_task(self.backend._events())

    async def asyncTearDown(self):
        self.reader.cancel()
        await asyncio.gather(self.reader, *self.backend.server_tasks, return_exceptions=True)
        self.store.db.close()
        self.temp.cleanup()

    async def event(self, method, **params):
        await self.backend.client.events.put({"method": method, "params": {"threadId": "main", **params}})
        for _ in range(20):
            await asyncio.sleep(0)
            if self.backend.client.events.empty():
                await asyncio.sleep(0)
                break
        if self.reader.done():
            self.reader.result()

    async def start(self, ident):
        await self.event("turn/started", turn={"id": ident})

    async def message(self, turn, text, phase="final", ident=None):
        await self.event(
            "item/completed",
            turnId=turn,
            item={"type": "agentMessage", "id": ident or turn + "-message", "text": text, "phase": phase},
        )

    async def end(self, turn, status="completed"):
        await self.event("turn/completed", turn={"id": turn, "status": status})

    async def test_empty_new_turn_never_reuses_old_progress_or_verified_report(self):
        await self.start("one")
        await self.message("one", "旧任务成功")
        await self.end("one")
        await self.start("two")
        self.assertEqual(self.backend.progress, "")
        await self.end("two")
        self.assertEqual(self.backend.on_conversation_done.await_args.args, ("two", "", "completed"))
        self.assertEqual(
            self.store.db.execute("select count(*) from transcripts where role='verified'").fetchone()[0], 1
        )

    async def test_late_completion_does_not_finish_new_turn_or_deliver_twice(self):
        await self.start("one")
        await self.message("one", "第一项")
        await self.end("one")
        await self.start("two")
        await self.message("one", "迟到旧结果", ident="late")
        await self.end("one")
        self.assertEqual(self.backend.active_turn, "two")
        self.assertEqual(self.backend.progress, "")
        self.assertEqual(self.backend.on_conversation_done.await_count, 1)
        await self.message("two", "第二项")
        await self.end("two")
        self.assertEqual(self.backend.on_conversation_done.await_args.args[1], "第二项")

    async def test_commentary_is_not_a_result_and_result_is_not_progress_truncation(self):
        await self.start("one")
        await self.message("one", "我去查一下", "commentary")
        await self.end("one")
        self.assertEqual(self.backend.on_conversation_done.await_args.args[1], "")
        await self.start("two")
        text = "真实结果。" * 300
        await self.message("two", text)
        await self.end("two")
        self.assertEqual(self.backend.on_conversation_done.await_args.args[1], text)

    async def test_overlapping_provider_notifications_have_independent_task_receipts(self):
        await self.start("one")
        first = self.backend.active_task
        await self.start("two")
        second = self.backend.active_task
        self.assertNotEqual(first, second)
        await self.event(
            "turn/completed",
            turn={
                "id": "one",
                "status": "completed",
                "items": [
                    {
                        "id": "old-final",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "第一项实际结果",
                    }
                ],
            },
        )
        self.assertEqual(self.backend.active_task, second)
        self.assertEqual(self.backend.active_turn, "two")
        old = self.store.db.execute("select status,result from tasks where id=?", (first,)).fetchone()
        self.assertEqual(old, ("completed", "第一项实际结果"))
        await self.message("two", "第二项实际结果")
        await self.end("two")
        self.assertIsNone(self.backend.active_task)


class FeedbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = fixtures.fixture(Path(self.temp.name))
        self.context = fixtures.Owned()
        self.c = self.host.conversation
        await self.c.start(self.context)
        self.host.voice.opened = True

    async def asyncTearDown(self):
        await self.context.close()
        self.host.store.db.close()
        self.temp.cleanup()

    def bind(self, turn="one"):
        self.c.provider_event({"kind": "turn_started", "turn_id": turn})

    async def test_duplicate_completions_submit_speech_once(self):
        self.bind()
        await asyncio.gather(*(self.c.task_done("one", "真实结果", "completed") for _ in range(3)))
        self.host.voice.say.assert_awaited_once_with("真实结果", execution_id="one")
        self.assertEqual(self.c.feedback.get("one")["state"], "submitted")

    async def test_lost_ack_is_unknown_and_never_automatically_repeated(self):
        self.bind()
        self.host.voice.say.side_effect = TimeoutError()
        await self.c.task_done("one", "结果", "completed")
        await self.c.task_done("one", "结果", "completed")
        self.host.voice.say.assert_awaited_once()
        self.assertEqual(self.c.feedback.get("one")["state"], "unknown")

    async def test_closed_connection_late_result_cannot_speak_in_new_connection(self):
        self.bind()
        self.c.media_event({"kind": "closed"}, self.host.voice.epoch)
        self.host.voice.epoch += 1
        await self.c.task_done("one", "旧结果", "completed")
        self.host.voice.say.assert_not_awaited()
        self.assertEqual(self.c.feedback.get("one")["state"], "closed")

    async def test_new_user_focus_supersedes_old_result_and_notice(self):
        self.bind()
        self.c.media_event(
            {"kind": "message", "message_id": "new", "role": "user", "text": "新问题", "status": "streaming"},
            self.host.voice.epoch,
        )
        await self.c.task_done("one", "旧结果", "completed")
        await self.c.provider_notice("旧任务需要确认", "one")
        self.host.voice.say.assert_not_awaited()
        self.assertEqual(self.c.feedback.get("one")["state"], "superseded")

    async def test_new_execution_supersedes_old_even_without_another_media_event(self):
        self.bind()
        self.bind("two")
        self.bind()  # replay of the old turn/started must not steal focus back
        await self.c.task_done("one", "旧结果", "completed")
        await self.c.task_done("two", "新结果", "completed")
        self.host.voice.say.assert_awaited_once_with("新结果", execution_id="two")

    async def test_empty_interrupted_and_quiet_results_do_not_trigger_response(self):
        self.bind()
        await self.c.task_done("one", "", "completed")
        self.bind("two")
        await self.c.task_done("two", "中断前进度", "interrupted")
        self.bind("three")
        self.host.quiet_active = lambda: True
        await self.c.task_done("three", "结果", "completed")
        self.host.voice.say.assert_not_awaited()
        self.host.voice.instruct.assert_not_awaited()

    async def test_restart_does_not_retry_uncertain_or_unbound_output(self):
        self.bind()
        self.c.feedback.finish("one", "dispatching")
        self.c.feedback.recover(self.c.session)
        await self.c.task_done("one", "结果", "completed")
        await self.c.task_done("unbound", "无归属结果", "completed")
        self.assertEqual(self.c.feedback.get("one")["state"], "unknown")
        self.host.voice.say.assert_not_awaited()


class ExitRaceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = fixtures.Owned()
        s = self.service = AssistantService.__new__(AssistantService)
        s.mode = "active"
        s.active = True
        s.memory = None
        s.reconnect_task = None
        s.connect_task = None
        s.transition = asyncio.Lock()
        s.standby_ready = asyncio.Event()
        s.standby_ready.set()
        s.player = SimpleNamespace(stop=AsyncMock(), manual_muted=False)
        s.voice = SimpleNamespace(close=AsyncMock())
        s.recognizer = SimpleNamespace(reset_keyword=Mock())
        s.preroll = deque()
        s.backend = SimpleNamespace(busy=True, cancel=AsyncMock())
        s.dialogue = Dialogue()
        s.wake_count = 0
        s.context = self.context
        s._cue = AsyncMock()
        s._connect = AsyncMock()

    async def asyncTearDown(self):
        await self.context.close()

    async def test_new_wake_waits_until_exit_transport_cleanup_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()

        self.service.voice.close.side_effect = close
        sleep = asyncio.create_task(self.service.sleep())
        await entered.wait()
        self.assertEqual(self.service.mode, "closing")
        wake = asyncio.create_task(self.service.wake())
        await asyncio.sleep(0)
        self.assertFalse(wake.done())
        self.service._connect.assert_not_awaited()
        release.set()
        await sleep
        await wake
        await asyncio.sleep(0)
        self.assertEqual(self.service.mode, "connecting")
        self.service.voice.close.assert_awaited_once()
        self.service.backend.cancel.assert_not_awaited()

    async def test_repeated_exit_coalesces_and_cleanup_failure_releases_waiters(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()

        self.service.voice.close.side_effect = close
        first = asyncio.create_task(self.service.sleep())
        await entered.wait()
        second = asyncio.create_task(self.service.sleep())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.service.voice.close.assert_awaited_once()
        self.service.player.stop.side_effect = RuntimeError("output unavailable")
        with self.assertRaises(RuntimeError):
            await self.service.sleep()
        self.assertEqual(self.service.mode, "standby")
        self.assertTrue(self.service.standby_ready.is_set())


class AttentionTest(unittest.TestCase):
    def test_short_followup_and_explicit_continuous_chat(self):
        d = Dialogue(user_at=1, reply_at=3)
        args = {"busy": False, "playing": False, "last_output": 4, "timeout": 60}
        self.assertFalse(d.expired(**args, now=11.9))
        self.assertTrue(d.expired(**args, now=12))
        d.continuous = True
        self.assertFalse(d.expired(**args, now=12))
        self.assertTrue(d.expired(**args, now=64))

    def test_stuck_execution_does_not_hold_voice_open_forever(self):
        d = Dialogue(user_at=1)
        self.assertTrue(d.expired(busy=True, playing=False, last_output=59, timeout=60, now=61))

    def test_goodbye_commands_and_quoted_negative_commands(self):
        for text in ["退下", "退下吧你", "你先退下吧", "还是退下吧，啥都听不懂", "嗯好，你退下退下"]:
            self.assertEqual(parse_command(text).kind, "sleep", text)
        for text in ["不要退下", "说说退下是什么意思", "如果我说退下你会怎么做", "文件先这样吧，继续下一步"]:
            self.assertIsNone(parse_command(text), text)
        self.assertEqual(parse_command("陪我聊聊").kind, "chat")

    def test_settings_and_voice_result_bounds(self):
        for value in [True, 0, 31, "8"]:
            with self.assertRaises(ValueError):
                Config(voice_follow_up_seconds=value)
        self.assertLess(len(spoken_result("甲" * 5000, "completed")), 1100)
        self.assertNotIn("```", spoken_result("结果```code```", "completed"))
        self.assertIn("bwrap: failed", spoken_result("实际输出：\n```text\nbwrap: failed\n```", "completed"))
