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

    async def complete(
        self, payload, *, session_id=None, usage=None, scope=None, service="inference", bounds=None
    ):
        from assistant_runtime.core.pi.inference import complete
        import asyncio

        meter = None
        if usage is not None:
            from assistant_runtime.core.pi.attempts import AttemptMeter

            entry = self.settings.entry(payload["model"])
            meter = AttemptMeter(
                usage,
                scope,
                service=service,
                provider=entry["provider"],
                model=entry["model"],
                bounds=bounds,
                body=payload,
                details={"model_alias": payload["model"], "reasoning_effort": payload["reasoning"]["effort"]},
            )
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                return await complete(self.accounts, payload, session_id, meter=meter)
        except Exception as exc:
            from assistant_runtime.api.models import ModelCallError

            if isinstance(exc, ModelCallError):
                entry = self.settings.entry(payload["model"])
                exc.failure.update(model=payload["model"], provider=entry["provider"])
            raise
        finally:
            if meter:
                await meter.finish()

    async def output_bound(self, payload):
        from assistant_runtime.core.pi.inference import output_bound

        return await output_bound(self.accounts, payload)


async def metered_complete(client, payload, *, session_id, usage, scope, service, provider, model, bounds):
    """Native client meters every attempt; injected one-shot clients keep their existing gate."""
    if isinstance(client, ModelClient):
        return await client.complete(
            payload, session_id=session_id, usage=usage, scope=scope, service=service, bounds=bounds
        )
    from assistant_runtime.api.ai_usage import metered

    return await metered(
        usage,
        scope,
        service,
        provider,
        model,
        lambda: client.complete(payload, session_id=session_id),
        bounds=bounds,
        body=payload,
    )
