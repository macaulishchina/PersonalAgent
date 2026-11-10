"""Agent-specific MCP registration; MCP services themselves do not import provider code."""

from assistant_runtime.api.deadlines import LOCAL_TRANSPORT_SECONDS

from assistant_runtime.core.plugin_catalog import services


def service_owner(name):
    return "devices" if name == "device" else name


def server_config(
    socket,
    enabled=True,
    *,
    plugins=("memory", "devices", "ai_usage"),
    task_run=None,
    manage_integrations=False,
):
    selected = set(plugins)
    result = {}
    for name, descriptor in services(selected).items():
        result[name] = {
            "local_service": name,
            "socket": str(socket),
            **({"task_run": task_run} if descriptor.task_context and task_run else {}),
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
        "local_service": "extensions",
        "socket": str(socket),
        "enabled": enabled,
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": 60,
        "tool_timeout_sec": LOCAL_TRANSPORT_SECONDS,
    }
    return result


def normalize_services(servers):
    """Convert the generated transport in older saved execution metadata."""
    result = {}
    for name, spec in servers.items():
        args = spec.get("args", [])
        if not name.startswith("external_") and args[:2] == ["-m", "assistant_runtime.mcp"]:
            if len(args) < 5 or args[2:4] != [name, "--socket"]:
                raise ValueError("Invalid saved internal MCP service")
            spec = {k: v for k, v in spec.items() if k not in {"command", "args"}}
            spec.update(local_service=name, socket=args[4])
            if "--task-run" in args[5:]:
                spec["task_run"] = args[args.index("--task-run", 5) + 1]
        result[name] = spec
    return result


def to_pi_mcp_json(mcp_servers, *, preload=False, allowed=None, limit=12, shared_url=None):
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
        if spec.get("local_service"):
            if not enabled:
                continue
            if not shared_url:
                raise ValueError("Internal MCP transport is not running")
            entry["url"] = f"{shared_url}/{name}"
            entry["headers"] = {"Authorization": "Bearer ${PA_SHARED_TOOL_TOKEN}"}
        elif "command" in spec:
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
            tool_exposure = {}
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
