import asyncio
from collections import deque
from types import SimpleNamespace
import unittest
import time
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from assistant_runtime.plugins.codex.native import NativeVoice
from assistant_runtime.plugins.codex.browser_audio import BrowserAudio
from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.plugins.pipewire.stream_audio import StreamPlayer


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_long_wake_feedback_replaces_old_audio_and_fits_bounded_queue(self):
        p = StreamPlayer(None)
        p.feed(np.full(480 * 4, 1000, dtype="<i2").tobytes())
        old_epoch = p.epoch
        await p.cue()
        frames = list(p.queue._queue)
        self.assertTrue(all(epoch > old_epoch for epoch, _, _, _ in frames))
        self.assertLess(len(frames), p.queue.maxsize)
        self.assertGreater(len(frames) * 0.02, 1.0)
        self.assertFalse(p.suppressed)
        self.assertIsNone(p.last_error)
        await p.stop()
        self.assertTrue(p.queue.empty())
        self.assertFalse(p.playing)

    async def test_pause_and_quiet_consonants_keep_their_original_timing(self):
        p = StreamPlayer(None)
        loud = np.full(480, 1000, dtype="<i2").tobytes()
        quiet = np.full(480, 40, dtype="<i2").tobytes()
        pcm = loud + bytes(960 * 5) + quiet + loud
        # Packet boundaries need not coincide with output frame boundaries.
        for offset in range(0, len(pcm), 334):
            p.feed(pcm[offset : offset + 334])
        output = b"".join(p.queue.get_nowait()[1] for _ in range(p.queue.qsize()))
        self.assertEqual(output, pcm)

    async def test_silent_stream_after_voice_does_not_prevent_idle_timeout(self):
        route = SimpleNamespace(sink="test", ensure=AsyncMock())
        proc = SimpleNamespace(
            returncode=None,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            kill=Mock(),
            wait=AsyncMock(),
        )
        from assistant_runtime.plugins.pipewire.playback import PipeWirePlayback

        async def open_output(rate):
            return await PipeWirePlayback.open(route.sink, rate)

        route.open_output = open_output
        p = StreamPlayer(route)
        with patch(
            "assistant_runtime.plugins.pipewire.playback.asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ):
            run = asyncio.create_task(p.run())
            try:
                p.feed(np.full(480, 1000, dtype="<i2").tobytes())
                await asyncio.wait_for(p.queue.join(), 1)
                await asyncio.sleep(0.2)
                p.feed(bytes(960 * 10))
                await asyncio.wait_for(p.queue.join(), 1)
                self.assertFalse(p.playing)
                self.assertEqual(p.written_bytes, 960 * 11)
            finally:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    async def test_stop_discards_pending_pcm_and_resets_playback_activity(self):
        p = StreamPlayer(None)
        p.feed(np.full(480, 1000, dtype="<i2").tobytes())
        await p.stop()
        self.assertEqual(p.pending_voice_frames, 0)
        self.assertFalse(p.stream_started)
        self.assertFalse(p.playing)

    async def test_clear_queued_audio_and_drop_late_audio_until_new_boundary(self):
        p = StreamPlayer(None)
        loud = np.full(960, 1000, dtype="<i2").tobytes()
        p.feed(loud)
        self.assertFalse(p.queue.empty())
        p.input_speaking = True
        await p.stop()
        p.feed(loud)
        self.assertTrue(p.queue.empty())
        p.input_speaking = False
        p.feed(loud)
        self.assertTrue(p.queue.empty())
        p.feed(bytes(24000 // 4 * 2))
        p.feed(loud)
        self.assertFalse(p.queue.empty())

    async def test_network_silence_does_not_keep_playback_active(self):
        p = StreamPlayer(None)
        p.feed(bytes(24000 * 2))
        self.assertFalse(p.playing)
        self.assertTrue(p.queue.empty())

    async def test_explicit_stop_stays_muted_across_old_audio_silence(self):
        p = StreamPlayer(None)
        p.manual_muted = True
        p.feed(bytes(24000 * 2))
        p.feed(np.full(960, 1000, dtype="<i2").tobytes())
        self.assertTrue(p.queue.empty())

    async def test_stream_queue_is_bounded_under_stalled_device(self):
        p = StreamPlayer(None)
        p.feed(np.full(24000 * 5, 1000, dtype="<i2").tobytes())
        self.assertTrue(p.queue.empty())
        self.assertTrue(p.suppressed)
        self.assertIsNotNone(p.last_error)

    async def test_browser_input_resampling_preserves_speech_and_rate(self):
        bridge = BrowserAudio(Mock(), AsyncMock())
        wave = np.sin(np.arange(16000) * 2 * np.pi * 440 / 16000) * 8000
        bridge.feed(wave.astype("<i2").tobytes())
        output = np.frombuffer(bridge.queue.get_nowait(), dtype="<i2")
        self.assertGreater(len(output), 45000)
        self.assertLessEqual(len(output), 48000)
        self.assertGreater(np.max(np.abs(output)), 5000)
        await bridge.close()


class NativeSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_equivalent_keyword_results_activate_once_and_then_stream_audio(self):
        for phrase in ["嘟嘟", "你好嘟嘟"]:
            with self.subTest(phrase=phrase):
                s = AssistantService.__new__(AssistantService)
                s.active = False
                s.preroll = deque(maxlen=50)
                pcm = bytes(640)
                s.recognizer = SimpleNamespace(
                    detect=AsyncMock(return_value=phrase),
                    vad=SimpleNamespace(
                        accept_waveform=Mock(), is_speech_detected=lambda: False, empty=lambda: True
                    ),
                )
                s.player = SimpleNamespace(input_speaking=False)
                s.voice = SimpleNamespace(feed=Mock())

                async def activate(preroll):
                    s.active = True

                s.wake = AsyncMock(side_effect=activate)

                async def frames():
                    yield pcm
                    yield pcm

                s.route = SimpleNamespace(frames=frames, raw_frame=pcm)
                await s._capture()
                s.wake.assert_awaited_once_with((pcm,))
                s.recognizer.detect.assert_awaited_once()
                s.voice.feed.assert_called_once_with(pcm)

    async def test_delayed_native_turn_does_not_cut_new_audio(self):
        s = AssistantService.__new__(AssistantService)
        s.events = asyncio.Queue()
        s.active = True
        s.voice = SimpleNamespace(epoch=1)
        s.native_interruptions = 0
        s.last_speech = time.monotonic() - 5
        s.player = SimpleNamespace(manual_muted=False, stop=AsyncMock())
        run = asyncio.create_task(s._events())
        try:
            event = {
                "kind": "user_turn",
                "epoch": 1,
                "event": {"type": "turn.created", "turn": {"role": "user"}},
            }
            await s.events.put(event)
            await asyncio.wait_for(s.events.join(), 1)
            s.player.stop.assert_not_awaited()
            s.last_speech = time.monotonic()
            await s.events.put(event)
            await asyncio.wait_for(s.events.join(), 1)
            s.player.stop.assert_awaited_once()
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    async def test_final_media_transcript_once_and_stale_connection_ignored(self):
        events = AsyncMock()
        voice = NativeVoice(None, None, None, events)
        final = {"type": "turn.done", "turn": {"role": "user", "transcript": "结束对话"}}
        voice.media_event(final, voice.epoch - 1)
        events.assert_not_called()
        voice.media_event(final, voice.epoch)
        await asyncio.gather(*voice.tasks)
        transcripts = [c.args[0] for c in events.await_args_list if c.args[0]["kind"] == "transcript"]
        self.assertEqual(
            transcripts, [{"kind": "transcript", "epoch": 0, "role": "user", "text": "结束对话"}]
        )
        await voice.notification("thread/realtime/transcript/done", {"role": "user", "text": "结束对话"})
        self.assertEqual(events.await_count, 1)  # final text only, no provider event or sideband duplicates

    async def test_web_reply_silent_until_real_voice_turn_and_old_turn_stays_hidden(self):
        events, output = AsyncMock(), Mock()
        voice = NativeVoice(None, None, output, events)
        voice.on_conversation = Mock()
        voice.media_event(
            {"type": "turn.created", "turn": {"id": "old", "role": "assistant", "transcript": "旧回答"}},
            voice.epoch,
        )
        voice.on_conversation.reset_mock()
        voice.silence_web_reply()
        voice.on_conversation.assert_called_once_with(
            {"kind": "interrupted", "message_id": "old"}, voice.epoch
        )
        voice.on_conversation.reset_mock()
        voice.deliver_audio(b"pcm", voice.epoch)
        output.assert_not_called()
        voice.media_event(
            {"type": "turn.created", "turn": {"id": "silent", "role": "assistant", "transcript": "网页回复"}},
            voice.epoch,
        )
        voice.on_conversation.assert_not_called()
        voice.media_event(
            {"type": "turn.created", "turn": {"id": "user", "role": "user", "transcript": "继续"}},
            voice.epoch,
        )
        self.assertFalse(voice.web_silent)
        voice.media_event(
            {"type": "turn.done", "turn": {"id": "silent", "role": "assistant", "transcript": "网页回复"}},
            voice.epoch,
        )
        voice.deliver_audio(b"new", voice.epoch)
        output.assert_called_once_with(b"new")
        await asyncio.gather(*voice.tasks)
        self.assertFalse(any(c.args[0].get("text") == "网页回复" for c in events.await_args_list))

    async def test_wake_uses_raw_microphone_not_echo_filtered_keyword(self):
        s = AssistantService.__new__(AssistantService)
        s.active = False
        s.preroll = deque(maxlen=50)
        raw = np.full(320, 1500, dtype="<i2").tobytes()
        s.recognizer = SimpleNamespace(
            detect=AsyncMock(return_value="嘟嘟"),
            vad=SimpleNamespace(accept_waveform=Mock(), is_speech_detected=lambda: False, empty=lambda: True),
        )
        s.player = SimpleNamespace(input_speaking=False)
        s.voice = SimpleNamespace(feed=Mock())
        s.wake = AsyncMock()

        async def frames():
            yield bytes(640)

        s.route = SimpleNamespace(frames=frames, raw_frame=raw)
        await s._capture()
        np.testing.assert_allclose(s.recognizer.detect.call_args.args[0], 1500 / 32768)
        s.wake.assert_awaited_once()
        s.voice.feed.assert_not_called()

    async def test_local_vad_does_not_interrupt_playback(self):
        s = AssistantService.__new__(AssistantService)
        s.active = True
        s.recognizer = SimpleNamespace(
            vad=SimpleNamespace(accept_waveform=Mock(), is_speech_detected=lambda: True, empty=lambda: True)
        )
        s.player = SimpleNamespace(input_speaking=False, stop=AsyncMock())
        s.voice = SimpleNamespace(feed=Mock())

        async def frames():
            yield bytes(640)

        s.route = SimpleNamespace(frames=frames)
        await s._capture()
        s.player.stop.assert_not_awaited()
        s.voice.feed.assert_called_once()

    async def test_sleep_closes_voice_without_cancelling_task(self):
        s = AssistantService.__new__(AssistantService)
        s.active = True
        s.mode = "active"
        s.connect_task = None
        s.preroll = deque([bytes(1024)])
        s.recognizer = SimpleNamespace(reset_keyword=Mock())
        s.player = SimpleNamespace(stop=AsyncMock())
        s.voice = SimpleNamespace(close=AsyncMock())
        s.backend = SimpleNamespace(busy=True, cancel=AsyncMock())
        s.transition = asyncio.Lock()
        await s.sleep()
        s.backend.cancel.assert_not_called()
        s.voice.close.assert_awaited_once()
        self.assertFalse(s.active)
        self.assertFalse(s.preroll)

    async def test_standby_speech_never_sent_to_remote_voice(self):
        s = AssistantService.__new__(AssistantService)
        s.active = False
        s.preroll = deque(maxlen=32)
        s.recognizer = SimpleNamespace(
            detect=AsyncMock(return_value=None),
            vad=SimpleNamespace(accept_waveform=Mock(), is_speech_detected=lambda: True, empty=lambda: True),
        )
        s.player = SimpleNamespace(input_speaking=False)
        s.voice = SimpleNamespace(feed=Mock())

        async def frames():
            for _ in range(3):
                yield bytes(1024)

        s.route = SimpleNamespace(frames=frames)
        await s._capture()
        s.voice.feed.assert_not_called()


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_does_not_block_user_turn_events(self):
        s = AssistantService.__new__(AssistantService)
        s.events = asyncio.Queue()
        s.active = True
        s.mode = "active"
        s.recovering = False
        s.voice = SimpleNamespace(epoch=1)
        s.native_interruptions = 0
        s.last_speech = time.monotonic() - 5
        s.player = SimpleNamespace(manual_muted=True, stop=AsyncMock())
        pending = []

        def spawn(coro, label):
            task = asyncio.create_task(coro)
            pending.append(task)
            return task

        s.context = SimpleNamespace(spawn=spawn)
        gate = asyncio.Event()
        s._reconnect = gate.wait
        run = asyncio.create_task(s._events())
        try:
            await s.events.put({"kind": "disconnected", "epoch": 1})
            await s.events.put({"kind": "user_turn", "epoch": 1})
            await asyncio.wait_for(s.events.join(), 0.1)
            self.assertEqual(s.native_interruptions, 1)
            self.assertFalse(s.player.manual_muted)
            self.assertTrue(s.recovering)
        finally:
            run.cancel()
            for task in pending:
                task.cancel()
            await asyncio.gather(run, *pending, return_exceptions=True)
