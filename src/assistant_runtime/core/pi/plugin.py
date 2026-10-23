"""Pi execution plugin. Speech is explicitly unavailable pending independent acceptance."""

from .declaration import DECLARATION

from assistant_runtime.core.pi.backend import PiBackend


class PiPlugin:
    manifest = DECLARATION.manifest

    def __init__(
        self,
        config,
        store,
        speak=None,
        control=None,
        mcp_registry=None,
        usage=None,
        *,
        skill_paths=(),
        foreground=True,
    ):
        self.backend = PiBackend(
            config, store, speak, control, mcp_registry, usage, skill_paths=skill_paths, foreground=foreground
        )

    async def start(self, context):
        if self.backend.integrations:
            self.backend.integrations.context = context
        context.own(self.backend.close)
        await self.backend.start()

        async def watch():
            await self.backend.events_task

        if self.backend.events_task is not None:
            context.spawn(watch(), "provider-events")

    def diagnostics(self):
        return {
            "backend": "pi",
            "execution_permissions": "full",
            "main_model": self.backend.resolved_model,
            "main_reasoning_effort": self.backend.resolved_reasoning_effort,
            "sandbox": {"state": "not_implemented", "todo": True},
        }
