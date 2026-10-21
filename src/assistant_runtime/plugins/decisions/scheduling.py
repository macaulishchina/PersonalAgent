"""Bounded admission with one worker reserved for interactive control."""

import asyncio
from collections import deque


class DecisionQueue:
    def __init__(self, capacity):
        self.capacity = capacity
        self.interactive, self.background = deque(), deque()
        self.changed, self.drained = asyncio.Event(), asyncio.Event()
        self.drained.set()
        self.pending = 0

    def qsize(self):
        return len(self.interactive) + len(self.background)

    def full(self, interactive=False):
        return self.qsize() >= (self.capacity if interactive else max(1, self.capacity - 1))

    def empty(self):
        return not self.qsize()

    def put_nowait(self, job):
        interactive = bool(job.get("active") and job["kind"] == "voice_intent")
        if self.full(interactive):
            raise asyncio.QueueFull
        target = self.interactive if interactive else self.background
        target.append(job)
        self.pending += 1
        self.drained.clear()
        self.changed.set()

    async def get(self, interactive_only=False):
        while True:
            if self.interactive:
                return self.interactive.popleft()
            if not interactive_only and self.background:
                return self.background.popleft()
            self.changed.clear()
            await self.changed.wait()

    def task_done(self):
        self.pending -= 1
        if not self.pending:
            self.drained.set()

    async def join(self):
        await self.drained.wait()
