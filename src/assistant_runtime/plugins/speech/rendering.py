"""Literal local Kokoro rendering; bounded work, no device access or cloud fallback."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import sherpa_onnx
import soxr


class LocalSpeech:
    def __init__(self, directory, voice=46):
        self.directory, self.voice = Path(directory).expanduser(), voice
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-rendering")
        self.engine = None
        self.pending = None
        self.closed = False
        self.lock = asyncio.Lock()

    def _render(self, text):
        if self.engine is None:
            root = self.directory
            cfg = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                        model=str(root / "model.int8.onnx"),
                        voices=str(root / "voices.bin"),
                        tokens=str(root / "tokens.txt"),
                        data_dir=str(root / "espeak-ng-data"),
                        lexicon=str(root / "lexicon-zh.txt") + "," + str(root / "lexicon-us-en.txt"),
                        lang="zh",
                    ),
                    num_threads=2,
                ),
                max_num_sentences=1,
            )
            if not cfg.validate():
                raise ValueError("Incomplete local speech model directory")
            self.engine = sherpa_onnx.OfflineTts(cfg)
        audio = self.engine.generate(text, sid=self.voice, speed=1.05)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if not 0 < len(samples) <= audio.sample_rate * 30 or not np.isfinite(samples).all():
            raise ValueError("Invalid or oversized synthesized audio")
        if audio.sample_rate != 24000:
            samples = soxr.resample(samples, audio.sample_rate, 24000)
        return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()

    async def render(self, text):
        async with self.lock:
            return await self._generate(text)

    async def _generate(self, text):
        if self.closed or not isinstance(text, str) or not 0 < len(text) <= 240:
            raise ValueError("Speech must be 1..240 characters")
        if self.pending and not self.pending.done():
            # A canceled acknowledgement may still be computing in the worker.
            # Wait within the caller's deadline, without queueing another native
            # computation or treating the previous result as this result's audio.
            try:
                await asyncio.shield(self.pending)
            except Exception:
                pass
        if self.closed:
            raise RuntimeError("Speech renderer closed")
        self.pending = asyncio.get_running_loop().run_in_executor(self.worker, self._render, text)
        return await asyncio.shield(self.pending)

    async def close(self):
        self.closed = True
        self.worker.shutdown(wait=False, cancel_futures=True)
