"""Explicit trusted plugins, owned resources, independent event queues and calls."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable

from .contracts import API_VERSION, Event
from .event_reporting import EventReporter, EventSink, identifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Manifest:
    name: str
    requires: tuple[str, ...] = ()
    api_version: int = API_VERSION
    critical: bool = False


class Context:
    def __init__(self, runtime, name):
        self.runtime, self.name = runtime, name
        self.generation = 1
        self.tasks: set[asyncio.Task] = set()
        self.resources = AsyncExitStack()
        self.error: str | None = None
        self.state = "starting"

    def own(self, cleanup):
        self.resources.push_async_callback(cleanup)

    def spawn(self, coroutine, label="worker"):
        task = asyncio.create_task(coroutine, name=f"{self.name}:{label}")
        self.tasks.add(task)

        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                self.error = f"{label}: {type(t.exception()).__name__}: {t.exception()}"[:240]
                self.state = "degraded"
                log.error("plugin_failure %s %s", self.name, self.error)
                self.runtime.publish(
                    Event("plugin.failed", self.name, {"error": self.error}, generation=self.generation)
                )
                if self.runtime.plugins[self.name].manifest.critical:
                    self.runtime.fatal.set()

        task.add_done_callback(done)
        return task

    def event_reporter(self, source=None):
        source = identifier(source or self.name)
        if source not in self.runtime.event_owners and len(self.runtime.event_owners) >= 256:
            raise ValueError("Event source binding limit reached")
        owner = self.runtime.event_owners.setdefault(source, self.name)
        if owner != self.name:
            raise ValueError("Event source already belongs to another plugin")
        return EventReporter(self, source)

    def register_event_sink(self, sink):
        if self.runtime.event_sink is not None:
            raise ValueError("Event recording already has an owner")
        self.runtime.event_sink = sink

        async def unregister():
            if self.runtime.event_sink is sink:
                self.runtime.event_sink = None

        self.own(unregister)

    def get(self, key, default=None):
        return self.runtime.store.get(f"plugin.{self.name}.{key}", default)

    def set(self, key, value):
        self.runtime.store.set(f"plugin.{self.name}.{key}", value)

    def register(self, request_type, handler):
        self.runtime.register(self.name, request_type, handler)

    def publish(self, kind, **data):
        self.runtime.publish(Event(kind, self.name, data, generation=self.generation))

    async def close(self):
        self.generation += 1
        self.state = "stopping"
        tasks = list(self.tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.resources.aclose()
        finally:
            self.state = "stopped"


class Runtime:
    def __init__(self, store, bindings=None):
        self.event_sink: EventSink | None = None
        self.event_owners: dict[str, str] = {}
        self.store = store
        self.bindings = bindings or {}
        self.plugins: dict[str, Any] = {}
        self.contexts: dict[str, Context] = {}
        self.handlers: dict[type, tuple[str, Callable[..., Awaitable]]] = {}
        self.subscribers: dict[str, asyncio.Queue] = {}
        self.dropped: dict[str, int] = {}
        self.started: list[str] = []
        self.fatal = asyncio.Event()
        self.ready = asyncio.Event()

    def add(self, plugin):
        m = plugin.manifest
        if m.api_version != API_VERSION or m.name in self.plugins:
            raise ValueError(f"Incompatible or duplicate plugin: {m.name}")
        self.plugins[m.name] = plugin

    def resolve(self, capability):
        name = self.bindings[capability]
        return self.plugins[name]

    def register(self, owner, request_type, handler):
        if request_type in self.handlers:
            raise ValueError(f"Multiple handlers for {request_type.__name__}")
        self.handlers[request_type] = owner, handler

    async def invoke(self, request, timeout=15):
        if type(request) not in self.handlers:
            raise RuntimeError(f"{type(request).__name__} unavailable: plugin is starting or disabled")
        owner, handler = self.handlers[type(request)]
        context = self.contexts[owner]
        epoch = context.generation
        if context.state not in {"ready", "degraded"}:
            raise RuntimeError(f"{owner} unavailable")
        result = await asyncio.wait_for(handler(request), timeout)
        if epoch != context.generation:
            raise RuntimeError("Stale plugin result discarded")
        return result

    def subscribe(self, name, capacity=128):
        if name in self.subscribers:
            raise ValueError("Duplicate event subscriber")
        queue = self.subscribers[name] = asyncio.Queue(maxsize=capacity)
        self.dropped[name] = 0
        return queue

    def unsubscribe(self, name):
        self.subscribers.pop(name, None)
        self.dropped.pop(name, None)

    def publish(self, event):
        context = self.contexts.get(event.source)
        if context and event.generation != context.generation:
            return
        for name, queue in self.subscribers.items():
            if queue.full():
                queue.get_nowait()
                queue.task_done()
                self.dropped[name] += 1
            queue.put_nowait(event)

    def order(self):
        result, visiting = [], set()

        def visit(name):
            if name in result:
                return
            if name in visiting or name not in self.plugins:
                raise ValueError(f"Missing dependency or cycle: {name}")
            visiting.add(name)
            for dependency in self.plugins[name].manifest.requires:
                visit(dependency)
            visiting.remove(name)
            result.append(name)

        for name in self.plugins:
            visit(name)
        for name in self.bindings.values():
            if name not in self.plugins:
                raise ValueError(f"Unbound capability: {name}")
        return result

    async def start(self):
        self.ready.clear()
        try:
            for name in self.order():
                context = self.contexts[name] = Context(self, name)
                self.started.append(name)
                try:
                    await self.plugins[name].start(context)
                    context.state = "ready"
                except Exception as exc:
                    context.error = f"{type(exc).__name__}: {exc}"[:240]
                    await context.close()
                    context.state = "failed"
                    if self.plugins[name].manifest.critical:
                        raise
            self.ready.set()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.ready.clear()
        for name in reversed(self.started):
            try:
                await self.contexts[name].close()
            except Exception:
                log.exception("plugin_cleanup_failed %s", name)
        self.started.clear()

    def health(self):
        return {
            name: {
                "state": ctx.state,
                "generation": ctx.generation,
                "error": ctx.error,
                "tasks": len(ctx.tasks),
            }
            for name, ctx in self.contexts.items()
        }
