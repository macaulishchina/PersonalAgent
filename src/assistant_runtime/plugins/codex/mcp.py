"""Codex-specific registration; MCP services themselves do not import Codex."""

import sys

from ...mcp.catalog import MEMORY_TOOLS, DEVICE_TOOLS, TASK_TOOLS, NOTIFICATION_TOOLS, INTEGRATION_TOOLS


def server_config(
    socket,
    enabled=True,
    tasks=False,
    task_run=None,
    notifications=False,
    integrations=False,
    manage_integrations=False,
):
    # Project-scoped local operations, including the explicitly requested camera
    # pan/tilt integration. This does not change global Codex permissions.
    return {
        name: {
            "command": sys.executable,
            "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)]
            + (
                ["--task-run", task_run]
                if name in {"tasks", "notifications", "integrations"} and task_run
                else []
            )
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
        for name, tools in (
            ("memory", MEMORY_TOOLS),
            ("device", DEVICE_TOOLS),
            *(([("tasks", TASK_TOOLS)]) if tasks or task_run else []),
            *(([("notifications", NOTIFICATION_TOOLS)]) if notifications else []),
            *(([("integrations", INTEGRATION_TOOLS)]) if integrations else []),
        )
    }
