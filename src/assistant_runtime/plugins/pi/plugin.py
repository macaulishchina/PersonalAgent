"""Pi execution plugin. Speech is explicitly unavailable pending independent acceptance."""

from ...kernel.runtime import Manifest
from .backend import PiBackend
from .voice import DisabledVoice


class PiPlugin:
    manifest = Manifest("pi", ("devices", "ai_usage"), critical=True)

    def __init__(
        self,
        config,
        store,
        on_audio,
        on_event,
        speak,
        control,
        mcp_registry=None,
        usage=None,
        *,
        skill_paths=(),
    ):
        self.backend = PiBackend(config, store, speak, control, mcp_registry, usage, skill_paths=skill_paths)
        self.voice = DisabledVoice()

    async def start(self, context):
        if self.backend.integrations:
            self.backend.integrations.context = context
        context.own(self.backend.close)
        await self.backend.start()

        async def watch():
            await self.backend.events_task

        context.spawn(watch(), "provider-events")

    def diagnostics(self):
        return {
            "backend": "pi",
            "execution_permissions": "full",
            "main_model": self.backend.resolved_model,
            "main_reasoning_effort": self.backend.resolved_reasoning_effort,
            "voice": {"state": "disabled", "reason": "pending_independent_acceptance"},
            "sandbox": {"state": "not_implemented", "todo": True},
        }
