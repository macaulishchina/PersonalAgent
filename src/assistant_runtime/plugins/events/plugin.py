"""Durable records only. Producers own collection and source lifecycle."""

from ...kernel.contracts import EventRequest, ObservationRequest, TaskRequest
from ...kernel.runtime import Manifest
from .ingress import Ingress
from .records import validate
from .settings import EventSettings
from .storage import Storage


class EventsPlugin:
    manifest = Manifest("events")

    def __init__(self, host):
        self.host = host
        self.settings = EventSettings.parse(host.config.events)
        self.storage = Storage(host.config.state_dir / "events", self.settings)
        self.context = self.ingress = None

    async def start(self, context):
        self.context = context
        context.own(self.close)
        await self.storage.start()
        self.ingress = Ingress(self.storage, context, self.settings.queue_size)
        context.register_event_sink(self.ingress)
        context.register(EventRequest, self.request)
        context.spawn(self.ingress.worker(), "ingress")

    async def close(self):
        if self.ingress:
            await self.ingress.close()
        await self.storage.close()

    async def append(self, source, record):
        return await self.ingress.write(source, "append", record=record)

    async def actor(self, caller):
        if caller is None:
            return "assistant", True
        info = await self.host.runtime.invoke(TaskRequest("execution_info", {"run_ids": [caller]}))
        run = info.get(caller)
        if not run or run["state"] not in {"running", "waiting_user"}:
            raise ValueError("Event tools require an active task execution")
        return "task:" + run["task_id"], run.get("events_management", False)

    async def request(self, request):
        action, args = request.action, request.arguments
        if not isinstance(args, dict):
            raise ValueError("Event arguments must be an object")
        if action in {"sources", "configure", "enable", "remove", "verify"}:
            return await self.host.runtime.invoke(ObservationRequest(action, args, request.caller_run))
        actor, manager = await self.actor(request.caller_run)
        if action == "capabilities":
            sources = {}
            if ObservationRequest in self.host.runtime.handlers:
                sources = await self.host.runtime.invoke(
                    ObservationRequest("capabilities", {}, request.caller_run)
                )
            return {
                "adapters": sources.get("adapters", {}),
                "manual_evidence": ["user_statement", "assistant_summary"],
                "quota_bytes": self.settings.quota_bytes,
                "page_default": 50,
                "page_maximum": 100,
                "source_management": manager and bool(sources),
            }
        if action == "record":
            record = validate(args["record"])
            if record["evidence"] not in {"user_statement", "assistant_summary"}:
                raise ValueError("Manual records must identify statement/summary evidence")
            return await self.append(actor, record)
        if action in {"correct", "delete"}:
            if not manager:
                old = await self.storage.call("get", id=args["id"])
                if old["source"] != actor and not (action == "delete" and old.get("deleted_at") is not None):
                    raise ValueError("Changing another source requires events_management")
            if action == "correct":
                record = validate(args["record"])
                if record["evidence"] not in {"user_statement", "assistant_summary"}:
                    raise ValueError("Corrections must be labeled as statements/summaries")
            result = await self.storage.call(action, **args)
            if action == "delete":
                self.ingress.health()
                self.context.publish("events.capacity_available")
            if result.get("cursor"):
                self.context.publish("events.committed", id=result["id"], cursor=result["cursor"])
            return result
        if action in {"query", "get", "changes", "stats", "status"}:
            result = await self.storage.call(action, **args)
            if action == "status":
                result.update(quota_bytes=self.settings.quota_bytes, ingress=self.ingress.status())
            return result
        raise ValueError("Unknown events action")

    def diagnostics(self):
        return self.ingress.status() if self.ingress else {"state": "unavailable"}
