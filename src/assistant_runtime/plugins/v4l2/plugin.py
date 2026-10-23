"""Standard camera registration, availability and owned acquisition cleanup."""

from .declaration import DECLARATION

import asyncio

from assistant_runtime.api.contracts import DeviceObservation
from .camera import V4L2Camera


class V4L2Plugin:
    manifest = DECLARATION.manifest

    def __init__(self, config, directory, video):
        self.directory, self.video = directory, video
        self.providers = {}
        for ident, spec in config.devices.items():
            if spec["adapter"] == "v4l2" and spec.get("enabled", True):
                if spec["kind"] != "camera":
                    raise ValueError("V4L2 adapter only supports camera inputs")
                self.providers[ident] = V4L2Camera(ident, spec)

    async def start(self, context):
        self.context = context
        for ident, provider in self.providers.items():

            async def probe(ident=ident):
                await self.observe(ident)

            self.directory.register("v4l2", ident, ("video.snapshot",), probe)
            self.video.register("v4l2", ident, provider)
            context.own(provider.close)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident):
        provider = self.providers[ident]
        try:
            data = await provider.inspect()
            observation = DeviceObservation(
                "error" if provider.last_error else "available", data, error=provider.last_error
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            observation = DeviceObservation("unavailable", error=str(exc)[:200])
        self.directory.observe("v4l2", ident, observation)

    async def watch(self):
        while True:
            for ident in self.providers:
                await self.observe(ident)
            await asyncio.sleep(5)
