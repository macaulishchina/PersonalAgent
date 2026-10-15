"""Bounded PCM output; PipeWire owns the playback clock, WebRTC supplies audio."""

import asyncio
import math
import logging
import time

import numpy as np

from .cues import wake_chime


class StreamPlayer:
    rate = 24000
    frame_bytes = 960  # 20 ms, mono PCM16

    def __init__(self, route):
        self.route = route
        self.queue = asyncio.Queue(maxsize=75)  # at most 1.5 seconds of local audio
        self.epoch = 0
        self.transport = None
        self.remainder = b""
        self.lock = asyncio.Lock()
        self.last_error = None
        self.last_started_at = None
        self.last_write = 0.0
        self.last_voice_write = 0.0
        self.pending_voice_frames = 0
        self.stream_started = False
        self.last_queue_ms = self.max_queue_ms = 0.0
        self.written_bytes = 0
        self.discarded_bytes = 0
        self.last_stop_seconds = None
        self.suppressed = False
        self.input_speaking = False
        self.silence_bytes = 0
        self.last_received = 0.0
        self.manual_muted = False

    @property
    def playing(self):
        return self.pending_voice_frames > 0 or time.monotonic() - self.last_voice_write < 0.18

    def feed(self, pcm):
        if self.manual_muted:
            self.discarded_bytes += len(pcm)
            return
        now = time.monotonic()
        gap = now - self.last_received
        self.last_received = now
        if self.suppressed:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            quiet = len(samples) and np.sqrt(np.mean(samples * samples)) < 150
            self.silence_bytes = self.silence_bytes + len(pcm) if quiet else 0
            # Live has no response ID / audio-done event. Resume after a quiet
            # output boundary, never just after an instruction acknowledgement.
            if not self.input_speaking and (self.silence_bytes >= self.rate * 2 * 0.2 or gap > 0.5):
                self.suppressed = False
            else:
                self.discarded_bytes += len(pcm)
                return
        data = self.remainder + pcm
        complete = len(data) // self.frame_bytes * self.frame_bytes
        self.remainder = data[complete:]
        for offset in range(0, complete, self.frame_bytes):
            frame = data[offset : offset + self.frame_bytes]
            voiced = np.max(np.abs(np.frombuffer(frame, dtype="<i2").astype(np.int32))) >= 80
            # Ignore idle silence before playback, but preserve EVERY subsequent
            # frame. Removing inter-word silence starves pacat and alters timing.
            # Playback activity is measured from voice, not silent PCM writes.
            if not self.stream_started and not voiced:
                continue
            self.stream_started = True
            try:
                self.queue.put_nowait((self.epoch, frame, voiced, now))
                self.pending_voice_frames += int(voiced)
            except asyncio.QueueFull:
                self.last_error = "Audio output stalled; queued audio discarded"
                self.epoch += 1
                self._clear()
                self.suppressed = True
                self.silence_bytes = 0
                return

    def _clear(self):
        self.discarded_bytes += len(self.remainder)
        self.remainder = b""
        while not self.queue.empty():
            _, pcm, voiced, _ = self.queue.get_nowait()
            self.discarded_bytes += len(pcm)
            self.pending_voice_frames -= int(voiced)
            self.queue.task_done()
        self.stream_started = False

    async def stop(self, suppress=True):
        start = time.monotonic()
        self.epoch += 1
        self._clear()
        self.suppressed = suppress
        self.silence_bytes = 0
        async with self.lock:
            await self._kill()
            self.last_write = self.last_voice_write = 0
        self.last_stop_seconds = time.monotonic() - start

    async def _kill(self):
        if self.transport:
            await self.transport.close()
        self.transport = None

    async def cue(self, error=False):
        # Local feedback stays available when network/authentication fails.
        await self.stop(suppress=False)
        if error:
            t = np.arange(int(self.rate * 0.12)) / self.rate
            envelope = np.sin(np.linspace(0, math.pi, len(t))) ** 2
            pcm = (np.sin(2 * math.pi * 400 * t) * envelope * 5500).astype("<i2").tobytes()
        else:
            pcm = wake_chime(self.rate)
        # Include enough silence to start pacat's prebuffer even when a short
        # local cue is the only audio available.
        self.feed(pcm + bytes(self.frame_bytes * 6))

    async def run(self):
        try:
            while True:
                epoch, pcm, voiced, received = await self.queue.get()
                try:
                    if epoch != self.epoch:
                        continue
                    if not self.transport or not self.transport.healthy:
                        await self.route.ensure(check_volume=True)
                    if epoch != self.epoch:
                        continue
                    async with self.lock:
                        if self.transport and not self.transport.healthy:
                            await self._kill()
                        if not self.transport:
                            self.transport = await self.route.open_output(self.rate)
                        if epoch != self.epoch:
                            continue
                        self.last_queue_ms = (time.monotonic() - received) * 1000
                        self.max_queue_ms = max(self.max_queue_ms, self.last_queue_ms)
                        await self.transport.write(pcm)
                        self.last_write = time.monotonic()
                        if voiced:
                            self.last_voice_write = self.last_write
                        if self.last_started_at is None:
                            self.last_started_at = self.last_write
                        self.written_bytes += len(pcm)
                        self.last_error = None
                    # Input already has a WebRTC clock. Writing immediately lets
                    # pacat's small prebuffer absorb jitter; sleeping here adds
                    # scheduling drift and repeatedly underruns the device.
                    await asyncio.sleep(0)
                except (OSError, RuntimeError, TimeoutError) as exc:
                    logging.getLogger(__name__).warning("audio_output_failed", exc_info=True)
                    self.last_error = "Audio output unavailable: " + type(exc).__name__
                    await self.stop()
                finally:
                    self.pending_voice_frames -= int(voiced)
                    self.queue.task_done()
        finally:
            async with self.lock:
                await self._kill()
