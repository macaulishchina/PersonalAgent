"""Expose the shared execution service as a declared core capability."""

from .declaration import DECLARATION, settings
from .service import ExecutionService


class ExecutionPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, host):
        self.host, self.service = host, None

    async def start(self, context):
        host = self.host
        self.service = ExecutionService(
            host.backend.client,
            host.store.db,
            host.workfiles,
            host.ai_usage.service,
            skill_paths=host.backend.client.skill_paths,
            default_model=host.config.main_model,
            default_effort=host.config.main_reasoning_effort,
            servers=self.servers,
            intake=getattr(host, "intake", None),
            **settings(host.config.execution),
        )
        host.backend.executions = self.service
        if host.backend.foreground:
            host.store.executions.outcome = self.service.records.get

        async def detach():
            host.backend.executions = None
            host.backend.client.admission = None
            if host.backend.foreground:
                host.store.executions.outcome = None

        context.own(detach)
        await self.service.start(context)

    async def maintenance_cancel(self):
        if self.service:
            for row in self.service.records.active():
                if row["data"].get("source", "execution") == "execution":
                    await self.service.cancel(row)

    def maintenance_pending(self):
        if self.service is None:
            return []
        records = {row["id"]: row for row in self.service.records.active()}
        for ident in self.service.admission.leases:
            records[ident] = self.service.records.get(ident)
        return [{"id": row["id"], "state": row["state"]} for row in records.values()]

    def servers(self):
        from assistant_runtime.api.plugins import current_call
        from assistant_runtime.api.ai_usage import current_usage
        from assistant_runtime.core.pi.mcp import server_config

        host = self.host
        call, usage = current_call.get(), current_usage.get()
        task_run = call.task_run if call else None
        if not task_run and usage and usage.task_id:
            task_run = usage.caller
        if host.backend.integrations:
            return host.backend.integrations.configuration(
                "background" if task_run else "foreground",
                task_run=task_run,
            )
        return server_config(host.config.socket, task_run=task_run, plugins=host.config.enabled_plugins)
