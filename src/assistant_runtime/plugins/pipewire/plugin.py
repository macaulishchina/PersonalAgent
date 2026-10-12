from ...kernel.runtime import Manifest
from .audio import AudioRoute
from .stream_audio import StreamPlayer


class PipeWirePlugin:
    manifest = Manifest("pipewire")

    def __init__(self, config, store):
        self.route = AudioRoute(config, store)
        self.player = StreamPlayer(self.route)

    async def start(self, context):
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
            "software_stop_seconds": self.player.last_stop_seconds,
        }
