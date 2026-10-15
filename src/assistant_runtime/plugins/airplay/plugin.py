"""AirPlay speaker family: standard inventory registration and PCM providers."""

import asyncio

from ...kernel.contracts import DeviceObservation
from ...kernel.runtime import Manifest
from .output import AirPlayOutput


class AirPlayPlugin:
    manifest = Manifest("airplay")

    def __init__(self, config, directory, outputs, voice_io):
        self.directory, self.outputs = directory, outputs
        self.voice_io = voice_io
        self.providers = {}
        for ident, spec in config.devices.items():
            if spec["adapter"] == "airplay" and spec.get("enabled", True):
                if spec["kind"] != "speaker":
                    raise ValueError("AirPlay adapter only supports speaker outputs")
                self.providers[ident] = AirPlayOutput(spec)

    async def start(self, context):
        self.context = context
        for ident, provider in self.providers.items():

            async def probe(ident=ident):
                await self.observe(ident, force=True)

            self.directory.register("airplay", ident, ("audio.playback", "audio.volume", "audio.stop"), probe)
            self.outputs.register("airplay", ident, provider)
            context.own(provider.close)
        context.spawn(self.watch(), "device-observer")

    async def observe(self, ident, force=False):
        provider = self.providers[ident]
        try:
            cfg = await provider.discover(force=force)
            connection = provider.connection
            data = {
                "receiver_name": cfg.name,
                "voice_bound": self.voice_io.output_device == ident,
                "stream_open": bool(connection and connection.healthy),
                "volume_percent": provider.volume,
                "volume_source": "last_explicit_request" if provider.volume is not None else "not_readable",
                "encoder_queue_ms": connection.queue.qsize() * 20 if connection else 0,
                "remote_buffering": True,
                "stop_semantics": "cancel stream and close receiver session",
            }
            self.directory.observe(
                "airplay",
                ident,
                DeviceObservation(
                    "error" if provider.last_error else "available", data, error=provider.last_error
                ),
            )
        except Exception as exc:
            self.directory.observe("airplay", ident, DeviceObservation("unavailable", error=str(exc)[:200]))

    async def watch(self):
        while True:
            for ident in self.providers:
                await self.observe(ident)
                provider = self.providers[ident]
                connection = provider.connection
                if connection and not connection.healthy:
                    await connection.close()
                    if provider.connection is connection:
                        provider.connection = None
            await asyncio.sleep(15)
