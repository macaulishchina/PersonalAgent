"""Register gateway cameras through the shared device and video capabilities."""

from assistant_runtime.api.configuration import ConfigurationActionRejected, ConfigurationError
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

    def maintenance_pending(self):
        # Discovery only; finite device operations are owned by core/devices.
        return []

    async def configuration_action(self, name, arguments):
        if name != "restart_gateway":
            raise ValueError("未知的 go2rtc 管理操作")
        gateways = {item.base_url: item for item in self.providers.values()}
        if not gateways:
            raise ConfigurationActionRejected([], "no_gateways", "尚未配置已启用的 go2rtc 摄像头")
        if any(item.lock.locked() or item.recording for item in self.providers.values()) or any(
            control.active is not None or control.pending_stops for control in self.controllers.values()
        ):
            raise ConfigurationActionRejected(
                [], "camera_busy", "摄像头正在取图、录像或控制云台，请结束操作后再重启"
            )
        try:
            results = await asyncio.gather(
                *(item.read("/api/restart", 4096, method="POST", params={}) for item in gateways.values()),
                return_exceptions=True,
            )
        finally:
            for item in self.providers.values():
                item.invalidate()
        failures = [
            f"{item.device_id}：{str(result)[:100]}"
            for item, result in zip(gateways.values(), results)
            if isinstance(result, BaseException)
        ]
        if failures:
            message = (
                f"已向 {len(gateways) - len(failures)}/{len(gateways)} 个网关提交重启请求。"
                "未获确认：" + "；".join(failures)[:180] + "。请检查网关状态；不会自动重试。"
            )
            if len(failures) == len(gateways):
                raise ConfigurationError([], "outcome_unknown", message)
            return {"message": message}
        return {
            "message": f"已提交全部 {len(gateways)} 个 go2rtc 网关的重启请求。"
            "请稍后重新加载设备；此结果不代表小米授权或摄像头取流已恢复。"
        }

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
