"""Optional assistant interaction state, separate from the generic application host."""

import asyncio
import time
from collections import deque
from .lifecycle import VoiceLifecycle
from .quiet import QuietHours
from .dialogue import Dialogue
from .interaction.input import InputBuffer


class AssistantState(VoiceLifecycle):
    def __init__(self, host):
        from .disabled import DisabledVoice

        self.host = host
        self.voice = DisabledVoice()
        self.interaction = None
        config = host.config
        self.quiet_hours = QuietHours.from_settings(config.quiet_hours)
        self.events = asyncio.Queue(maxsize=256)
        self.active = False
        self.mode = "standby"
        self.last_activity = time.monotonic()
        self.last_frame = None
        self.last_input = ""
        self.input_count = self.wake_count = 0
        self.last_error = None
        self.connect_task = None
        self.preroll = deque(maxlen=50)  # approximately one second, memory only
        self.input_buffer = InputBuffer(config.interaction.get("input_buffer_seconds", 30))
        self.native_pending = deque(maxlen=1500)
        self.capture_sequence = 0
        self.tasks = []
        self.transition = asyncio.Lock()
        self.standby_ready = asyncio.Event()
        self.standby_ready.set()
        self.notification_output = False
        self.last_wake = None
        self.last_output = None
        self.recovering = False
        self.input_rms = 0
        self.native_interruptions = 0
        self.last_speech = 0.0
        self.dialogue = Dialogue(config.voice_follow_up_seconds)
        self.reconnect_task = None
        self.voice_decision_task = None
        from assistant_runtime.core.speech.interaction.voice_control import VoiceControl

        self.voice_control = VoiceControl(self)

    @property
    def config(self):
        return self.host.config

    @property
    def store(self):
        return self.host.store

    @property
    def runtime(self):
        return self.host.runtime

    @property
    def backend(self):
        return self.host.backend

    @property
    def workfiles(self):
        return self.host.workfiles

    @property
    def ai_usage(self):
        return self.host.ai_usage

    @property
    def conversation(self):
        return self.host.conversation

    @property
    def memory(self):
        return self.host.memory

    @property
    def brain_plugin(self):
        return self.host.brain_plugin

    @property
    def audio_plugin(self):
        return self.host.audio_plugin

    @property
    def devices(self):
        return self.host.devices

    @property
    def no_mic(self):
        return self.host.no_mic

    @property
    def started(self):
        return self.host.started

    @property
    def notifications(self):
        return self.host.notifications

    def status(self):
        return {
            "assistant_name": self.config.assistant_name,
            "interaction": {
                "enabled": bool(getattr(self, "interaction", None)),
                "ready": bool(getattr(self, "interaction", None) and self.interaction.ready),
                "error": self.interaction.error if getattr(self, "interaction", None) else None,
                "input_gaps": self.input_buffer.gaps,
                "path": self.interaction.path if getattr(self, "interaction", None) else "disabled",
                "trace": list(self.interaction.state.trace)[-16:]
                if getattr(self, "interaction", None)
                else [],
                "stages": dict(self.interaction.state.current.stages)
                if getattr(self, "interaction", None) and self.interaction.state.current
                else {},
            },
            "wake_words": self.config.wake_words,
            "quiet_hours": {"settings": self.config.quiet_hours, "active": self.quiet_active()},
            "wake_detector": self.recognizer.diagnostics(),
            "service": "running",
            "plugins": self.runtime.health(),
            "devices": self.devices.list(),
            "memory": {
                "ready": bool(self.memory and self.memory.ready),
                "requested_model": self.config.model_api.get("memory"),
                "pending_jobs": self.store.memory.pending_count(),
                "cached_facts": len(self.memory.cache) if self.memory else 0,
            },
            "session_id": getattr(self.backend, "session_id", None),
            **self.brain_plugin.diagnostics(),
            "voice": {"state": "disabled", "reason": "pending_independent_acceptance"},
            **(self.audio_plugin.diagnostics() if self.audio_plugin else {"audio_ready": False}),
            "mode": self.mode,
            "voice_dialogue": {
                "mode": "continuous" if self.dialogue.continuous else "short",
                "follow_up_seconds": self.dialogue.follow_up_seconds,
            },
            "microphone_enabled": not self.no_mic,
            "capture_recent": self.last_frame is not None and time.monotonic() - self.last_frame < 3,
            "voice_io": {
                "input_device": self.config.input_device,
                "output_device": self.config.default_output,
                "locked": True,
            },
            "volume": self.route.volume if self.route else None,
            "task_running": self.backend.busy,
            "task_progress": self.backend.progress,
            "speaking": self.player.playing if self.player else False,
            "pending_speech": self.player.pending_voice_frames if self.player else 0,
            "input_count": self.input_count,
            "native_user_turns": self.native_interruptions,
            "last_input": self.last_input,
            "wake_count": self.wake_count,
            "input_rms": self.input_rms,
            "error": self.last_error
            or (self.route.last_error if self.route else None)
            or (self.player.last_error if self.player else None)
            or self.voice.last_error
            or self.backend.last_error,
            "uptime_seconds": round(time.time() - self.started),
        }
