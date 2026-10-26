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
        import secrets
        from assistant_runtime.core.pi.accounts import NativeAccounts
        import asyncio

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
            client.transport.slots,
            client.cwd,
            dict(client.environment),
            backend.config,
        )
        started = client.started
        key = secrets.token_urlsafe(32) if settings.enabled else None
        accounts = NativeAccounts(config.root, settings, binary=config.pi_binary)
        if settings.enabled:
            try:
                await accounts.validate_configuration()
            except BaseException:
                await accounts.close()
                raise
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
            client.transport.slots = asyncio.Semaphore(settings.concurrency)
            if key:
                client.environment["PA_EXECUTION_TOKEN"] = key
            else:
                client.environment.pop("PA_EXECUTION_TOKEN", None)
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
            if old_accounts:
                await old_accounts.close()

        return ConfigurationUpdate(commit, rollback, release)
