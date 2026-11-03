"""Capture one session's explicit tool and skill selection."""

from copy import deepcopy
import re

NATIVE_TOOLS = frozenset(
    {
        "read",
        "read_result",
        "write",
        "edit",
        "bash",
        "powershell",
        "grep",
        "find",
        "ls",
        "request_user_input",
        "pa_read",
        "pa_write",
        "pa_check",
    }
)


def names(value, label, maximum):
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", name) for name in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a bounded list of unique names")
    return sorted(value)


def select(tools, skills, servers, paths):
    servers = deepcopy(servers)
    selected_paths = list(paths)
    if skills is not None:
        skills = names(skills, "skills", 32)
        known = {path.name: path for path in paths}
        if set(skills) - known.keys():
            raise ValueError("Execution requested an unavailable skill")
        selected_paths = [known[name] for name in skills]
    if tools is not None:
        tools = names(tools, "tools", 256)
        for name in tools:
            if name in NATIVE_TOOLS:
                continue
            parts = name.split("__", 2)
            if len(parts) != 3 or parts[0] != "mcp" or parts[1] not in servers:
                raise ValueError("Execution requested an unavailable tool or server")
            server, tool = servers[parts[1]], parts[2]
            if (
                not server.get("enabled", True)
                or tool in server.get("disabled_tools", [])
                or (server.get("enabled_tools") is not None and tool not in server["enabled_tools"])
            ):
                raise ValueError("Execution requested a tool outside its configured scope")
        for name, server in servers.items():
            prefix = f"mcp__{name}__"
            selected = [t[len(prefix) :] for t in tools if t.startswith(prefix)]
            server["enabled"] = server.get("enabled", True) and bool(selected)
            server["enabled_tools"] = selected
    return {"allowed_tools": tools, "skill_paths": [str(p) for p in selected_paths], "mcp_servers": servers}
