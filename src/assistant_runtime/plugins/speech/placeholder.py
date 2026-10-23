"""Unavailable voice entrance, without loading wake or recognition models."""

from ...kernel.runtime import Manifest


class VoicePlaceholder:
    manifest = Manifest("voice_disabled")

    def __init__(self):
        self.recognizer = self

    async def start(self, context):
        pass

    def diagnostics(self):
        return {"state": "disabled", "reason": "pending_independent_acceptance"}

    def reset_keyword(self):
        pass
