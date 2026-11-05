"""Resizable async admission. Lowering capacity never revokes existing leases."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections import deque

current_capacity_lease = ContextVar("execution_capacity_lease", default=None)


class CapacityLease:
    def __init__(self, capacity):
        self.capacity = capacity
        self.lock = asyncio.Lock()
        self.closed = False
        self.borrowed = False

    def close(self):
        self.closed = True
        if not self.borrowed:
            self.capacity.release()


class Capacity:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.changed = asyncio.Event()
        self.inference_waiting = 0
        self.leases = {}
        self.waiters = deque()

    def reserve(self, ident):
        lease = CapacityLease(self)
        self.leases[ident] = lease
        return lease

    def release_reservation(self, ident):
        self.leases.pop(ident).close()

    @asynccontextmanager
    async def inference(self):
        lease = current_capacity_lease.get()
        if lease is not None and lease.capacity is self:
            async with lease.lock:
                if lease.closed:
                    raise RuntimeError("Execution admission has ended")
                lease.borrowed = True
                try:
                    yield
                finally:
                    lease.borrowed = False
                    if lease.closed:
                        self.release()
        else:
            self.inference_waiting += 1
            try:
                await self.acquire()
            finally:
                self.inference_waiting -= 1
            try:
                yield
            finally:
                self.release()

    def resize(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("Capacity must be a positive integer")
        self.limit = limit
        self.changed.set()
        self.drain()

    def drain(self):
        while self.waiters and self.active < self.limit:
            future = self.waiters.popleft()
            if not future.done():
                self.active += 1
                future.set_result(True)

    async def acquire(self):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        self.drain()
        try:
            return await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
                if future in self.waiters:
                    self.waiters.remove(future)
            raise

    def release(self):
        if self.active < 1:
            raise RuntimeError("Capacity lease released twice")
        self.active -= 1
        self.changed.set()
        self.drain()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()
