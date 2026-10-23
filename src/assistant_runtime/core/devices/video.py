"""Camera provider registration and explicit one-frame routing; no protocol code."""


class VideoInputs:
    def __init__(self, devices):
        self.devices = devices
        self.providers = {}

    def register(self, owner, device_id, provider):
        spec = self.devices[device_id]
        if spec["adapter"] != owner or spec["kind"] != "camera" or not spec.get("enabled", True):
            raise ValueError("Video input owner/role mismatch")
        if device_id in self.providers:
            raise ValueError("Duplicate video input provider")
        self.providers[device_id] = provider

    async def capture(self, request):
        if request.device_id not in self.providers:
            raise ValueError(f"No video input provider registered: {request.device_id}")
        frame = await self.providers[request.device_id].capture()
        if frame.device_id != request.device_id:
            raise RuntimeError("Video frame identity mismatch")
        return frame

    async def record(self, request):
        if request.device_id not in self.providers:
            raise ValueError(f"No video input provider registered: {request.device_id}")
        clip = await self.providers[request.device_id].record(request.duration_seconds)
        if clip.device_id != request.device_id:
            raise RuntimeError("Recorded video identity mismatch")
        return clip
