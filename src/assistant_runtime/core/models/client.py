"""Bounded auxiliary inference using the shared Pi provider adapter."""


class ModelClient:
    def __init__(self, settings, *, accounts):
        self.settings, self.accounts = settings, accounts

    async def start(self):
        await self.accounts.validate_configuration()

    async def close(self):
        await self.accounts.close()

    async def health(self):
        await self.accounts.validate_configuration()
        return {"reachable": True, "scope": "local_adapter", "inference_tested": False}

    async def complete(self, payload, *, session_id=None):
        from assistant_runtime.core.pi.inference import complete
        import asyncio

        async with asyncio.timeout(self.settings.timeout_seconds):
            return await complete(self.accounts, payload, session_id)

    async def output_bound(self, payload):
        from assistant_runtime.core.pi.inference import output_bound

        return await output_bound(self.accounts, payload)
