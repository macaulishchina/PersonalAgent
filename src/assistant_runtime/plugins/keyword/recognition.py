"""Local FSMN V2 wake detection and voice activity; Codex handles speech."""

import asyncio
import threading

import sherpa_onnx

from .fsmn import Fsmn


class Recognizer:
    def __init__(self, config):
        models = config.models
        self.detector = Fsmn(models / "fsmn-v2", config.wake_words)
        self.generation = 0
        self.stream_generation = 0
        self.lock = threading.Lock()
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(models / "silero_vad.onnx")
        cfg.silero_vad.min_silence_duration = 0.55
        cfg.silero_vad.min_speech_duration = 0.2
        cfg.silero_vad.max_speech_duration = 30
        cfg.sample_rate = 16000
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)

    def keyword(self, samples):
        generation = self.generation
        with self.lock:
            if self.stream_generation != generation:
                self.detector.reset()
                self.stream_generation = generation
            events = self.detector.feed(samples)
        if generation != self.generation:
            return None
        return events[0][1] if events else None

    async def detect(self, samples):
        generation = self.generation
        word = await asyncio.to_thread(self.keyword, samples)
        return word if generation == self.generation else None

    def reset_keyword(self):
        # Reset on the inference worker, avoiding a race or blocking the event
        # loop while an in-flight frame finishes. Its result is invalidated.
        self.generation += 1

    def diagnostics(self):
        return self.detector.diagnostics()
