"""Agent-specific MCP registration; MCP services themselves do not import provider code."""

import sys
from assistant_runtime.api.deadlines import LOCAL_TRANSPORT_SECONDS

from assistant_runtime.core.plugin_catalog import services


def server_config(
    socket,
    enabled=True,
    *,
    plugins=("memory", "devices", "ai_usage"),
    task_run=None,
    manage_integrations=False,
):
    selected = set(plugins)
    if task_run:
        selected.add("tasks")
    result = {}
    for name, descriptor in services(selected).items():
        owner = "devices" if name == "device" else name
        if owner not in selected:
            continue
        result[name] = {
            "command": sys.executable,
            "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)]
            + (["--task-run", task_run] if descriptor.task_context and task_run else [])
            + (
                ["--manage-integrations"]
                if name == "integrations" and task_run and manage_integrations
                else []
            ),
            "enabled": enabled,
            **(
                {"enabled_tools": list(descriptor.background_tools)}
                if task_run and not manage_integrations and descriptor.background_tools is not None
                else {}
            ),
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 60,
            "tool_timeout_sec": LOCAL_TRANSPORT_SECONDS,
        }
    result["extensions"] = {
        "command": sys.executable,
        "args": ["-m", "assistant_runtime.mcp", "extensions", "--socket", str(socket)],
        "enabled": enabled,
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 60,
        "tool_timeout_sec": LOCAL_TRANSPORT_SECONDS,
    }
    return result


def to_pi_mcp_json(mcp_servers, *, preload=False, allowed=None, limit=12):
    """Translate internal mcp_servers dict to Pi's mcp.json schema."""
    selected = [name for name in (allowed or []) if name.startswith("mcp__")]
    eager = preload and 0 < len(selected) <= min(4, limit)
    servers = {}
    for name, spec in (mcp_servers or {}).items():
        if not isinstance(spec, dict):
            continue
        enabled = bool(spec.get("enabled", True))
        entry = {
            "enabled": enabled,
            "exposure": "deferred",
            "timeout": int(spec.get("tool_timeout_sec") or spec.get("startup_timeout_sec") or 600),
        }
        if "command" in spec:
            entry["command"] = spec["command"]
            if spec.get("args"):
                entry["args"] = list(spec["args"])
            if spec.get("cwd"):
                entry["cwd"] = str(spec["cwd"])
            if spec.get("env"):
                entry["env"] = dict(spec["env"])
        elif "url" in spec:
            entry["url"] = spec["url"]
            headers = dict(spec.get("http_headers") or {})
            for header, env_name in (spec.get("env_http_headers") or {}).items():
                headers[header] = f"${{{env_name}}}"
            if spec.get("bearer_token_env_var"):
                headers["Authorization"] = f"Bearer ${{{spec['bearer_token_env_var']}}}"
            if headers:
                entry["headers"] = headers
        else:
            continue
        tool_exposure = {}
        enabled_tools = spec.get("enabled_tools")
        rules = spec.get("tools") or {}
        disabled_tools = set(spec.get("disabled_tools") or []) | {
            name for name, rule in rules.items() if rule.get("approval_mode") == "reject"
        }
        if enabled_tools is not None:
            if len(enabled_tools) == 0:
                entry["exposure"] = "hidden"
            else:
                entry["exposure"] = "hidden"
                for t in enabled_tools:
                    if t not in disabled_tools:
                        tool_exposure[t] = "deferred"
        if spec.get("default_tools_approval_mode") == "reject":
            entry["exposure"] = "hidden"
            tool_exposure = {
                t: "deferred"
                for t, rule in rules.items()
                if rule.get("approval_mode") in {"approve", "prompt"}
                and t not in disabled_tools
                and (enabled_tools is None or t in enabled_tools)
            }
        for t in disabled_tools:
            tool_exposure[t] = "hidden"
        if eager:
            # Pi waits for these servers before its first request. Hidden tools stay hidden.
            for tool, exposure in list(tool_exposure.items()):
                if exposure == "deferred" and f"mcp__{name}__{tool}" in selected:
                    tool_exposure[tool] = "direct"
        if tool_exposure:
            entry["toolExposure"] = tool_exposure
        servers[name] = entry
    return {"autoEnableCodemode": False, "mcpServers": servers}
