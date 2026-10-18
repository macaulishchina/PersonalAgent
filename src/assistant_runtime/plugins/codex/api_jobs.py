"""Reuse CodexJobs in an isolated API process; never change the voice process login."""

import asyncio
import json
import logging
from types import SimpleNamespace

from .jobs import CodexJobs
from .api_catalog import write_catalog
from .rpc import CodexClient
from .integrations import CodexIntegrations
from ..models.settings import ModelSettings
from ..models.gateway import gateway_credential

log = logging.getLogger(__name__)


class ApiJobs(CodexJobs):
    def __init__(self, backend, notify, settings):
        super().__init__(backend, notify)
        self.settings = settings

    async def models(self, refresh=False):
        return [m for m in self.settings.catalog() if m["available"] and m["execution_validated"]]


class RoutedJobs:
    def __init__(self, host, notify):
        self.host, self.notify = host, notify
        self.settings = ModelSettings.parse(host.config.model_api)
        self.subscription = CodexJobs(host.backend, notify)
        self.api = None
        self.context = host.runtime.contexts["tasks"]
        self.lock = asyncio.Lock()
        self.api_failed = False

    async def api_jobs(self):
        async with self.lock:
            if self.api_failed:
                raise RuntimeError("API executor unavailable; restart required, no automatic replay")
            if self.api:
                return self.api
            cfg = self.host.config
            key = gateway_credential(cfg.root, self.settings)
            provider = {
                "name": "Assistant API",
                "base_url": self.settings.gateway_url,
                "wire_api": "responses",
                "env_key": "ASSISTANT_CODEX_GATEWAY_KEY",
                "requires_openai_auth": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
                "stream_idle_timeout_ms": self.settings.timeout_seconds * 1000,
            }
            client = CodexClient(
                cfg.codex_binary,
                cfg.workspace,
                overrides={
                    "model_provider": "assistant_api",
                    "model_catalog_json": write_catalog(cfg.state_dir / "model-api", self.settings),
                    "model_providers.assistant_api": provider,
                    "mcp_servers": {},
                    # A one-second optional grace can dispatch a cold API turn
                    # before its MCP tools exist. Use each server's bounded startup
                    # timeout; do not ask the model to compensate for missing tools.
                    "mcp_optional_startup_grace_ms": 0,
                    "features.remote_control": False,
                },
                environment={"ASSISTANT_CODEX_GATEWAY_KEY": key},
            )
            self.context.own(client.close)
            backend = SimpleNamespace(
                config=cfg,
                client=client,
                thread_id=None,
                model_provider="assistant_api",
                integrations=None,
                server_tasks=set(),
            )

            async def close_requests():
                tasks = list(backend.server_tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            self.context.own(close_requests)
            api = ApiJobs(backend, self.notify, self.settings)
            try:
                await client.start()
                if self.host.mcp_registry:
                    backend.integrations = CodexIntegrations(backend, self.host.mcp_registry)
                    backend.integrations.context = self.context
                    await backend.integrations.start()
                self.api = api
                self.context.spawn(self.events(), "api-model-events")
                return api
            except BaseException:
                self.api_failed = True
                await client.close()
                raise

    async def events(self):
        try:
            while True:
                event = await self.api.client.events.get()
                if event.get("method") == "local/disconnected":
                    raise RuntimeError("API executor disconnected")
                if self.api.backend.integrations and await self.api.backend.integrations.event(event):
                    continue
                handled = await self.api.event(event)
                if not handled and "id" in event:
                    await self.api.client.send(
                        {"id": event["id"], "error": {"code": -32601, "message": "Unknown API task request"}}
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.api_failed = True
            log.error("API executor event handling stopped: %s", type(exc).__name__)
            try:
                for ident in list(self.api.handles):
                    self.notify(
                        ident, "unknown", progress="API 执行连接已中断，未重放操作。", turn_active=False
                    )
            finally:
                # The event consumer can fail while the child is still alive.
                # Stop new native work; already-started tool outcomes remain unknown.
                await self.api.client.close()

    def is_api(self, model):
        return model in self.settings.entries

    async def models(self, refresh=False):
        existing = await self.subscription.models(refresh)
        configured = [m for m in self.settings.catalog() if m["available"] and m["execution_validated"]]
        if {m["model"] for m in existing} & {m["model"] for m in configured}:
            raise ValueError("API alias collides with subscription model ID")
        return existing + configured

    async def select(self, model, effort):
        if not self.is_api(model):
            return await self.subscription.select(model, effort)
        m = self.settings.entry(model)
        if not m.get("execution_validated") or effort not in m.get("reasoning_efforts", ["none"]):
            raise ValueError("API execution or reasoning effort has not been validated")
        return {"model": model, "reasoning_effort": effort}

    async def owner(self, run):
        binding = run["data"].get("execution_backend")
        if binding is None:
            binding = "api" if self.is_api(run["data"]["spec"]["execution"]["model"]) else "subscription"
        return await self.api_jobs() if binding == "api" else self.subscription

    async def launch(self, run, directory):
        model = run["data"]["spec"]["execution"]["model"]
        target = "api" if self.is_api(model) else "subscription"
        previous = run["data"].get("execution_backend", "subscription")
        upgrades = run["data"].get("model_upgrades", [])
        changed = bool(upgrades and upgrades[-1]["phase"] == "dispatching")
        if run["data"].get("thread_id") and changed and (target == "api" or previous == "api"):
            old = await self.owner(run)
            await old.upgrade_ready(run)
            checkpoint = upgrades[-1]["request"]["checkpoint"]
            old_thread = run["data"]["thread_id"]
            await old.finalize({**run, "state": "completed"})
            run = {
                **run,
                "data": {
                    **run["data"],
                    "thread_id": None,
                    "turn_id": None,
                    "continuation": "继续原授权任务。以下是已核对的交接检查点，保留操作标识，不重复执行已完成动作；未知结果先查询：\n"
                    + json.dumps(checkpoint, ensure_ascii=False),
                },
            }
            if (
                self.notify(
                    run["id"],
                    None,
                    thread_id=None,
                    turn_id=None,
                    previous_thread_id=old_thread,
                    execution_backend=target,
                )
                is False
            ):
                return
        elif run["data"].get("thread_id") and previous != target:
            raise ValueError("Cannot change executor of an existing run without a verified checkpoint")
        if (
            self.notify(
                run["id"],
                None,
                execution_backend=target,
                model_configuration_revision=self.settings.fingerprint() if target == "api" else None,
            )
            is False
        ):
            return
        executor = await self.api_jobs() if target == "api" else self.subscription
        await executor.launch(run, directory)

    async def event(self, event):
        return await self.subscription.event(event)

    async def upgrade_ready(self, run):
        await (await self.owner(run)).upgrade_ready(run)

    async def fallback_ready(self, run):
        await (await self.owner(run)).fallback_ready(run)

    async def detach(self, run):
        await (await self.owner(run)).detach(run)

    async def supplement(self, run, text):
        await (await self.owner(run)).supplement(run, text)

    async def stop(self, run, pause=False):
        await (await self.owner(run)).stop(run, pause)

    async def finalize(self, run):
        await (await self.owner(run)).finalize(run)

    async def reconcile(self, run):
        return await (await self.owner(run)).reconcile(run)
