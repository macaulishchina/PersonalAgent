"""Codex-specific registration; MCP services themselves do not import Codex."""

import sys

from ...mcp.catalog import MEMORY_TOOLS, DEVICE_TOOLS, TASK_TOOLS, NOTIFICATION_TOOLS


def server_config(socket, enabled=True, tasks=False, task_run=None, notifications=False):
    # Project-scoped local operations, including the explicitly requested camera
    # pan/tilt integration. This does not change global Codex permissions.
    return {
        name: {
            "command": sys.executable,
            "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)]
            + (["--task-run", task_run] if name in {"tasks", "notifications"} and task_run else []),
            "enabled": enabled,
            "enabled_tools": list(tools),
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 100,
        }
        for name, tools in (
            ("memory", MEMORY_TOOLS),
            ("device", DEVICE_TOOLS),
            *(([("tasks", TASK_TOOLS)]) if tasks or task_run else []),
            *(([("notifications", NOTIFICATION_TOOLS)]) if notifications else []),
        )
    }
