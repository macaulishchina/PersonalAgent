import asyncio
from assistant_runtime.api.contracts import DeviceObservation
from .windows_audio import Endpoint
from .declaration import DECLARATION


class WindowsAudioPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config, directory, inputs, outputs):
        self.config = config
        self.directory, self.inputs, self.outputs = directory, inputs, outputs
        self.providers = {
            ident: Endpoint(spec, config.minimum_volume)
            for ident, spec in config.devices.items()
            if spec["adapter"] == "pipewire" and spec.get("enabled", True)
        }

    def maintenance_pending(self):
        return []  # Device operations and stream leases fence replacement.

    async def prepare_configuration(self, config):
        from .policy import volume_update

        return volume_update(self.config, config, self.providers.values())

    async def start(self, context):
        for ident, provider in self.providers.items():
            microphone = provider.spec["kind"] == "microphone"
            registry = self.inputs if microphone else self.outputs
            registry.register("pipewire", ident, provider)

            async def probe(ident=ident):
                await self.observe(ident)

            self.directory.register(
                "pipewire",
                ident,
                ("audio.capture",) if microphone else ("audio.playback", "audio.volume"),
                probe,
            )
            context.own(provider.close)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident):
        try:
            value = DeviceObservation("available", await self.providers[ident].inspect())
        except Exception as exc:
            value = DeviceObservation("unavailable", error=str(exc)[:200])
        self.directory.observe("pipewire", ident, value)

    async def watch(self):
        while True:
            for ident in self.providers:
                await self.observe(ident)
            await asyncio.sleep(3)

    def diagnostics(self):
        return {"backend": "wasapi", "voice_state": "disabled", "devices": len(self.providers)}
