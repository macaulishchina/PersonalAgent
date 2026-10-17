"""Codex MCP configuration and discovery. No model turns or device actions."""

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys

from ...mcp.catalog import SERVICES
from .mcp import server_config


class CodexIntegrations:
    def __init__(self, backend, registry):
        self.backend, self.registry = backend, registry
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
            "config/read", {"includeLayers": False, "cwd": self.backend.config.workspace}
        )
        self.inherited = result["config"].get("mcp_servers", {})

    async def capture(self, context, task_run=None, *, cwd=None, probe=None, manage_integrations=False):
        # Config layering depends on cwd. Read that context's effective server
        # identities, including servers added outside this process since startup.
        result = await self.backend.client.request(
            "config/read", {"includeLayers": False, "cwd": str(cwd or self.backend.config.workspace)}
        )
        return self.configuration(
            context,
            task_run,
            probe=probe,
            manage_integrations=manage_integrations,
            inherited=result["config"].get("mcp_servers", {}),
        )

    def configuration(self, context, task_run=None, *, probe=None, manage_integrations=False, inherited=None):
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
                tasks="tasks" in cfg.plugins and context != "maintenance",
                task_run=task_run,
                notifications="notifications" in cfg.plugins,
                events="events" in cfg.plugins,
                media="media" in cfg.plugins,
                decisions="decisions" in cfg.plugins,
                integrations="integrations" in cfg.plugins,
                manage_integrations=manage_integrations,
            )
        )
        data = self.registry.snapshot()
        for ident in data["removed"]:
            result["external_" + ident] = {
                "command": sys.executable,
                "args": ["-c", "pass"],
                "enabled": False,
            }
        for ident, row in data["servers"].items():
            spec, transport = row["definition"], copy.deepcopy(row["definition"]["transport"])
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
                "default_tools_approval_mode": "prompt" if spec["approval"] == "reject" else spec["approval"],
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
            "background_policy": "snapshot_at_execution_start",
            "inherited_user_servers": sorted(set(self.inherited) - SERVICES.keys()),
            "inherit_user_servers": self.registry.settings.inherit_user_servers,
        }

    def require_confirmed(self):
        if self.applied_revision is None:
            raise RuntimeError("MCP provider context is unconfirmed; no new input started")

    async def apply(self, force=False):
        backend = self.backend
        async with backend.submit_lock:
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
            revision = self.registry.revision
            summary = (
                await backend.client.request(
                    "thread/read", {"threadId": backend.thread_id, "includeTurns": False}
                )
            )["thread"]
            if summary.get("status", {}).get("type") == "active":
                return {"state": "deferred", "reason": "provider_active", **self.status()}

            # Resuming a loaded thread does not apply new MCP overrides. Release our
            # idle subscription before resuming the SAME persisted conversation.
            async def resume(configuration):
                await backend.client.request("thread/unsubscribe", {"threadId": backend.thread_id})
                return await backend.client.request(
                    "thread/resume",
                    {
                        "threadId": backend.thread_id,
                        "excludeTurns": True,
                        "cwd": backend.config.workspace,
                        "model": backend.config.codex_model,
                        "approvalPolicy": backend.config.approval_policy,
                        "sandbox": backend.config.codex_sandbox,
                        "config": {
                            "model_reasoning_effort": backend.config.codex_reasoning_effort,
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

            # A disconnected management client must not abandon the foreground
            # between unsubscribe and resume. This caller retains the lock and
            # owns the bounded transaction until completion, even on cancellation.
            transaction = asyncio.create_task(refresh())
            try:
                return await asyncio.shield(transaction)
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.gather(transaction, return_exceptions=True))
                raise

    async def open_probe(self, ident, context):
        self.registry.work.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = await self.capture(context, cwd=self.registry.work, probe=ident)
        opening = asyncio.create_task(
            self.backend.client.request(
                "thread/start",
                {
                    "ephemeral": True,
                    "cwd": str(self.registry.work),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
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
                    await self.close_probe(reply["thread"]["id"])
                except Exception:
                    pass

            if self.context:
                self.context.spawn(drain(), "mcp-opening-cleanup")
            else:
                await drain()
            raise
        thread = result["thread"]["id"]
        self.probes.add(thread)
        return thread

    async def close_probe(self, thread):
        try:
            await self.backend.client.request("thread/unsubscribe", {"threadId": thread}, timeout=10)
        finally:
            self.probes.discard(thread)

    async def verify(self, ident, context, schemas=False):
        spec = self.registry.get(ident)["definition"]
        if context not in spec["contexts"]:
            raise ValueError("MCP server is excluded from this context")
        transport = spec["transport"]
        refs = (
            transport.get("env_vars", [])
            if transport["type"] == "stdio"
            else list(transport["env_headers"].values())
            + ([transport["bearer_token_env"]] if transport.get("bearer_token_env") else [])
        )
        missing_env = [
            key
            for key in refs
            if key not in transport.get("env", {}) and (key not in os.environ or key == "OPENAI_API_KEY")
        ]
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
        thread = await self.open_probe(ident, context)
        try:
            result = await self.backend.client.request(
                "mcpServerStatus/list",
                {"threadId": thread, "serverName": "external_" + ident, "detail": "toolsAndAuthOnly"},
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
                visible = (
                    (spec["allow_tools"] is None or tool in spec["allow_tools"])
                    and tool not in spec["deny_tools"]
                    and spec["approval"] != "reject"
                    and spec["tool_approvals"].get(tool) != "reject"
                )
                summaries.append(
                    {
                        "name": tool,
                        "description": (data.get("description") or "")[:300],
                        "enabled_by_policy": visible,
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
        # Reserve before opening: connection startup yields to other callers.
        self.logins[runtime_name] = future
        thread = None
        try:
            thread = await self.open_probe(ident, "foreground")
            result = await self.backend.client.request(
                "mcpServer/oauth/login",
                {"name": runtime_name, "threadId": thread, "timeoutSecs": 300},
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
                    pass  # Optional OAuth cleanup must not fail the reasoning provider.

        self.context.spawn(finish(), "mcp-login")
        return {
            "state": "user_action_required",
            "authorization_url": result["authorizationUrl"],
            "next_step": "Open this URL to authorize, then verify and apply the server configuration",
        }

    async def event(self, event):
        params = event.get("params", {})
        if event.get("method") == "mcpServer/oauthLogin/completed":
            future = self.logins.get(params.get("name"))
            if future and not future.done():
                future.set_result(bool(params.get("success")))
            return True
        if params.get("threadId") in self.probes:
            if "id" in event:
                await self.backend.client.respond(event["id"], {"action": "decline"})
            return True
        return False
