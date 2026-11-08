"""Speech integrity, cleanup and durable non-token cost reservations."""

import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
from assistant_runtime.api.speech import SpeechError, spoken_excerpt
from assistant_runtime.api.ai_usage import UsageScope, UsageError
from assistant_runtime.core.speech_io.service import InputStream, BufferedRecognition
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.ai_usage.settings import UsageSettings
from assistant_runtime.plugins.ai_passport.repository import Devices


class SpeechIntegrityTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_sixty_seconds_finishes_and_next_frame_is_rejected_precisely(self):
        class Counter:
            def __init__(self):
                self.bytes = 0
                self.finished = self.closed = False

            async def send(self, pcm):
                self.bytes += len(pcm)

            async def finish(self):
                self.finished = True
                return "完整的六十秒转写"

            async def close(self):
                self.closed = True

        for attempt in range(3):
            upstream = Counter()
            stream = InputStream(upstream, 60, lambda _: None)
            for sequence in range(3000):
                await stream.accept(sequence, b"\0\0" * 320)
            self.assertEqual(stream.size, stream.max_bytes)
            if attempt == 1:
                with self.assertRaisesRegex(SpeechError, "^speech_input_limit$"):
                    await stream.accept(3000, b"\0\0" * 320)
                self.assertFalse(upstream.finished)
            else:
                self.assertEqual(await stream.finish(), "完整的六十秒转写")
                self.assertTrue(upstream.finished)
            self.assertEqual(upstream.bytes, 1920000)
            self.assertTrue(upstream.closed)

    async def test_gap_format_and_deadline_have_distinct_reasons(self):
        for reason, sequence, pcm in [
            ("speech_input_gap", 1, b"\0\0"),
            ("speech_invalid_pcm", 0, b"\0"),
            ("speech_input_deadline", 0, b"\0\0"),
        ]:
            upstream = AsyncMock()
            stream = InputStream(upstream, 1, lambda _: None)
            if reason == "speech_input_deadline":
                stream.started -= 12
            with self.assertRaisesRegex(SpeechError, "^" + reason + "$"):
                await stream.accept(sequence, pcm)
            upstream.send.assert_not_awaited()
            upstream.finish.assert_not_awaited()

    async def test_partial_updates_are_optional_for_final_only_providers(self):
        class FinalOnly:
            def __init__(self):
                self.audio = bytearray()
                self.closed = False

            async def send(self, pcm):
                self.audio.extend(pcm)

            async def finish(self):
                return "只有最终转写，也能完整提交。"

            async def close(self):
                self.closed = True

        upstream = FinalOnly()

        async def invoke(*args, **kwargs):
            return upstream

        context = SimpleNamespace(invoke=invoke, spawn=lambda coro, _: asyncio.create_task(coro))
        pending = BufferedRecognition(context, None)
        stream = InputStream(pending, 1, lambda _: None)
        await stream.accept(0, b"\x01\x00" * 320)
        with self.assertRaises(StopAsyncIteration):
            await asyncio.wait_for(stream.wait_update(""), 1)
        self.assertFalse(upstream.closed)
        self.assertEqual(await stream.finish(), "只有最终转写，也能完整提交。")
        self.assertEqual(upstream.audio, b"\x01\x00" * 320)
        self.assertTrue(upstream.closed)

    async def test_provider_opening_preserves_frames_and_release_before_ready(self):
        opened = asyncio.Event()
        upstream = AsyncMock()
        upstream.finish.return_value = "首尾完整"

        async def invoke(*args, **kwargs):
            await opened.wait()
            return upstream

        context = SimpleNamespace(invoke=invoke, spawn=lambda coro, _: asyncio.create_task(coro))
        pending = BufferedRecognition(context, None)
        stream = InputStream(pending, 1, lambda _: None)
        await stream.accept(0, b"\x01\x00" * 320)
        await stream.accept(1, b"\x02\x00" * 320)
        task = asyncio.create_task(stream.finish())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        opened.set()
        self.assertEqual(await task, "首尾完整")
        self.assertEqual(
            [c.args[0] for c in upstream.send.await_args_list], [b"\x01\x00" * 320, b"\x02\x00" * 320]
        )
        upstream.close.assert_awaited_once()
        self.assertEqual(pending.pending_bytes, 0)

    async def test_slow_provider_buffer_overflow_cancels_without_finishing(self):
        async def invoke(*args, **kwargs):
            await asyncio.Event().wait()

        context = SimpleNamespace(invoke=invoke, spawn=lambda coro, _: asyncio.create_task(coro))
        pending = BufferedRecognition(context, None)
        stream = InputStream(pending, 10, lambda _: None)
        for index in range(8):
            await stream.accept(index, b"\x00\x00" * 4096)
        with self.assertRaisesRegex(SpeechError, "speech_input_buffer_limit"):
            await stream.accept(8, b"\x00\x00")
        self.assertTrue(stream.closed)
        self.assertTrue(pending.task.cancelled())
        self.assertEqual(pending.pending_bytes, 0)

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
    def test_pairing_survives_reopened_repository_without_a_new_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.sqlite3"
            ident, credential = "d" * 32, "synthetic-credential" * 3
            with sqlite3.connect(path) as db:
                devices = Devices(db)
                code = devices.begin(ident, credential)["code"]
                devices.activate(code, "PP", "same-session")
            db.close()
            with sqlite3.connect(path) as db:
                reopened = Devices(db)
                self.assertEqual(reopened.authenticate(ident, credential), "same-session")
                self.assertTrue(reopened.begin(ident, credential)["paired"])
                with self.assertRaises(PermissionError):
                    reopened.authenticate(ident, "wrong")
                reopened.revoke(ident)
                with self.assertRaises(PermissionError):
                    reopened.authenticate(ident, credential)
            db.close()

    def test_pair_identity_revocation_and_no_plaintext(self):
        db = sqlite3.connect(":memory:")
        devices = Devices(db)
        ident, credential = "d" * 32, "secret-credential" * 3
        bundle = devices.begin(ident, credential)
        self.assertRegex(bundle["code"], r"^[0-9]{8}$")
        self.assertEqual(devices.begin(ident, credential), bundle)
        with self.assertRaises(PermissionError):
            devices.authenticate(ident, credential)
        self.assertEqual(devices.challenge(bundle["code"]), ident)
        devices.activate(bundle["code"], "PP", "session-owned")
        self.assertEqual(devices.authenticate(ident, credential), "session-owned")
        self.assertTrue(devices.begin(ident, credential)["paired"])
        with self.assertRaises(PermissionError):
            devices.challenge(bundle["code"])
        self.assertNotIn(credential, repr(db.execute("SELECT * FROM passport_devices").fetchall()))
        self.assertTrue(devices.claim_playback(ident, "message", 1))
        self.assertFalse(devices.claim_playback(ident, "message", 1))
        devices.revoke(ident)
        with self.assertRaises(PermissionError):
            devices.authenticate(ident, credential)
        with self.assertRaises(PermissionError):
            devices.begin(ident, credential)
        db.close()

    def test_pairing_expiry_and_different_credential_do_not_rebind(self):
        db = sqlite3.connect(":memory:")
        devices = Devices(db)
        challenge = devices.begin("d" * 32, "first" * 8)
        with self.assertRaises(PermissionError):
            devices.begin("d" * 32, "second" * 8)
        with db:
            db.execute("UPDATE passport_pairing SET expires=0")
        with self.assertRaises(PermissionError):
            devices.activate(challenge["code"], "PP", "other-session")
        self.assertEqual(devices.list(), [])
        db.close()

    def test_spoken_excerpt_does_not_cut_unfinished_sentence(self):
        self.assertEqual(spoken_excerpt("短回复"), ("短回复", 3))
        self.assertEqual(spoken_excerpt("长" * 201), ("", 0))
        self.assertEqual(spoken_excerpt("第一句。" + "长" * 201), ("第一句。", 4))
