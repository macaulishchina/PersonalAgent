"""Pi execution plugin. Speech is explicitly unavailable pending independent acceptance."""

from .declaration import DECLARATION

from assistant_runtime.core.pi.backend import PiBackend


class PiPlugin:
    manifest = DECLARATION.manifest

    def __init__(
        self,
        config,
        store,
        speak=None,
        control=None,
        mcp_registry=None,
        usage=None,
        *,
        skill_paths=(),
        foreground=True,
    ):
        self.backend = PiBackend(
            config, store, speak, control, mcp_registry, usage, skill_paths=skill_paths, foreground=foreground
        )

    async def start(self, context):
        if self.backend.integrations:
            self.backend.integrations.context = context
        context.own(self.backend.close)
        await self.backend.start()

        async def watch():
            await self.backend.events_task

        if self.backend.events_task is not None:
            context.spawn(watch(), "provider-events")

    def diagnostics(self):
        return {
            "backend": "pi",
            "execution_permissions": "full",
            "main_model": self.backend.resolved_model,
            "main_reasoning_effort": self.backend.resolved_reasoning_effort,
            "sandbox": {"state": "not_implemented", "todo": True},
        }

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationUpdate, ConfigurationPending
        from assistant_runtime.core.models.settings import ModelSettings
        from assistant_runtime.core.models.gateway import gateway_credential
        from assistant_runtime.core.pi.accounts import NativeAccounts
        import asyncio
        import aiohttp

        backend, client = self.backend, self.backend.client

        def busy():
            return (
                any(lock.locked() for lock in client.locks.values())
                or client.transport.pending
                or any(w.active_turn or w.compacting for w in client.workers.values())
            )

        if busy():
            raise ConfigurationPending()
        settings = ModelSettings.parse(config.model_api)
        previous = (
            client.settings,
            client.accounts,
            client.transport.transport,
            client.transport.slots,
            client.cwd,
            dict(client.environment),
            backend.config,
        )
        started = client.started
        key = gateway_credential(config.root, settings) if settings.enabled else None
        accounts = NativeAccounts(config.root, settings, binary=config.pi_binary)
        transport = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.timeout_seconds), trust_env=False
        )
        previous_skills = client.skill_paths
        previous_meta = {ident: dict(worker.meta) for ident, worker in client.workers.items()}

        # The composition root selects explicit paths; the executor only consumes them.
        skill_paths = getattr(config, "_prepared_skill_paths", client.skill_paths)

        async def commit():
            if busy():
                raise ConfigurationPending()
            for worker in client.workers.values():
                await worker.close()
            client.settings, client.accounts, client.cwd = settings, accounts, config.workspace
            client.skill_paths = skill_paths
            client.transport.transport = transport
            client.transport.slots = asyncio.Semaphore(settings.concurrency)
            if key:
                client.environment["ASSISTANT_MODEL_GATEWAY_KEY"] = key
            else:
                client.environment.pop("ASSISTANT_MODEL_GATEWAY_KEY", None)
            for ident, worker in client.workers.items():
                # Explicitly scoped task skills retain their original authorization.
                if worker.meta.get("skill_paths") == [str(p) for p in previous_skills]:
                    worker.meta["skill_paths"] = [str(p) for p in skill_paths]
                worker.meta["cwd"] = config.workspace
                client._save_meta(ident)
            backend.config = config
            for child in backend.conversations.values():
                child.config = config
            if settings.enabled and not started:
                await backend.start()

        async def rollback():
            if not started and client.started:
                await backend.close()
                client.started = False
            (
                client.settings,
                client.accounts,
                client.transport.transport,
                client.transport.slots,
                client.cwd,
                client.environment,
                backend.config,
            ) = previous
            client.skill_paths = previous_skills
            for ident, meta in previous_meta.items():
                client.workers[ident].meta = meta
                client._save_meta(ident)
            for child in backend.conversations.values():
                child.config = previous[-1]

        async def release(committed):
            old_accounts = previous[1] if committed else accounts
            old_transport = previous[2] if committed else transport
            if old_accounts:
                await old_accounts.close()
            if old_transport:
                await old_transport.close()
            if committed and not started:
                await transport.close()  # backend.start created its own transport.

        return ConfigurationUpdate(commit, rollback, release)
