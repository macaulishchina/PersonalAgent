import asyncio
from assistant_runtime.api.contracts import DeviceObservation
from .windows_camera import Camera
from .declaration import DECLARATION


class WindowsCameraPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config, directory, video):
        self.directory, self.video = directory, video
        self.providers = {
            ident: Camera(ident, spec)
            for ident, spec in config.devices.items()
            if spec["adapter"] == "v4l2" and spec.get("enabled", True)
        }

    def maintenance_pending(self):
        return []

    async def start(self, context):
        for ident, provider in self.providers.items():

            async def probe(ident=ident):
                await self.observe(ident)

            self.directory.register("v4l2", ident, ("video.snapshot",), probe)
            self.video.register("v4l2", ident, provider)
            context.own(provider.close)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident):
        try:
            observation = DeviceObservation("available", await self.providers[ident].inspect())
        except Exception as exc:
            observation = DeviceObservation("unavailable", error=str(exc)[:200])
        self.directory.observe("v4l2", ident, observation)

    async def watch(self):
        while True:
            for ident in self.providers:
                # Do not compete with an operator's active acquisition.
                if not self.providers[ident].lock.locked():
                    await self.observe(ident)
            await asyncio.sleep(5)
