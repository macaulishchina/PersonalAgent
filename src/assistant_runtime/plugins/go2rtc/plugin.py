"""Register gateway cameras through the shared device and video capabilities."""

import asyncio

from ...kernel.contracts import DeviceObservation
from ...kernel.runtime import Manifest
from .camera import Go2RTCCamera


class Go2RTCPlugin:
    manifest = Manifest("go2rtc")

    def __init__(self, config, directory, video):
        self.directory, self.video = directory, video
        self.providers = {}
        for ident, spec in config.devices.items():
            if spec["adapter"] == "go2rtc" and spec.get("enabled", True):
                if spec["kind"] != "camera":
                    raise ValueError("go2rtc adapter only supports camera inputs")
                self.providers[ident] = Go2RTCCamera(ident, spec)

    async def start(self, context):
        self.context = context
        for ident, provider in self.providers.items():
            context.own(provider.close)
            await provider.start()

            async def probe(ident=ident):
                await self.observe(ident)

            self.directory.register("go2rtc", ident, ("video.snapshot",), probe)
            self.video.register("go2rtc", ident, provider)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident):
        provider = self.providers[ident]
        try:
            data = await provider.inspect()
            observation = DeviceObservation(
                "error" if provider.last_error else "available", data, error=provider.last_error
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            error = "go2rtc probe timed out" if isinstance(exc, TimeoutError) else str(exc)[:200]
            observation = DeviceObservation("unavailable", error=error)
        self.directory.observe("go2rtc", ident, observation)

    async def watch(self):
        while True:
            await asyncio.gather(*(self.observe(ident) for ident in self.providers))
            await asyncio.sleep(5)
