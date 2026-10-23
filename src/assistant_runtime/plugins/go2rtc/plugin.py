"""Register gateway cameras through the shared device and video capabilities."""

from .declaration import DECLARATION

import asyncio

from assistant_runtime.api.contracts import DeviceObservation
from .camera import Go2RTCCamera
from .ptz import Go2RTCPanTilt


class Go2RTCPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config, directory, video, camera_controls=None):
        self.directory, self.video = directory, video
        self.providers = {}
        self.camera_controls = camera_controls
        self.controllers = {}
        physical_controllers = set()
        for ident, spec in config.devices.items():
            if spec["adapter"] == "go2rtc" and spec.get("enabled", True):
                if spec["kind"] != "camera":
                    raise ValueError("go2rtc adapter only supports camera inputs")
                self.providers[ident] = Go2RTCCamera(ident, spec, config.state_dir / "video-recordings")
                if "pan_tilt" in spec["connection"]:
                    physical = spec.get("physical_id", ident)
                    if physical in physical_controllers:
                        raise ValueError("One pan/tilt controller is allowed per physical camera")
                    physical_controllers.add(physical)
                    self.controllers[ident] = Go2RTCPanTilt(self.providers[ident])

    async def start(self, context):
        self.context = context
        for ident, provider in self.providers.items():
            context.own(provider.close)
            await provider.start()

            async def probe(ident=ident):
                await self.observe(ident)

            capabilities = ["video.snapshot", "video.record"]
            if ident in self.controllers:
                if self.camera_controls is None:
                    raise ValueError("Pan/tilt requires the shared camera control registry")
                controller = self.controllers[ident]
                context.own(controller.close)
                self.camera_controls.register("go2rtc", ident, controller)
                capabilities.append("camera.pan_tilt")
            self.directory.register("go2rtc", ident, capabilities, probe)
            self.video.register("go2rtc", ident, provider)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident):
        provider = self.providers[ident]
        try:
            data = await provider.inspect()
            if ident in self.controllers:
                data["pan_tilt"] = self.controllers[ident].state()
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
