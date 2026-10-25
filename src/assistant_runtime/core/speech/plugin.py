"""Owned dormant speech control and maintenance; no application assembly policy."""

from assistant_runtime.api.contracts import Control, Trigger
from assistant_runtime.api import plugin_manifest
from dataclasses import replace


class AssistantPlugin:
    manifest = plugin_manifest(__file__)

    def __init__(self, service, dependencies):
        self.manifest = replace(
            self.manifest,
            requires=dependencies,
            dependency_versions=tuple(
                (n, v) for n, v in self.manifest.dependency_versions if n in dependencies
            ),
        )
        self.service = service

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationPending
        from assistant_runtime.core.configuration.updates import attributes
        from .quiet import QuietHours
        from .dialogue import Dialogue
        from .interaction.input import InputBuffer

        if self.service.active or self.service.transition.locked():
            raise ConfigurationPending()
        return attributes(
            [
                (self.service, "quiet_hours", QuietHours.from_settings(config.quiet_hours)),
                (self.service, "dialogue", Dialogue(config.voice_follow_up_seconds)),
                (
                    self.service,
                    "input_buffer",
                    InputBuffer(config.interaction.get("input_buffer_seconds", 30)),
                ),
            ],
            ready=lambda: not self.service.active and not self.service.transition.locked(),
        )

    async def start(self, context):
        self.service.context = context
        context.register(Control, self.service.control)
        from assistant_runtime.api.contracts import VoiceDecisionRequest

        context.register(VoiceDecisionRequest, self.service.voice_control.request)

        async def trigger(request):
            accepted = await self.service.wake(source=request.source)
            context.publish("assistant.triggered", source=request.source)
            return {"accepted": bool(accepted)}

        context.register(Trigger, trigger)
        context.spawn(self.service._events(), "voice-events")
        context.spawn(self.service._maintenance(), "idle-and-devices")
        if self.service.interaction:
            context.own(self.service.interaction.close)
            context.spawn(self.service.interaction.prepare(), "speech-prepare")
