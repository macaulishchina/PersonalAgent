"""PipeWire device family: connection validation, stable selection and snapshots."""

import re
import time

from assistant_runtime.api.contracts import DeviceObservation
from .volume import hardware_volume

MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def validate_device(spec):
    connection = spec["connection"]
    kind = spec["kind"]
    keys = set(connection) - {"hardware_volume"}
    if kind == "microphone":
        valid = keys == {"match"}
    elif kind == "speaker":
        valid = keys in ({"address"}, {"sink"})
    else:
        raise ValueError(f"PipeWire does not support device kind: {kind}")
    if not valid or any(not isinstance(connection[k], str) or not connection[k].strip() for k in keys):
        raise ValueError(f"Invalid PipeWire connection for {kind}")
    if "address" in connection and not MAC.fullmatch(connection["address"]):
        raise ValueError("Bluetooth speaker address must be a MAC")
    hardware_volume(connection)


def resolve(spec, sources, sinks):
    connection = spec["connection"]
    if spec["kind"] == "microphone":
        matches = [
            s for s in sources if connection["match"] in s["name"] and not s["name"].endswith(".monitor")
        ]
    else:
        address = connection.get("address")
        prefix = f"bluez_output.{address.upper().replace(':', '_')}." if address else None
        matches = [
            s
            for s in sinks
            if prefix and s["name"].startswith(prefix) or not prefix and s["name"] == connection["sink"]
        ]
    if len(matches) > 1:
        raise ValueError("Ambiguous hardware identity: refine the configured connection")
    return matches[0] if matches else None


class PipeWireDevices:
    def __init__(self, config, route, directory):
        self.config, self.route, self.directory = config, route, directory
        self.specs = {
            i: s for i, s in config.devices.items() if s["adapter"] == "pipewire" and s.get("enabled", True)
        }
        for spec in self.specs.values():
            validate_device(spec)

    def register(self):
        for ident, spec in self.specs.items():
            capabilities = (
                ("audio.capture",)
                if spec["kind"] == "microphone"
                else ("audio.playback", "audio.volume", "audio.stop")
            )
            self.directory.register("pipewire", ident, capabilities, self.refresh)

    async def refresh(self):
        sources, sinks = await self.route.devices()
        for ident, spec in self.specs.items():
            data = {
                "voice_bound": ident
                == (self.route.input_device if spec["kind"] == "microphone" else self.route.output_device)
            }
            try:
                node = resolve(spec, sources, sinks)
                if node:
                    data.update(node_state=node.get("state"), muted=node.get("mute"))
                    if spec["kind"] == "microphone":
                        p = self.route.capture
                        data["capturing"] = data["voice_bound"] and p is not None and p.healthy
                    else:
                        values = [v.get("value") for v in node.get("volume", {}).values()]
                        data["volume_percent"] = (
                            round(sum(values) / len(values) * 100 / 65536)
                            if values and all(type(v) is int and v >= 0 for v in values)
                            else None
                        )
                        data["route_ready"] = data["voice_bound"] and self.route.ready.is_set()
                observation = DeviceObservation(
                    "available" if node else "unavailable",
                    data,
                    time.time(),
                    None if node else "Configured hardware not present",
                )
            except ValueError as exc:
                observation = DeviceObservation("error", data, time.time(), str(exc))
            self.directory.observe("pipewire", ident, observation)

    async def run(self, context):
        import asyncio

        while True:
            try:
                await self.refresh()
                context.set_health("ready")
            except (RuntimeError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
                context.set_health("degraded", str(exc)[:200])
                for ident in self.specs:
                    self.directory.observe(
                        "pipewire", ident, DeviceObservation("error", error=str(exc)[:200])
                    )
            await asyncio.sleep(3)
