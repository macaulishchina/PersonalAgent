from ...kernel.runtime import Manifest
from .audio import AudioRoute
from .stream_audio import StreamPlayer
from .devices import PipeWireDevices


class PipeWirePlugin:
    manifest = Manifest("pipewire")

    def __init__(self, config, store, directory, outputs, voice_io, inputs):
        self.outputs, self.inputs = outputs, inputs
        self.route = AudioRoute(config, store, outputs, voice_io, inputs)
        self.player = StreamPlayer(self.route)
        self.devices = PipeWireDevices(config, self.route, directory)

    async def start(self, context):
        self.devices.register()
        from .io import PipeWireEndpoint

        for ident, spec in self.devices.specs.items():
            registry = self.inputs if spec["kind"] == "microphone" else self.outputs
            registry.register(
                "pipewire",
                ident,
                PipeWireEndpoint(
                    spec, self.route.config.minimum_volume if ident == self.route.output_device else 0
                ),
                shared=True,
            )
        context.spawn(self.devices.run(context), "device-observer")
        context.own(self.route.close)
        context.own(self.player.stop)
        context.spawn(self.player.run(), "playback")

    def diagnostics(self):
        return {
            "audio_ready": self.route.ready.is_set(),
            "echo_cancel": bool(self.route.module),
            "echo_cancel_engine": "webrtc-aec3" if self.route.module else None,
            "playback_queue_ms": round(self.player.last_queue_ms, 1),
            "max_playback_queue_ms": round(self.player.max_queue_ms, 1),
            "written_audio_bytes": self.player.written_bytes,
            "discarded_audio_bytes": self.player.discarded_bytes,
            "playback_suppressed": self.player.suppressed,
            "playback_manual_muted": self.player.manual_muted,
            "local_input_speaking": self.player.input_speaking,
            "software_stop_seconds": self.player.last_stop_seconds,
        }
