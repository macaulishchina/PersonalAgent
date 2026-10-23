"""Owned dormant speech control and maintenance; no application assembly policy."""

from assistant_runtime.api.contracts import Control, Trigger


class AssistantPlugin:
    from assistant_runtime.kernel.runtime import Manifest

    manifest = Manifest("assistant", ("pipewire", "voice_disabled", "pi"), critical=True)

    def __init__(self, service, dependencies):
        self.manifest = self.Manifest("assistant", dependencies, critical=True)
        self.service = service

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
