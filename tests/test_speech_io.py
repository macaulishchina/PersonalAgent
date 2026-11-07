"""Speech integrity, cleanup and durable non-token cost reservations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from assistant_runtime.api.speech import SpeechError, spoken_excerpt
from assistant_runtime.api.ai_usage import UsageScope, UsageError
from assistant_runtime.core.speech_io.service import InputStream
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.ai_usage.settings import UsageSettings
from assistant_runtime.plugins.ai_passport.repository import Devices


class SpeechIntegrityTest(unittest.IsolatedAsyncioTestCase):
    async def test_final_only_and_gap_never_finishes(self):
        upstream = AsyncMock()
        upstream.finish.return_value = "最终转写"
        released = set()
        stream = InputStream(upstream, 1, released.add)
        await stream.accept(0, b"\x00\x00" * 160)
        self.assertEqual(await stream.finish(), "最终转写")
        self.assertIn(stream, released)
        upstream.close.assert_awaited_once()
        upstream.reset_mock()
        stream = InputStream(upstream, 1, released.add)
        with self.assertRaises(SpeechError):
            await stream.accept(1, b"\x00\x00")
        upstream.send.assert_not_awaited()
        upstream.finish.assert_not_awaited()
        self.assertTrue(stream.closed)

    async def test_budget_is_shared_persistent_and_unknown_retains_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = UsageSettings.parse(
                {
                    "work_limits": {"max_cost_cny": 10},
                    "prices": {
                        "audio": {"provider": "speech", "audio_cny_per_second": 0.1},
                        "text": {"provider": "tts", "text_cny_per_character": 0.1},
                    },
                }
            )
            usage = UsageService(Path(directory), settings)
            await usage.start()
            scope = UsageScope("same-work", "terminal:test")
            try:
                ticket = await usage.admit(
                    scope, service="asr", provider="speech", model="asr", bounds={"audio_seconds": 60}
                )
                self.assertEqual(ticket["reserved_quantities"]["audio_seconds"], 60)
                await usage.settle(ticket["id"], state="unknown")
                with self.assertRaises(UsageError):
                    await usage.admit(
                        scope, service="tts", provider="tts", model="tts", bounds={"text_characters": 50}
                    )
            finally:
                await usage.close()
            usage = UsageService(Path(directory), settings)
            await usage.start()
            try:
                with self.assertRaises(UsageError):
                    await usage.admit(
                        scope, service="tts", provider="tts", model="tts", bounds={"text_characters": 50}
                    )
            finally:
                await usage.close()


class TerminalIdentityTest(unittest.TestCase):
    def test_pair_identity_revocation_and_no_plaintext(self):
        db = sqlite3.connect(":memory:")
        devices = Devices(db)
        bundle = devices.issue("PP", "session-owned")
        with self.assertRaises(PermissionError):
            devices.authenticate(bundle["device_id"], bundle["credential"])
        devices.pair(bundle["device_id"], bundle["pairing_token"], bundle["credential"])
        self.assertEqual(devices.authenticate(bundle["device_id"], bundle["credential"]), "session-owned")
        self.assertNotIn(bundle["credential"], repr(db.execute("SELECT * FROM passport_devices").fetchall()))
        self.assertTrue(devices.claim_playback(bundle["device_id"], "message", 1))
        self.assertFalse(devices.claim_playback(bundle["device_id"], "message", 1))
        devices.revoke(bundle["device_id"])
        with self.assertRaises(PermissionError):
            devices.authenticate(bundle["device_id"], bundle["credential"])
        db.close()

    def test_spoken_excerpt_does_not_cut_unfinished_sentence(self):
        self.assertEqual(spoken_excerpt("短回复"), ("短回复", 3))
        self.assertEqual(spoken_excerpt("长" * 201), ("", 0))
        self.assertEqual(spoken_excerpt("第一句。" + "长" * 201), ("第一句。", 4))
