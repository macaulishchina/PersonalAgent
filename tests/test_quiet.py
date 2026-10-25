import asyncio
from collections import deque
from assistant_runtime.core.speech.interaction.input import InputBuffer
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from assistant_runtime.core.speech.quiet import QuietHours
from assistant_runtime.core.speech.lifecycle import VoiceLifecycle
from assistant_runtime.app.config import Config


SETTINGS = {"enable": True, "start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai"}


class QuietHoursTest(unittest.TestCase):
    def test_overnight_boundaries_use_configured_timezone_not_host_timezone(self):
        policy = QuietHours.from_settings(SETTINGS)
        for timestamp, expected in (
            ("2026-10-13T14:59:59+00:00", False),
            ("2026-10-13T15:00:00+00:00", True),
            ("2026-10-13T16:00:00+00:00", True),
            ("2026-10-13T22:59:59+00:00", True),
            ("2026-10-13T23:00:00+00:00", False),
            ("2026-10-14T15:00:00+00:00", True),
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(policy.active(datetime.fromisoformat(timestamp)), expected)

    def test_daytime_schedule_and_disabled_default(self):
        self.assertIsNone(QuietHours.from_settings({}))
        self.assertEqual(Config().quiet_hours, {})
        policy = QuietHours.from_settings({**SETTINGS, "start": "12:00", "end": "13:00"})
        self.assertTrue(policy.active(datetime.fromisoformat("2026-10-13T12:00:00+08:00")))
        self.assertFalse(policy.active(datetime.fromisoformat("2026-10-13T13:00:00+08:00")))

    def test_invalid_schedule_rejected_before_device_startup(self):
        for settings in (
            None,
            {"start": "23:00"},
            {**SETTINGS, "start": "24:00"},
            {**SETTINGS, "end": "7:00"},
            {**SETTINGS, "end": "23:00"},
            {**SETTINGS, "timezone": "not-a-zone"},
            {**SETTINGS, "enable": "false"},
            {**SETTINGS, "enable": 0},
            {**SETTINGS, "enable": None},
            {key: value for key, value in SETTINGS.items() if key != "enable"},
            {**SETTINGS, "enable": False, "start": "24:00"},
            {**SETTINGS, "enabled": True},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                Config(plugins=["assistant"], quiet_hours=settings)

    def test_switch_preserves_schedule_and_can_be_reenabled(self):
        settings = {**SETTINGS, "enable": False}
        config = Config(quiet_hours=settings)
        policy = QuietHours.from_settings(config.quiet_hours)
        night = datetime.fromisoformat("2026-10-13T23:30:00+08:00")
        self.assertFalse(policy.active(night))
        self.assertEqual(config.quiet_hours, settings)
        self.assertEqual(policy.start.isoformat(), "23:00:00")
        self.assertEqual(policy.end.isoformat(), "07:00:00")
        self.assertEqual(policy.timezone.key, "Asia/Shanghai")
        self.assertTrue(QuietHours.from_settings({**settings, "enable": True}).active(night))


class QuietSessionTest(unittest.IsolatedAsyncioTestCase):
    def service(self, active=False):
        service = VoiceLifecycle.__new__(VoiceLifecycle)
        service.input_buffer = InputBuffer()
        service.native_pending = deque(maxlen=1500)
        service.active = active
        service.quiet_hours = SimpleNamespace(active=Mock(return_value=True))
        service.player = SimpleNamespace(feed=Mock(), cue=AsyncMock())
        service.voice = SimpleNamespace(say=AsyncMock(), epoch=1, feed=Mock())
        return service

    async def test_silence_blocks_wake_error_cues_speech_and_native_pcm(self):
        service = self.service()
        await service.wake()
        self.assertFalse(service.active)
        service.active = True
        await service.speak("不应播放")
        await service._cue(error=True)
        service.audio_received(bytes(960))
        service.player.cue.assert_not_awaited()
        service.player.feed.assert_not_called()
        service.voice.say.assert_not_awaited()

    async def test_microphone_frames_are_not_sent_or_used_for_wake_during_quiet_hours(self):
        service = self.service(active=True)
        service.preroll = deque([bytes(640)])
        service.recognizer = SimpleNamespace(detect=AsyncMock())

        async def frames():
            yield bytes(640)

        service.route = SimpleNamespace(frames=frames)
        await service._capture()
        service.recognizer.detect.assert_not_awaited()
        service.voice.feed.assert_not_called()
        self.assertFalse(service.preroll)

    async def test_nightly_transition_ends_voice_without_cancelling_background_task(self):
        service = self.service(active=True)
        service.mode = "active"
        service.connect_task = None
        service.preroll = deque()
        service.recognizer = SimpleNamespace(reset_keyword=Mock())
        service.player.stop = AsyncMock()
        service.voice.close = AsyncMock()
        service.backend = SimpleNamespace(busy=True, cancel=AsyncMock())
        service.transition = asyncio.Lock()
        service.standby_ready = asyncio.Event()
        await service._enforce_quiet()
        self.assertFalse(service.active)
        service.player.stop.assert_awaited_once()
        service.voice.close.assert_awaited_once()
        service.backend.cancel.assert_not_awaited()
        service.quiet_hours.active.return_value = False
        await service._enforce_quiet()
        self.assertFalse(service.active)
        service.player.cue.assert_not_awaited()

    async def test_stale_native_user_turn_cannot_unmute_during_quiet_hours(self):
        service = self.service(active=True)
        service.events = asyncio.Queue()
        service.player.manual_muted = True
        run = asyncio.create_task(service._events())
        try:
            await service.events.put({"epoch": 1, "kind": "user_turn"})
            await asyncio.wait_for(service.events.join(), 1)
            self.assertTrue(service.player.manual_muted)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
