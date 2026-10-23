"""Explicit voice placeholder. Never captures audio or reports successful playback."""

import asyncio


class DisabledVoice:
    available = False
    opened = False
    closing = True
    epoch = 0
    speech_completed = False
    output_frames = 0
    last_error = None
    pending_execution = None
    on_conversation = None

    def __init__(self):
        self.ready = asyncio.Event()

    async def open(self, *args, **kwargs):
        raise RuntimeError("Voice is not enabled; use the web conversation")

    async def say(self, *args, **kwargs):
        raise RuntimeError("Speech playback is not enabled")

    async def close(self):
        return

    def feed(self, pcm):
        raise RuntimeError("Voice capture is not enabled")

    def end_interim(self):
        return

    def execution_started(self, ident):
        self.pending_execution = ident

    def finish_execution(self, ident):
        if self.pending_execution == ident:
            self.pending_execution = None
