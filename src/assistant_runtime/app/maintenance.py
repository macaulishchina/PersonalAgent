"""Operator lifecycle coordination over owner-provided work evidence."""


class Maintenance:
    def __init__(self, host):
        self.host = host

    def pause(self):
        if not self.host.intake.paused:
            self.host.intake.paused = True
            for plugin in self.host.runtime.plugins.values():
                callback = getattr(plugin, "maintenance_pause", None)
                if callback:
                    callback()
        return self.status()

    def resume(self):
        for plugin in self.host.runtime.plugins.values():
            callback = getattr(plugin, "maintenance_resume", None)
            if callback:
                callback()
        self.host.intake.paused = False
        return self.status()

    async def cancel(self):
        import asyncio

        await asyncio.gather(
            *(
                plugin.maintenance_cancel()
                for plugin in self.host.runtime.plugins.values()
                if hasattr(plugin, "maintenance_cancel")
            ),
            return_exceptions=True,
        )
        return self.status()

    def status(self):
        blockers = [
            {"owner": "admission", "id": ident, "state": label}
            for ident, label in self.host.intake.pending.items()
        ]
        for name, plugin in self.host.runtime.plugins.items():
            context = self.host.runtime.contexts.get(name)
            if context:
                blockers.extend(
                    {"owner": name, "state": "capability_call"} for call in context.calls if not call.done()
                )
            pending = getattr(plugin, "maintenance_pending", None)
            if pending:
                blockers.extend({"owner": name, **item} for item in pending())
            elif (
                self.host.intake.paused
                and context
                and context.tasks
                and (
                    type(getattr(plugin, "instance", plugin)).__module__.startswith(
                        "assistant_runtime.plugins."
                    )
                    or hasattr(plugin, "definition")
                )
            ):
                # Never infer completion of an extension's autonomous work from silence.
                blockers.append({"owner": name, "state": "drain_not_supported"})
        return {"paused": self.host.intake.paused, "blockers": blockers, "ready": not blockers}
