"""Standalone capture evidence and cancellation; no physical audio in automatic tests."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

from scripts.audio_check.capture import Capture, duration
from scripts.audio_check.metrics import SignalStats, noise_comparison


def tone():
    return (np.sin(2 * np.pi * 1000 * np.arange(320) / 16000) * 3276.7).astype("<i2").tobytes()


def config():
    return SimpleNamespace(
        input_device="mic",
        default_output="speaker",
        devices={
            "mic": {"adapter": "pipewire", "kind": "microphone", "connection": {"match": "fixed"}},
            "speaker": {"adapter": "pipewire", "kind": "speaker", "connection": {"sink": "fixed"}},
        },
    )


class MetricsTest(unittest.TestCase):
    def test_level_and_full_scale_do_not_overflow(self):
        stats = SignalStats()
        stats.add(tone())
        self.assertAlmostEqual(stats.report()["rms_dbfs"], -23.01, places=1)
        self.assertEqual(stats.report()["clipped_sample_percent"], 0)
        stats = SignalStats()
        stats.add(np.full(320, -32768, dtype="<i2").tobytes())
        value = stats.report()
        self.assertEqual(value["rms_dbfs"], 0)
        self.assertEqual(value["clipped_sample_percent"], 100)
        self.assertEqual(value["dc_offset_percent_fs"], -100)
        json.dumps(value, allow_nan=False)

    def test_zero_runs_across_blocks_preserve_subframe_precision(self):
        stats = SignalStats()
        for values in ([1] * 160 + [0] * 160, [0] * 320, [0] * 80 + [1] * 240):
            stats.add(np.array(values, dtype="<i2").tobytes())
        self.assertEqual(stats.report()["longest_zero_run_ms"], 35)
        self.assertEqual(stats.report()["samples"], 960)
        for invalid in (b"", b"x", "wrong"):
            with self.assertRaises(ValueError):
                stats.add(invalid)

    def test_duration_rejects_nonfinite_and_unbounded_inputs(self):
        for value in (True, "5", None, 0, 31, 10**1000, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                duration(value)

    def test_noise_comparison_requires_matching_position_and_clean_capture(self):
        def background(environment, level, findings=None, position="study"):
            return {
                "environment": environment,
                "position": position,
                "kind": "background",
                "measurement": {"signal": {"rms_dbfs": level}},
                "findings": findings or [],
            }

        quiet, noisy = background("quiet", -60), background("noise", -30)
        self.assertEqual(noise_comparison([quiet]), [])
        self.assertEqual(noise_comparison([quiet, noisy])[0]["status"], "observed")
        noisy["findings"] = ["interrupted"]
        self.assertEqual(noise_comparison([quiet, noisy])[0]["status"], "unconfirmed")
        noisy["position"] = "kitchen"
        self.assertEqual(noise_comparison([quiet, noisy]), [])


class CaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_clears_window_and_releases_only_owned_stream(self):
        entered = asyncio.Event()

        async def read(size):
            entered.set()
            await asyncio.Event().wait()

        transport = SimpleNamespace(healthy=True, read=read, close=AsyncMock())
        with (
            patch("scripts.audio_check.capture.require_stopped", new=AsyncMock()),
            patch(
                "scripts.audio_check.capture.PipeWireEndpoint",
                return_value=SimpleNamespace(open=AsyncMock(return_value=transport)),
            ),
        ):
            capture = Capture(config())
            await capture.start()
            task = asyncio.create_task(capture.record(1))
            await entered.wait()
            await asyncio.sleep(0)
            with self.assertRaisesRegex(RuntimeError, "已有录音窗口"):
                await capture.record(1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNone(capture.current)
            await capture.close()
            transport.close.assert_awaited_once()
            self.assertFalse(capture.inputs.streams)
            self.assertFalse(capture.inputs.resources.snapshot("mic"))

    async def test_capture_failure_propagates_without_waiting_for_full_window(self):
        transport = SimpleNamespace(
            healthy=True, read=AsyncMock(side_effect=EOFError("device disconnected")), close=AsyncMock()
        )
        with (
            patch("scripts.audio_check.capture.require_stopped", new=AsyncMock()),
            patch(
                "scripts.audio_check.capture.PipeWireEndpoint",
                return_value=SimpleNamespace(open=AsyncMock(return_value=transport)),
            ),
        ):
            capture = Capture(config())
            await capture.start()
            try:
                with self.assertRaisesRegex(EOFError, "disconnected"):
                    await asyncio.wait_for(capture.record(30), 1)
            finally:
                await capture.close()
            self.assertIsNone(capture.current)
            transport.close.assert_awaited_once()
