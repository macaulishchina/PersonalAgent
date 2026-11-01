"""Pi MCP configuration and discovery. No model turns or device actions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys

from assistant_runtime.core.plugin_catalog import services
from assistant_runtime.core.pi.mcp import server_config


class PiIntegrations:
    def __init__(self, backend, registry):
        self.backend, self.registry = backend, registry
        from assistant_runtime.api.configuration_io import configuration_path

        frozen = configuration_path(registry.path)
        self.runtime_data = None
        if frozen != registry.path:
            from assistant_runtime.core.integrations.configuration import validate_registry

            self.runtime_data = (
                validate_registry(json.loads(frozen.read_text(encoding="utf-8")))
                if frozen.exists()
                else {"revision": 0, "servers": {}, "removed": []}
            )
        self.inherited = {}
        self.applied_revision = None
        self.applied_configuration = None
        self.failed_revision = None
        self.last_error = None
        self.probes = set()
        self.logins = {}
        self.context = None

    async def start(self):
        result = await self.backend.client.request(
            "configuration.read", {"includeLayers": False, "cwd": self.backend.config.workspace}
        )
        self.inherited = result["config"].get("mcp_servers", {})

    async def capture(
        self,
        context,
        task_run=None,
        *,
        cwd=None,
        probe=None,
        manage_integrations=False,
        probe_definition=None,
    ):
        result = await self.backend.client.request(
            "configuration.read", {"includeLayers": False, "cwd": str(cwd or self.backend.config.workspace)}
        )
        return self.configuration(
            context,
            task_run,
            probe=probe,
            probe_definition=probe_definition,
            manage_integrations=manage_integrations,
            inherited=result["config"].get("mcp_servers", {}),
        )

    def credential_environment(self, spec):
        from assistant_runtime.core.models.credentials import credential

        transport = spec["transport"]
        refs = (
            transport.get("env_vars", [])
            if transport["type"] == "stdio"
            else [
                *transport.get("env_headers", {}).values(),
                *([transport["bearer_token_env"]] if transport.get("bearer_token_env") else []),
            ]
        )
        values = {}
        for key in refs:
            try:
                values[key] = credential(self.registry.root, key)
            except ValueError:
                pass
        return refs, values

    def configuration(
        self,
        context,
        task_run=None,
        *,
        probe=None,
        manage_integrations=False,
        inherited=None,
        probe_definition=None,
    ):
        cfg = self.backend.config
        disabled = context == "maintenance" or probe is not None
        result = {
            ident: {"enabled": False}
            for ident in (self.inherited if inherited is None else inherited)
            if disabled or not self.registry.settings.inherit_user_servers
        }
        result.update(
            server_config(
                cfg.socket,
                enabled=not disabled,
                task_run=task_run,
                manage_integrations=manage_integrations,
                plugins=cfg.enabled_plugins,
            )
        )
        data = (
            self.registry.snapshot() if probe is not None or self.runtime_data is None else self.runtime_data
        )
        if probe_definition is not None:
            data = {"revision": 0, "removed": [], "servers": {probe: {"definition": probe_definition}}}
        for ident in data["removed"]:
            result["external_" + ident] = {
                "command": sys.executable,
                "args": ["-c", "pass"],
                "enabled": False,
            }
        for ident, row in data["servers"].items():
            spec, transport = row["definition"], copy.deepcopy(row["definition"]["transport"])
            _, environment = self.credential_environment(spec)
            client = getattr(self.backend, "client", None)
            if client is not None and hasattr(client, "environment"):
                client.environment.update(environment)
            kind = transport.pop("type")
            if kind == "stdio":
                directory = Path(transport.get("cwd") or self.registry.work / ident)
                if not transport.get("cwd"):
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                transport["cwd"] = str(directory)
            else:
                transport = {
                    "url": transport["url"],
                    "http_headers": transport["headers"],
                    "env_http_headers": transport["env_headers"],
                    **(
                        {"bearer_token_env_var": transport["bearer_token_env"]}
                        if transport.get("bearer_token_env")
                        else {}
                    ),
                }
            available = spec["enabled"] and context in spec["contexts"]
            result["external_" + ident] = {
                **transport,
                "enabled": ident == probe if probe is not None else (available and context != "maintenance"),
                "startup_timeout_sec": spec["startup_timeout_seconds"],
                "tool_timeout_sec": spec["tool_timeout_seconds"],
                "default_tools_approval_mode": spec["approval"],
                "disabled_tools": sorted(
                    set(spec["deny_tools"])
                    | {tool for tool, policy in spec["tool_approvals"].items() if policy == "reject"}
                ),
                "tools": {
                    tool: {"approval_mode": policy}
                    for tool, policy in spec["tool_approvals"].items()
                    if policy != "reject"
                },
                **(
                    {"enabled_tools": []}
                    if spec["approval"] == "reject"
                    else {"enabled_tools": spec["allow_tools"]}
                    if spec["allow_tools"] is not None
                    else {}
                ),
            }
        return result

    def status(self):
        return {
            "registry_revision": self.registry.revision,
            "foreground_applied_revision": self.applied_revision,
            "foreground_pending": self.applied_revision != self.registry.revision,
            "failed_revision": self.failed_revision,
            "last_apply_error": self.last_error,
            "background_policy": "restart_required"
            if self.runtime_data is not None
            else "snapshot_at_execution_start",
            "inherited_user_servers": sorted(
                set(self.inherited) - services(self.backend.config.enabled_plugins).keys() - {"device"}
            ),
            "inherit_user_servers": self.registry.settings.inherit_user_servers,
        }

    def require_confirmed(self):
        if self.applied_revision is None:
            raise RuntimeError("MCP provider context is unconfirmed; no new input started")

    async def apply(self, force=False, *, staged=False):
        backend = self.backend
        async with backend.submit_lock:
            if not staged and self.runtime_data is not None and self.applied_revision is not None:
                return {
                    "state": "restart_required"
                    if self.applied_revision != self.registry.revision
                    else "applied",
                    **self.status(),
                }
            if not force and self.failed_revision == self.registry.revision:
                return {
                    "state": "failed",
                    "using_previous": self.applied_revision is not None
                    and self.applied_configuration is not None,
                    "error": self.last_error,
                    **self.status(),
                }
            if not force and self.applied_revision == self.registry.revision:
                return {"state": "applied", **self.status()}
            if backend.busy or backend.active_turn or backend.voice_active():
                return {"state": "deferred", "reason": "foreground_or_voice_active", **self.status()}
            config = await self.capture("foreground")
            revision = (
                self.runtime_data["revision"] if self.runtime_data is not None else self.registry.revision
            )
            summary = (
                await backend.client.request(
                    "session.read", {"session_id": backend.execution_session_id, "include_runs": False}
                )
            )["session"]
            if summary.get("status", {}).get("type") == "active":
                return {"state": "deferred", "reason": "provider_active", **self.status()}

            async def resume(configuration):
                await backend.client.request("session.close", {"session_id": backend.execution_session_id})
                return await backend.client.request(
                    "session.resume",
                    {
                        "session_id": backend.execution_session_id,
                        "exclude_runs": True,
                        "cwd": backend.config.workspace,
                        "model": backend.config.main_model,
                        "config": {
                            "model_reasoning_effort": backend.config.main_reasoning_effort,
                            "mcp_servers": configuration,
                        },
                    },
                    timeout=60,
                )

            async def refresh():
                try:
                    await resume(config)
                except Exception as exc:
                    self.failed_revision, self.last_error = revision, type(exc).__name__
                    if self.applied_configuration is None:
                        raise RuntimeError(
                            "MCP configuration refresh failed; provider context unconfirmed"
                        ) from None
                    try:
                        await resume(self.applied_configuration)
                    except Exception:
                        self.applied_revision = None
                        raise RuntimeError(
                            "MCP refresh and rollback unconfirmed; no input replayed"
                        ) from None
                    return {
                        "state": "failed",
                        "using_previous": True,
                        "error": self.last_error,
                        **self.status(),
                    }
                self.applied_revision = revision
                self.applied_configuration = config
                self.failed_revision = self.last_error = None
                return {"state": "applied", **self.status()}

            transaction = asyncio.create_task(refresh())
            # This owned child completes the same configuration transaction. It
            # must cross the caller's barrier; unrelated requests remain paused.
            authorized = getattr(backend.client, "configuration_tasks", set())
            if asyncio.current_task() in authorized:
                authorized.add(transaction)
            try:
                return await asyncio.shield(transaction)
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.gather(transaction, return_exceptions=True))
                raise
            finally:
                authorized.discard(transaction)

    async def open_probe(self, ident, context, probe_definition=None):
        self.registry.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = await self.capture(
            context,
            cwd=self.registry.work,
            probe=ident,
            **({"probe_definition": probe_definition} if probe_definition is not None else {}),
        )
        opening = asyncio.create_task(
            self.backend.client.request(
                "session.create",
                {
                    "ephemeral": True,
                    "model": self.backend.config.main_model,
                    "cwd": str(self.registry.work),
                    "config": {"mcp_servers": config},
                },
                timeout=60,
            )
        )
        try:
            result = await asyncio.shield(opening)
        except asyncio.CancelledError:

            async def drain():
                try:
                    reply = await opening
                    await self.close_probe(reply["session"]["id"])
                except Exception:
                    pass

            if self.context:
                self.context.spawn(drain(), "mcp-opening-cleanup")
            else:
                await drain()
            raise
        thread = result["session"]["id"]
        self.probes.add(thread)
        return thread

    async def close_probe(self, thread):
        try:
            await self.backend.client.request("session.close", {"session_id": thread}, timeout=10)
        finally:
            self.probes.discard(thread)

    async def verify(self, ident, context, schemas=False, *, draft=None):
        spec = draft if draft is not None else self.registry.get(ident)["definition"]
        if context not in spec["contexts"]:
            raise ValueError("MCP server is excluded from this context")
        transport = spec["transport"]
        refs, environment = self.credential_environment(spec)
        missing_env = [key for key in refs if key not in transport.get("env", {}) and key not in environment]
        if missing_env:
            return {
                "state": "unavailable",
                "reason": "missing_service_environment",
                "missing_environment": missing_env,
            }
        if transport["type"] == "stdio":
            directory = Path(transport.get("cwd") or self.registry.work / ident)
            if transport.get("cwd") and not directory.is_dir():
                return {"state": "unavailable", "reason": "working_directory_missing"}
            command = transport["command"]
            if os.path.dirname(command) and not Path(command).is_absolute():
                command = str(directory / command)
            if not shutil.which(command, path=transport["env"].get("PATH", os.environ.get("PATH"))):
                return {"state": "unavailable", "reason": "command_not_found"}
        thread = await self.open_probe(
            ident, context, **({"probe_definition": spec} if draft is not None else {})
        )
        try:
            result = await self.backend.client.request(
                "mcp.inspect",
                {"session_id": thread, "serverName": "external_" + ident, "detail": "toolsAndAuthOnly"},
                timeout=spec["startup_timeout_seconds"] + 10,
            )
            rows = [row for row in result["data"] if row["name"] == "external_" + ident]
            if not rows:
                return {"state": "unavailable", "reason": "server_not_discovered"}
            row = rows[0]
            tools = row.get("tools", {})
            missing = sorted(set(spec["allow_tools"] or []) - tools.keys())
            state = (
                "verified"
                if row.get("runtimeStatus") == "connected" and not row.get("toolsError") and not missing
                else "unavailable"
            )
            if row.get("authStatus") == "notLoggedIn":
                state = "auth_required"
            summaries = []
            for tool, data in list(tools.items())[:200]:
                from assistant_runtime.core.integrations.configuration import tool_permission

                permission = tool_permission(spec, tool, context)
                visible = permission == "allowed"
                summaries.append(
                    {
                        "name": tool,
                        "description": (data.get("description") or "")[:300],
                        "enabled_by_policy": visible,
                        "permission": permission,
                        "annotations": {
                            k: v
                            for k, v in (data.get("annotations") or {}).items()
                            if k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
                            and type(v) is bool
                        },
                        **({"input_schema": data.get("inputSchema")} if schemas else {}),
                    }
                )
            reply = {
                "state": state,
                "validation": "initialize_and_discover_no_tool_execution",
                "context": context,
                "configured_enabled": spec["enabled"],
                "runtime_status": row.get("runtimeStatus"),
                "auth_status": row.get("authStatus"),
                "tools_error": bool(row.get("toolsError")),
                "missing_allowed_tools": missing,
                "tools_count": len(tools),
                "tools": summaries,
                "truncated": len(tools) > 200,
            }
            if len(json.dumps(reply).encode()) > 1024 * 1024:
                raise ValueError("MCP tool schema inventory exceeds 1 MiB; verify without schemas")
            return reply
        finally:
            await self.close_probe(thread)

    async def login(self, ident):
        if self.registry.get(ident)["definition"]["transport"]["type"] != "http":
            raise ValueError("OAuth login requires an HTTP MCP transport")
        runtime_name = "external_" + ident
        if runtime_name in self.logins:
            raise ValueError("MCP login already in progress")
        if len(self.logins) >= 4 or self.context is None:
            raise RuntimeError("MCP login owner unavailable or busy")
        future = asyncio.get_running_loop().create_future()
        self.logins[runtime_name] = future
        thread = None
        try:
            thread = await self.open_probe(ident, "foreground")
            result = await self.backend.client.request(
                "mcp.login",
                {"name": runtime_name, "session_id": thread, "timeoutSecs": 300},
                timeout=30,
            )
        except BaseException:
            self.logins.pop(runtime_name, None)
            future.cancel()
            if thread is not None:
                await self.close_probe(thread)
            raise

        async def finish():
            try:
                await asyncio.wait_for(future, 310)
            except TimeoutError:
                pass
            finally:
                self.logins.pop(runtime_name, None)
                try:
                    await self.close_probe(thread)
                except Exception:
                    pass

        self.context.spawn(finish(), "mcp-login")
        return {
            "state": "user_action_required",
            "authorization_url": result["authorizationUrl"],
            "next_step": "Open this URL to authorize, then verify and apply the server configuration",
        }

    async def event(self, event):
        params = event.get("data", {})
        if event.get("kind") == "mcp.login_completed":
            future = self.logins.get(params.get("name"))
            if future and not future.done():
                future.set_result(bool(params.get("success")))
            return True
        if params.get("session_id") in self.probes:
            if "id" in event:
                await self.backend.client.respond(event["id"], {"action": "decline"})
            return True
        return False
