"""Media is bounded and generation-aware; it does not pass through the event bus."""

import asyncio
from assistant_runtime.api.contracts import AudioFrame


class AudioStream:
    def __init__(self, capacity=75):
        self.queue: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=capacity)
        self.generation = 0
        self.dropped = 0

    def reset(self):
        self.generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    def put(self, frame: AudioFrame):
        if frame.generation != self.generation:
            self.dropped += 1
            return False
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        self.queue.put_nowait(frame)
        return True
