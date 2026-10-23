"""Source configuration/lifecycle only. Producers choose how and when to observe."""

from .declaration import DECLARATION

import asyncio
import time

from assistant_runtime.api.contracts import EventRequest, ObservationRequest, TaskRequest
from assistant_runtime.api import identifier
from .registry import Registry


class ObservationsPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, api, providers):
        self.api, self.providers = api, providers
        self.registry = Registry(api.resource("registry_root"), providers)
        self.instances = {}
        self.lock = asyncio.Lock()
        self.context = None
        self.accepting = False

    async def start(self, context):
        self.context = context
        context.own(self.close)
        await asyncio.to_thread(self.registry.load)
        context.register(ObservationRequest, self.request)
        self.accepting = True
        for ident, definition in self.registry.sources.items():
            if definition["enabled"]:
                self.launch(ident, definition)
        self.updates = context.subscribe(8)
        context.spawn(self.capacity_updates(), "capacity-updates")

    async def capacity_updates(self):
        while True:
            event = await self.updates.get()
            if event.kind == "events.capacity_available":
                for instance, _, _ in self.instances.values():
                    retry = getattr(instance, "retry", None)
                    if retry is not None:
                        retry.set()

    def launch(self, ident, definition):
        reporter = self.context.event_reporter(ident)
        instance = self.providers[definition["adapter"]].create(reporter, definition["config"])
        task = self.context.spawn(self.run(ident, instance, reporter), "source:" + ident)
        self.instances[ident] = instance, reporter, task

    async def run(self, ident, instance, reporter):
        # Independent startup and failure: an unavailable source cannot delay others.
        try:
            try:
                status = await self.api.invoke(EventRequest("status"))
                prior = next((row for row in status["coverage"] if row["source"] == ident), {})
                at = prior.get("gap_start") or prior.get("observed_until") or time.time()
                await reporter.gap("startup_or_resume", at=at)
            except Exception:
                instance.health = {"state": "degraded", "error": "recording_unavailable"}
            await instance.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            instance.health = {
                "state": "degraded",
                "error": "source_failed",
                "error_class": type(exc).__name__,
            }

    async def stop(self, ident, reason):
        entry = self.instances.pop(ident, None)
        if not entry:
            return
        instance, reporter, task = entry
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Stop producer, reject new offers, then finish admitted edges before opening the gap.
        try:
            await reporter.finish(reason)
        except Exception:
            pass  # storage health retains the failure; shutdown must still complete

    async def close(self):
        self.accepting = False
        async with self.lock:
            for ident in list(self.instances):
                await self.stop(ident, "service_stopped")

    def health(self):
        return {
            ident: (
                self.instances[ident][0].health
                if ident in self.instances
                else {"state": "disabled" if not definition["enabled"] else "unavailable"}
            )
            for ident, definition in self.registry.sources.items()
        }

    async def authorize(self, caller):
        if caller is None:
            return True
        info = await self.api.invoke(TaskRequest("execution_info", {"run_ids": [caller]}))
        run = info.get(caller)
        if not run or run["state"] not in {"running", "waiting_user"}:
            raise ValueError("Event tools require an active task execution")
        return run.get("events_management", False)

    async def reconfigure(self, action, ident, args):
        if not self.accepting:
            raise RuntimeError("Sources are stopping")
        result = await asyncio.to_thread(
            self.registry.update, action, ident, args["revision"], args.get("definition"), args.get("enabled")
        )
        await self.stop(ident, "source_" + action)
        definition = self.registry.sources.get(ident)
        if definition and definition["enabled"] and self.accepting:
            self.launch(ident, definition)
        return result

    async def request(self, request):
        manager = await self.authorize(request.caller_run)
        action, args = request.action, request.arguments
        if action == "sources":
            return {**self.registry.describe(), "health": self.health()}
        if action == "capabilities":
            return {
                "adapters": {name: provider.description for name, provider in self.providers.items()},
                "source_management": manager,
            }
        if action not in {"configure", "enable", "remove", "verify"}:
            raise ValueError("Unknown source action")
        if not manager:
            raise ValueError("Source management requires events_management task resource")
        async with self.lock:
            ident = identifier(args["id"])
            if action == "verify":
                definition = self.registry.sources[ident]
                reporter = self.context.event_reporter(ident)
                instance = self.providers[definition["adapter"]].create(reporter, definition["config"])
                try:
                    return await instance.verify()
                except Exception:
                    raise RuntimeError(
                        "Source verification failed; inspect connectivity and private credentials"
                    ) from None
                finally:
                    await reporter.close()
            operation = asyncio.create_task(self.reconfigure(action, ident, args))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise

    def diagnostics(self):
        return {"sources": self.health()}
