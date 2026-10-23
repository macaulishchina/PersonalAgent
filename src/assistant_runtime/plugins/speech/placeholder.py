"""Unavailable voice entrance, without loading wake or recognition models."""

from .declaration import DECLARATION


class VoicePlaceholder:
    manifest = DECLARATION.manifest

    def __init__(self):
        self.recognizer = self

    async def start(self, context):
        pass

    def diagnostics(self):
        return {"state": "disabled", "reason": "pending_independent_acceptance"}

    def reset_keyword(self):
        pass
