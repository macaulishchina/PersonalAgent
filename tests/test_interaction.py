"""Input loss, request fencing, uncertain delivery and scoped execution boundaries."""

import asyncio
from collections import deque
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from assistant_runtime.core.speech.interaction.input import InputBuffer
from assistant_runtime.core.speech.interaction.output import OutputScheduler
from assistant_runtime.core.speech.interaction.state import InteractionState
from assistant_runtime.core.speech.interaction.settings import InteractionSettings
from assistant_runtime.core.speech.interaction.service import Interaction
from assistant_runtime.core.speech.lifecycle import VoiceLifecycle
from assistant_runtime.api.speech import FeedbackIntent, RequestRef
from assistant_runtime.api.speech import InputChunk


class InputTest(unittest.TestCase):
    def test_overload_is_explicit_and_never_replays_an_incomplete_prefix(self):
        source = InputBuffer(2)
        for n in range(105):
            source.offer(bytes(640), n * 0.02)
        self.assertEqual(source.gaps, 1)
        self.assertTrue(source.queue.get_nowait()[2])
        with self.assertRaises(BufferError):
            source.since(1)
        self.assertLessEqual(source.size, 64000)

    def test_capture_discontinuity_and_privacy_clear(self):
        source = InputBuffer()
        source.offer(bytes(640), 1)
        source.offer(bytes(640), 2)
        self.assertEqual(source.gaps, 1)
        source.clear()
        self.assertEqual(len(source.frames), 0)
        self.assertTrue(source.queue.empty())

    def test_versions_reject_old_results_and_context_expires(self):
        state = InteractionState(120)
        first = state.begin("播放第一季")
        second = state.begin("不，第二季", correction=True)
        self.assertEqual(first.ref.id, second.ref.id)
        self.assertFalse(state.mark(first.ref, "completed"))
        second.updated -= 121
        self.assertEqual(state.context(), {})
        second.task_id, second.state = "task", "executing"
        self.assertEqual(state.context()["task_id"], "task")

    def test_opt_in_and_bounded_settings(self):
        self.assertFalse(InteractionSettings.parse({}).enabled)
        for config in ({"enabled": True}, {"routing_seconds": 4}, {"input_buffer_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                InteractionSettings.parse(config)


def player():
    value = SimpleNamespace(
        epoch=0,
        discarded_bytes=0,
        last_error=None,
        manual_muted=False,
        frame_bytes=960,
        queue=asyncio.Queue(),
        feed=Mock(),
        playing=False,
    )

    async def stop(**kwargs):
        value.epoch += 1

    value.stop = AsyncMock(side_effect=stop)
    return value


class OutputTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.player = player()
        self.renderer = SimpleNamespace(render=AsyncMock(return_value=bytes(960)), close=AsyncMock())
        self.output = OutputScheduler(self.player, self.renderer, self.db, lambda: True)
        self.intent = FeedbackIntent(RequestRef("request", 1), "failed", "失败", time.monotonic() + 30)

    async def asyncTearDown(self):
        await self.output.close()
        self.db.close()

    async def test_failed_delivery_is_unknown_and_never_replayed_even_after_restart(self):
        self.renderer.render.side_effect = TimeoutError()
        self.assertEqual(await self.output.say(self.intent), "unknown")
        recovered = OutputScheduler(self.player, self.renderer, self.db, lambda: True)
        self.assertEqual(await recovered.say(self.intent), "duplicate")
        self.assertEqual(self.renderer.render.await_count, 1)

    async def test_interrupt_during_synthesis_fences_late_pcm(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def render(text):
            entered.set()
            await release.wait()
            return bytes(960)

        self.renderer.render.side_effect = render
        task = asyncio.create_task(self.output.say(self.intent))
        await entered.wait()
        await self.output.interrupt()
        release.set()
        self.assertEqual(await task, "interrupted")
        self.player.feed.assert_not_called()

    async def test_speech_is_serial_and_expired_or_superseded_items_stay_silent(self):
        self.assertEqual(await self.output.say(self.intent, lambda: False), "deferred")
        self.renderer.render.assert_not_awaited()
        self.assertEqual(await self.output.say(self.intent), "completed")
        self.assertEqual(await self.output.say(self.intent), "duplicate")

    async def test_quiet_never_claims_delivery(self):
        self.output.allowed = lambda: False
        self.assertEqual(await self.output.say(self.intent), "deferred")
        self.assertEqual(self.db.execute("SELECT count(*) FROM speech_receipts").fetchone()[0], 0)


class ExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = set()

        def spawn(coro, name):
            task = asyncio.create_task(coro, name=name)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return task

        self.host = SimpleNamespace(
            context=SimpleNamespace(spawn=spawn),
            runtime=SimpleNamespace(invoke=AsyncMock()),
            config=SimpleNamespace(wake_words=["嘟嘟"]),
            player=player(),
            active=True,
            quiet_active=lambda: False,
            control=AsyncMock(),
            dialogue=SimpleNamespace(replied=Mock()),
        )
        self.output = SimpleNamespace(
            say=AsyncMock(return_value="completed"), interrupt=AsyncMock(), close=AsyncMock()
        )
        self.subject = Interaction(self.host, InteractionSettings(), AsyncMock(), self.output)
        self.subject.state.attending = True

    async def asyncTearDown(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_unknown_submission_is_not_retried(self):
        self.host.runtime.invoke.side_effect = TimeoutError()
        request = self.subject.state.begin("播放电影")
        with patch(
            "assistant_runtime.core.speech.interaction.service.decide",
            AsyncMock(return_value={"status": "fallback"}),
        ):
            await self.subject.route(request, {}, 1)
        self.assertEqual(self.host.runtime.invoke.await_count, 1)
        self.assertEqual(request.state, "unknown")
        self.assertEqual(self.output.say.await_args.args[0].kind, "unknown")

    async def test_superseded_routing_never_launches_old_work(self):
        old = self.subject.state.begin("播放第一季")
        self.subject.state.begin("播放第二季")
        with patch(
            "assistant_runtime.core.speech.interaction.service.decide",
            AsyncMock(return_value={"status": "fallback"}),
        ):
            await self.subject.route(old, {}, 1)
        self.host.runtime.invoke.assert_not_awaited()

    async def test_unconfirmed_handover_does_not_start_replacement(self):
        old = self.subject.state.begin("播放第一季")
        old.task_id = "old-task"
        self.subject.operation = old
        request = self.subject.state.begin("不，第二季")
        self.host.runtime.invoke.side_effect = TimeoutError()
        with patch(
            "assistant_runtime.core.speech.interaction.service.decide",
            AsyncMock(return_value={"status": "ok", "choice": "correction"}),
        ):
            await self.subject.route(request, {}, 1)
        calls = [c.args[0].action for c in self.host.runtime.invoke.await_args_list]
        self.assertEqual(calls, ["get"])
        self.assertEqual(request.state, "unknown")

    async def test_ending_attention_preserves_task_but_invalidates_speech(self):
        request = self.subject.state.begin("下载电影")
        request.task_id = "download"
        await self.subject.end_attention()
        self.host.runtime.invoke.assert_not_awaited()
        self.assertEqual(await self.subject.feedback(request, "completed", "下载完成"), "deferred")
        self.output.say.assert_not_awaited()

    async def test_uncertain_route_clarifies_without_device_execution(self):
        request = self.subject.state.begin("那个")
        with patch(
            "assistant_runtime.core.speech.interaction.service.decide",
            AsyncMock(return_value={"status": "ok", "choice": "uncertain"}),
        ):
            await self.subject.route(request, {}, 1)
        self.host.runtime.invoke.assert_not_awaited()
        self.assertEqual(self.output.say.await_args.args[0].kind, "clarification")

    async def test_fast_task_result_consumes_delayed_acknowledgement(self):
        async def invoke(request, **kwargs):
            if request.action == "create":
                return {"id": "task"}
            return {"runs": [{"id": "run", "state": "completed", "data": {"result": "已核对电视状态。"}}]}

        self.host.runtime.invoke.side_effect = invoke
        request = self.subject.state.begin("查询电视状态")
        with patch(
            "assistant_runtime.core.speech.interaction.service.decide",
            AsyncMock(return_value={"status": "fallback"}),
        ):
            await self.subject.route(request, {}, 1)
        for _ in range(5):
            await asyncio.sleep(0)
        self.output.say.assert_awaited_once()
        self.assertEqual(self.output.say.await_args.args[0].kind, "completed")
        self.assertEqual(request.task_id, "task")

    async def test_gap_blocks_remaining_utterance_until_new_wake(self):
        await self.subject.gap()
        self.subject.recognizer.accept.reset_mock()
        await self.subject.accept(InputChunk(1, 0, 0.02, bytes(640)))
        self.subject.recognizer.accept.assert_not_awaited()
        self.host.runtime.invoke.assert_not_awaited()
        await self.subject.begin(())
        self.assertFalse(self.subject.input_blocked)


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_capture_continues_without_waiting_for_cue_or_inference(self):
        host = VoiceLifecycle.__new__(VoiceLifecycle)
        host.input_buffer = InputBuffer()
        host.quiet_active = lambda: False

        async def frames():
            for _ in range(10):
                yield bytes(640)

        host.route = SimpleNamespace(frames=frames)
        await host._capture()
        self.assertEqual(host.input_buffer.queue.qsize(), 10)
        self.assertEqual(len(host.input_buffer.frames), 10)

    async def test_building_connection_buffers_input_instead_of_feeding_unready_provider(self):
        host = VoiceLifecycle.__new__(VoiceLifecycle)
        host.capture_sequence = 0
        host.quiet_active = lambda: False
        host.active, host.mode, host.interaction = True, "connecting", None
        host.native_pending = deque(maxlen=100)
        host.voice = SimpleNamespace(feed=Mock())
        host.recognizer = SimpleNamespace(
            vad=SimpleNamespace(accept_waveform=Mock(), is_speech_detected=lambda: False, empty=lambda: True)
        )
        host.player = player()
        source = InputBuffer()
        chunk = source.offer(bytes(640), 1)
        await host._process_frame(chunk, chunk.pcm, False)
        host.voice.feed.assert_not_called()
        self.assertEqual(list(host.native_pending), [chunk.pcm])
