"""Registered non-local PCM output providers; no device protocol implementation."""


class AudioOutputs:
    def __init__(self, devices):
        self.devices = devices
        self.providers = {}

    def register(self, owner, device_id, provider):
        spec = self.devices[device_id]
        if spec["adapter"] != owner or spec["kind"] != "speaker" or not spec.get("enabled", True):
            raise ValueError("Audio output owner/role mismatch")
        if device_id in self.providers:
            raise ValueError("Duplicate audio output provider")
        self.providers[device_id] = provider

    def get(self, device_id):
        if device_id not in self.providers:
            raise ValueError(f"No media output provider registered: {device_id}")
        return self.providers[device_id]
