"""Local streaming Paraformer adapter. Sequence/time ownership stays above the model."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import sherpa_onnx

from ...kernel.speech import Recognition


class LocalRecognition:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech-recognition")
        self.engine = None
        self.stream = None
        self.closed = False
        self.lock = asyncio.Lock()

    def _reset(self):
        if self.engine is None:
            paths = [
                self.directory / name for name in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt")
            ]
            if not all(path.is_file() for path in paths):
                raise ValueError("Incomplete streaming Paraformer model directory")
            self.engine = sherpa_onnx.OnlineRecognizer.from_paraformer(
                encoder=str(paths[0]),
                decoder=str(paths[1]),
                tokens=str(paths[2]),
                num_threads=2,
                sample_rate=16000,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=0.8,
                rule3_min_utterance_length=25,
            )
        self.stream = self.engine.create_stream()

    async def _call(self, function, *args):
        async with self.lock:
            if self.closed:
                raise RuntimeError("Recognition closed")
            future = asyncio.get_running_loop().run_in_executor(self.worker, function, *args)
            return await future

    async def reset(self):
        await self._call(self._reset)

    def _accept(self, chunk):
        self.stream.accept_waveform(16000, np.frombuffer(chunk.pcm, dtype="<i2").astype(np.float32) / 32768)
        while self.engine.is_ready(self.stream):
            self.engine.decode_stream(self.stream)
        text = self.engine.get_result(self.stream).strip()
        endpoint = self.engine.is_endpoint(self.stream)
        # Keep hesitation/correction fragments open rather than treating every
        # fixed pause as a sentence boundary. Ambiguous meaning is clarified later.
        incomplete = text.rstrip("，,。 ").endswith(
            ("然后", "但是", "不", "不是", "我要", "帮我", "播放", "打开")
        )
        return Recognition(text, bool(endpoint and text and not incomplete))

    async def accept(self, chunk):
        return await self._call(self._accept, chunk)

    async def close(self):
        self.closed = True
        self.worker.shutdown(wait=False, cancel_futures=True)
