"""Generic MCP management, separate from device actions and provider protocols."""

import asyncio
import hashlib
import json
import time
from typing import Callable

from assistant_runtime.kernel.runtime import Manifest
from assistant_runtime.api.contracts import IntegrationRequest, IntegrationProvider, TaskRequest


def signature(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class IntegrationsPlugin:
    def __init__(self, host, registry, provider: Callable[[], IntegrationProvider]):
        self.host, self.registry = host, registry
        self.provider = provider
        self.manifest = Manifest("integrations", (host.config.bindings["brain"],))
        self.error = None
        self.probing = asyncio.Semaphore(2)
        self.pending = 0

    @property
    def adapter(self):
        adapter = self.provider()
        if adapter is None:
            raise RuntimeError("Reasoning provider has no MCP integration adapter")
        return adapter

    def maintenance_pending(self):
        return (
            [{"state": "running"}]
            if self.pending or getattr(self.adapter, "logins", {}) or getattr(self.adapter, "probes", set())
            else []
        )

    async def start(self, context):
        self.context = context
        context.register(IntegrationRequest, self.request, timeout=3660)
        context.spawn(self.refresh(), "apply-at-idle")

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationUpdate, ConfigurationPending
        from assistant_runtime.api.configuration_io import configuration_path
        from .settings import IntegrationSettings
        from .configuration import validate_registry
        from pathlib import Path

        settings = IntegrationSettings.parse(config.external_mcp)
        path = (
            Path(settings.registry_path)
            if settings.registry_path
            else config.root / "config/mcp/servers.json"
        )
        source = configuration_path(path)
        data = (
            validate_registry(json.loads(source.read_text(encoding="utf-8")))
            if source.exists()
            else {"revision": 0, "servers": {}, "removed": []}
        )
        backend = self.host.backend
        adapters = [b.integrations for b in [backend, *backend.conversations.values()] if b.integrations]
        if self.maintenance_pending() or any(
            b.busy or b.active_turn for b in [backend, *backend.conversations.values()]
        ):
            raise ConfigurationPending()
        previous = self.registry.settings, self.registry.path, self.registry.data
        old_runtime = [(adapter, adapter.runtime_data) for adapter in adapters]

        async def apply():
            for adapter in adapters:
                if adapter.backend.execution_session_id:
                    result = await adapter.apply(force=True, staged=True)
                    if result["state"] != "applied":
                        raise (
                            ConfigurationPending()
                            if result["state"] == "deferred"
                            else RuntimeError("MCP update failed")
                        )

        async def commit():
            self.registry.settings, self.registry.path, self.registry.data = settings, path, data
            for adapter in adapters:
                adapter.runtime_data = data
            await apply()

        async def rollback():
            self.registry.settings, self.registry.path, self.registry.data = previous
            for adapter, old in old_runtime:
                adapter.runtime_data = old
            await apply()

        return ConfigurationUpdate(commit, rollback)

    async def refresh(self):
        while True:
            try:
                if self.adapter.status()["foreground_pending"]:
                    result = await self.adapter.apply()
                    self.error = result.get("error") if result["state"] == "failed" else None
            except Exception as exc:
                self.error = type(exc).__name__
            await asyncio.sleep(2 if not self.error else 10)

    def entry(self, ident):
        item = self.registry.public(ident)
        checks = {}
        for scope in ("foreground", "background"):
            check = self.context.get(f"check.{ident}.{scope}")
            if check:
                checks[scope] = {
                    **{key: value for key, value in check.items() if key != "signature"},
                    "current": check.get("signature") == signature(self.registry.get(ident)),
                }
        return {**item, "verification": checks}

    async def request(self, request):
        action, args = request.action, request.arguments
        if not isinstance(args, dict):
            raise ValueError("MCP management arguments must be an object")
        can_manage = request.caller_run is None
        if request.caller_run:
            info = await self.host.runtime.invoke(
                TaskRequest("execution_info", {"run_ids": [request.caller_run]})
            )
            can_manage = info.get(request.caller_run, {}).get("mcp_management", False)
            if action not in {"list", "get", "verify"} and not can_manage:
                raise ValueError("MCP management requires an active task with the mcp_management resource")
        if action == "list":
            return {
                "revision": self.registry.revision,
                "servers": [self.entry(ident) for ident in self.registry.data["servers"]],
                "runtime": self.adapter.status(),
                "error": self.error,
            }
        if action == "get":
            return {
                "revision": self.registry.revision,
                "server": self.entry(args["name"]),
                "runtime": self.adapter.status(),
            }
        if action in {"configure", "enable", "remove", "reload"}:
            async with self.registry.lock:
                self.registry.reload()
                if action == "configure":
                    self.registry.configure(args["name"], args["changes"], args["revision"])
                elif action == "enable":
                    self.registry.configure(args["name"], {"enabled": args["enabled"]}, args["revision"])
                elif action == "remove":
                    self.registry.remove(args["name"], args["revision"])
                return {
                    "revision": self.registry.revision,
                    "state": "saved",
                    "application": "restart_required"
                    if getattr(self.adapter, "runtime_data", None) is not None
                    else "queued_for_idle_foreground_and_new_background_executions",
                    **({"server": self.entry(args["name"])} if action in {"configure", "enable"} else {}),
                }
        if action == "apply":
            return await self.adapter.apply(force=True)
        if action == "login":
            try:
                return await self.adapter.login(args["name"])
            except RuntimeError as exc:
                return {"state": "unavailable", "error": type(exc).__name__}
        if action == "verify":
            ident, scope = args["name"], args.get("context", "foreground")
            if scope not in {"foreground", "background"}:
                raise ValueError("Invalid MCP validation context")
            if request.caller_run and not can_manage and scope != "background":
                raise ValueError("Background tasks may only verify their own MCP context")
            if self.pending >= 4:
                raise RuntimeError("MCP validation is busy")
            row = self.registry.get(ident)
            schemas = args.get("include_schemas", False)
            if type(schemas) is not bool:
                raise ValueError("include_schemas must be boolean")
            self.pending += 1

            async def probe():
                async with self.probing:
                    try:
                        return await self.adapter.verify(ident, scope, schemas)
                    except (RuntimeError, OSError, TimeoutError, ValueError) as exc:
                        return {
                            "state": "unavailable",
                            "error": type(exc).__name__,
                            "validation": "discovery_failed",
                        }

            task = self.context.spawn(probe(), "verify-mcp")
            task.add_done_callback(lambda done: setattr(self, "pending", self.pending - 1))
            try:
                result = await asyncio.shield(task)
                check = {
                    "server_revision": row["revision"],
                    "signature": signature(row),
                    "checked_at": time.time(),
                    "state": result["state"],
                }
                self.context.set(f"check.{ident}.{scope}", check)
                return {
                    "name": ident,
                    "server_revision": row["revision"],
                    "current": self.registry.data["servers"].get(ident) == row,
                    **result,
                    "runtime": self.adapter.status(),
                }
            except asyncio.CancelledError:
                if not task.cancelling():
                    task.cancel()
                await asyncio.shield(asyncio.gather(task, return_exceptions=True))
                raise
        raise ValueError("Unknown MCP management action")

    def diagnostics(self):
        return {
            "revision": self.registry.revision,
            "servers": len(self.registry.data["servers"]),
            "pending_verifications": self.pending,
            "error": self.error,
        }
