"""Wake observation lifecycle. The assistant owns the business acceptance hook."""

import asyncio
import time
from assistant_runtime.api.observations import fields, schema


class WakeEvents:
    max_instances = 1
    description = schema({}, ["wake.accepted", "wake.connected", "wake.failed", "wake.cancelled"])

    def __init__(self, host):
        self.host = host

    def validate(self, config):
        fields(config, ())
        return dict(config)

    def create(self, reporter, config):
        return WakeSource(self.host, reporter)


class WakeSource:
    def __init__(self, host, reporter):
        self.host, self.reporter = host, reporter
        self.health = {"state": "starting"}

    async def run(self):
        self.host.wake_reporter = self.reporter
        try:
            while True:
                try:
                    await self.reporter.checkpoint()
                    self.health = {"state": "healthy", "last_success": time.time()}
                except Exception:
                    self.health = {"state": "degraded", "error": "recording_unavailable"}
                await asyncio.sleep(30)
        finally:
            if self.host.wake_reporter is self.reporter:
                self.host.wake_reporter = None

    async def verify(self):
        return {
            "connected": self.reporter.sink() is not None,
            "semantics_verified": False,
            "note": "Await a real accepted keyword wake",
        }
