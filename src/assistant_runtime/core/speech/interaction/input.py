"""Continuous bounded capture handoff. Missing samples invalidate the utterance."""

import asyncio
from collections import deque

from assistant_runtime.api.speech import InputChunk


class InputBuffer:
    def __init__(self, seconds=30):
        self.limit = int(seconds * 32000)
        self.frames = deque()
        self.size = 0
        self.sequence = 0
        self.gaps = 0
        self.previous_end = None
        self.queue = asyncio.Queue(maxsize=max(100, int(seconds * 50)))

    def offer(self, pcm, ended, raw=None):
        self.sequence += 1
        chunk = InputChunk(self.sequence, ended - len(pcm) / 32000, ended, pcm)
        interrupted = self.previous_end is not None and chunk.started - self.previous_end > 0.25
        self.previous_end = ended
        self.frames.append(chunk)
        self.size += len(pcm)
        while self.size > self.limit:
            self.size -= len(self.frames.popleft().pcm)
        if self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()
            interrupted = True
        if interrupted:
            self.gaps += 1
        self.queue.put_nowait((chunk, raw or pcm, interrupted))
        return chunk

    def since(self, sequence):
        if self.frames and sequence < self.frames[0].sequence:
            raise BufferError("Speech input history exhausted")
        return tuple(frame for frame in self.frames if frame.sequence >= sequence)

    def clear(self):
        self.frames.clear()
        self.size = 0
        self.previous_end = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
