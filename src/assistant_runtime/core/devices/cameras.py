"""Directional camera control routing; adapters own protocols and cleanup."""


class CameraControls:
    def __init__(self, devices):
        self.devices = devices
        self.providers = {}

    def register(self, owner, device_id, provider):
        spec = self.devices[device_id]
        if spec["adapter"] != owner or spec["kind"] != "camera" or not spec.get("enabled", True):
            raise ValueError("Camera control owner/role mismatch")
        if device_id in self.providers:
            raise ValueError("Duplicate camera control provider")
        self.providers[device_id] = provider

    async def move(self, request):
        provider = self.providers.get(request.device_id)
        if provider is None:
            raise ValueError(f"Camera does not support pan/tilt: {request.device_id}")
        result = await provider.move(request)
        if result.get("device_id") != request.device_id:
            raise RuntimeError("Camera control result identity mismatch")
        return result
