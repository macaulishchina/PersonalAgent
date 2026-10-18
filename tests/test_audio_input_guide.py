"""Standalone guide ownership, subprocess cleanup, scene coverage and private evidence."""

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

from scripts.audio_check.capture import require_stopped
from scripts.audio_check.guide import CUES, POSITIONS, RATE, GuidePlayer, load_prompt, prompt_folder
from scripts.audio_check.prepare import command, prepare_prompts
from scripts.audio_check.protocol import Protocol, write_wave
from scripts.check_audio_input import run
from test_audio_input_quality import config, tone


def prompts(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for cue in CUES:
        with wave.open(str(folder / (cue + ".wav")), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(RATE)
            file.writeframes(bytes(960))


class GuideTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_service_and_unmanaged_socket_prevent_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            value = SimpleNamespace(unit_name="agent.service", socket=Path(directory) / "control.sock")
            for output in (
                b"ActiveState=active\nMainPID=123\n",
                b"ActiveState=inactive\nMainPID=123\n",
                b"bad\xff",
            ):
                with patch("scripts.audio_check.capture.command", new=AsyncMock(return_value=output)):
                    with self.assertRaises(RuntimeError):
                        await require_stopped(value)
            with patch(
                "scripts.audio_check.capture.command",
                new=AsyncMock(return_value=b"ActiveState=inactive\nMainPID=0\n"),
            ):
                await require_stopped(value)

                async def connected(reader, writer):
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_unix_server(connected, value.socket)
                try:
                    with self.assertRaisesRegex(RuntimeError, "仍在监听"):
                        await require_stopped(value)
                finally:
                    server.close()
                    await server.wait_closed()

    async def test_cancel_reaps_real_owned_child_and_non_utf8_error_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "pid"
            task = asyncio.create_task(
                command(
                    [
                        sys.executable,
                        "-c",
                        "import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)",
                        str(marker),
                    ]
                )
            )
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
            pid = int(marker.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        with self.assertRaisesRegex(RuntimeError, "Voice preparation failed"):
            await command(
                [sys.executable, "-c", "import sys;sys.stderr.buffer.write(bytes([228,255]));sys.exit(1)"]
            )

    async def test_prompt_staging_publishes_valid_cache_without_cleanup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state, model = Path(directory) / "state", Path(directory) / "model"
            model.mkdir()
            (model / "model.int8.onnx").touch()

            async def build(arguments, **kwargs):
                prompts(Path(arguments[-1]))

            with patch("scripts.audio_check.prepare.command", side_effect=build) as worker:
                folder = await prepare_prompts(state, model)
                self.assertEqual(len(load_prompt(state, "start")), 960)
                self.assertEqual(await prepare_prompts(state, model), folder)
                self.assertEqual(worker.call_count, 1)
                self.assertEqual(list(folder.parent.glob("building-*")), [])
            (folder / "start.wav").write_bytes(b"bad")
            with self.assertRaises(ValueError):
                load_prompt(state, "start")

    async def test_output_waits_for_eof_drain_and_cancellation_closes_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            value = config()
            value.state_dir = Path(directory)
            prompts(prompt_folder(value.state_dir))
            order = []
            process = SimpleNamespace(
                returncode=0,
                stdin=SimpleNamespace(close=Mock(side_effect=lambda: order.append("eof"))),
                wait=AsyncMock(side_effect=lambda: order.append("drained")),
            )
            transport = SimpleNamespace(
                healthy=True,
                process=process,
                write=AsyncMock(),
                close=AsyncMock(side_effect=lambda: order.append("closed")),
            )
            with patch(
                "scripts.audio_check.guide.PipeWireEndpoint",
                return_value=SimpleNamespace(open=AsyncMock(return_value=transport)),
            ):
                player = GuidePlayer(value)
            await player.play("start", AsyncMock())
            self.assertEqual(order, ["eof", "drained", "closed"])
            self.assertFalse(player.outputs.resources.snapshot("speaker"))
            entered = asyncio.Event()

            async def blocked(pcm):
                entered.set()
                await asyncio.Event().wait()

            transport.write = blocked
            task = asyncio.create_task(player.play("start", AsyncMock()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(transport.close.await_count, 2)
            self.assertFalse(player.outputs.streams)
            await player.close()

    async def test_full_guide_covers_each_scene_position_and_both_utterance_types(self):
        capture = SimpleNamespace(
            ready=AsyncMock(),
            record=AsyncMock(return_value=(tone(), {"signal": {"rms_dbfs": -30}, "stream": {}}, [])),
        )
        report = {"wake_words": ["configured-name"], "phases": []}
        protocol = Protocol(capture, None, report, Path("unused"))
        protocol.wait = AsyncMock()
        with contextlib.redirect_stderr(io.StringIO()):
            await protocol.run()
        self.assertEqual(
            {(row["environment"], row["position"], row["kind"]) for row in report["phases"]},
            {
                (environment, position, kind)
                for environment in ("quiet", "noise")
                for position in POSITIONS
                for kind in ("background", "reading", "wake_reading")
            },
        )
        self.assertEqual(len(report["phases"]), 24)
        for row in report["phases"]:
            if row["kind"] == "wake_reading":
                self.assertTrue(row["reference"].startswith("configured-name，"))
            self.assertNotIn("file", row)

    async def test_interruption_saves_partial_private_evidence_without_restarting_service(self):
        with tempfile.TemporaryDirectory() as directory:
            value = config()
            value.state_dir = Path(directory) / "state"
            value.wake_words = ["configured-name"]
            capture = SimpleNamespace(start=AsyncMock(), ready=AsyncMock(), close=AsyncMock())
            capture.record = AsyncMock(side_effect=asyncio.CancelledError())
            args = SimpleNamespace(
                config=Path("unused"),
                output=None,
                guided=False,
                text_only=False,
                save_audio=False,
                prepare_guide=False,
                seconds=1,
                json=False,
            )
            with (
                patch("scripts.check_audio_input.Config.load", return_value=value),
                patch("scripts.check_audio_input.require_stopped", new=AsyncMock()),
                patch("scripts.check_audio_input.Capture", return_value=capture),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await run(args)
            capture.close.assert_awaited_once()
            paths = list(value.state_dir.glob("acceptance/audio-input/*/result.json"))
            self.assertEqual(len(paths), 1)
            report = json.loads(paths[0].read_text())
            self.assertFalse(report["completed"])
            self.assertFalse(report["auto_restore"])
            self.assertFalse(report["audio_retained"])
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(value.state_dir.rglob("*.wav")), [])

    def test_explicit_wav_save_is_private_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "human.wav"
            write_wave(path, tone())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with wave.open(str(path)) as audio:
                self.assertEqual(audio.getnframes(), 320)
                self.assertEqual(audio.getframerate(), 16000)
            with self.assertRaises(FileExistsError):
                write_wave(path, b"different")
