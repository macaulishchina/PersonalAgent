"""The native voice and task thread have one owner; provider details stop here."""

from ...kernel.runtime import Manifest
from ...kernel.contracts import AudioFrame
from .backend import CodexBackend
from .native import NativeVoice


class CodexPlugin:
    manifest = Manifest("codex", ("devices",), critical=True)

    def __init__(self, config, store, on_audio, on_event, speak, control, mcp_registry=None):
        self.backend = CodexBackend(config, store, speak, control, mcp_registry)
        self.voice = NativeVoice(
            config, self.backend, lambda pcm: on_audio(AudioFrame(pcm, 24000, self.voice.epoch)), on_event
        )
        self.backend.on_realtime = self.voice.notification
        self.backend.voice_active = lambda: self.voice.opened

    async def start(self, context):
        if self.backend.integrations:
            self.backend.integrations.context = context
        context.own(self.backend.close)
        context.own(self.voice.close)
        await self.backend.start()
        if (await self.backend.account()).get("type") != "chatgpt":
            raise RuntimeError("Existing ChatGPT subscription login required; API key disabled")

        async def watch():
            await self.backend.events_task

        context.spawn(watch(), "provider-events")

    def diagnostics(self):
        return {
            "backend": "native",
            "codex_model": self.backend.resolved_model,
            "codex_reasoning_effort": self.backend.resolved_reasoning_effort,
            "codex_requested_model": self.backend.config.codex_model,
            "codex_requested_reasoning_effort": self.backend.config.codex_reasoning_effort,
            "realtime_voice": self.voice.config.realtime_voice,
            "voice_auth": "chatgpt",
            "transport": "webrtc",
            "webrtc_engine": "chromium",
            "voice_connect_ms": self.voice.open_timing_ms,
            "native_output_frames": self.voice.output_frames,
            "rtp_inbound": [
                {
                    k: s.get(k)
                    for k in (
                        "packetsReceived",
                        "packetsLost",
                        "concealedSamples",
                        "silentConcealedSamples",
                        "totalSamplesReceived",
                        "packetsDiscarded",
                    )
                }
                for s in self.voice.rtp_stats
                if s.get("type") == "inbound-rtp"
            ],
        }
