"""Lifecycle placeholder; no device registration, tools, workers or I/O."""

from .declaration import DECLARATION


class AiPassportPlugin:
    manifest = DECLARATION.manifest

    async def start(self, context):
        context.set_health("degraded", "ai_passport_scaffold_only: 设备接入尚未实现")

    def diagnostics(self):
        return {"state": "unavailable", "reason": "ai_passport_scaffold_only"}
