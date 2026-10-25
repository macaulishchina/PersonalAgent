"""Resizable async admission. Lowering capacity never revokes existing leases."""

import asyncio


class Capacity:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.changed = asyncio.Event()

    def resize(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("Capacity must be a positive integer")
        self.limit = limit
        self.changed.set()

    async def acquire(self):
        while self.active >= self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.active += 1
        return True

    def release(self):
        if self.active < 1:
            raise RuntimeError("Capacity lease released twice")
        self.active -= 1
        self.changed.set()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()
