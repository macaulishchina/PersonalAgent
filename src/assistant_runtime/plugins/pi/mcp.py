"""Agent-specific MCP registration; MCP services themselves do not import provider code."""

import sys

from ...mcp.catalog import SERVICES


def server_config(
    socket,
    enabled=True,
    tasks=False,
    task_run=None,
    notifications=False,
    integrations=False,
    manage_integrations=False,
    events=False,
    media=False,
    search=False,
    decisions=False,
    models=False,
):
    return {
        name: {
            "command": sys.executable,
            "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)]
            + (["--task-run", task_run] if SERVICES[name]["task_context"] and task_run else [])
            + (
                ["--manage-integrations"]
                if name == "integrations" and task_run and manage_integrations
                else []
            ),
            "enabled": enabled,
            "enabled_tools": list(
                tools[:3] if name == "integrations" and task_run and not manage_integrations else tools
            ),
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 200 if name == "integrations" else 100,
        }
        for name, descriptor in SERVICES.items()
        if name in {"memory", "device", "ai_usage"}
        or {
            "tasks": tasks or bool(task_run),
            "notifications": notifications,
            "integrations": integrations,
            "events": events,
            "media": media,
            "search": search,
            "decisions": decisions,
            "models": models,
        }.get(name, False)
        for tools in (descriptor["tools"],)
    }


def to_pi_mcp_json(mcp_servers):
    """Translate internal mcp_servers dict to Pi's mcp.json schema."""
    servers = {}
    for name, spec in (mcp_servers or {}).items():
        if not isinstance(spec, dict):
            continue
        enabled = bool(spec.get("enabled", True))
        entry = {
            "enabled": enabled,
            "exposure": "direct",
            "timeout": int(spec.get("tool_timeout_sec") or spec.get("startup_timeout_sec") or 60),
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
        disabled_tools = set(spec.get("disabled_tools") or [])
        if enabled_tools is not None:
            if len(enabled_tools) == 0:
                entry["exposure"] = "hidden"
            else:
                entry["exposure"] = "hidden"
                for t in enabled_tools:
                    if t not in disabled_tools:
                        tool_exposure[t] = "direct"
        for t in disabled_tools:
            tool_exposure[t] = "hidden"
        if tool_exposure:
            entry["toolExposure"] = tool_exposure
        servers[name] = entry
    return {"autoEnableCodemode": False, "mcpServers": servers}
