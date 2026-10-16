"""Codex-specific registration; MCP services themselves do not import Codex."""

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
):
    # Project-scoped local operations, including the explicitly requested camera
    # pan/tilt integration. This does not change global Codex permissions.
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
        if name in {"memory", "device"}
        or {
            "tasks": tasks or bool(task_run),
            "notifications": notifications,
            "integrations": integrations,
            "events": events,
        }.get(name, False)
        for tools in (descriptor["tools"],)
    }
