"""Codex-specific registration; MCP services themselves do not import Codex."""

import sys

from ...mcp.catalog import MEMORY_TOOLS, DEVICE_TOOLS


def server_config(socket, enabled=True):
    # The user authorized these existing memory/device operations for this project's
    # assistant. Splitting their schemas does not grant any new capability.
    return {
        name: {
            "command": sys.executable,
            "args": ["-m", "assistant_runtime.mcp", name, "--socket", str(socket)],
            "enabled": enabled,
            "enabled_tools": list(tools),
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 100,
        }
        for name, tools in (("memory", MEMORY_TOOLS), ("device", DEVICE_TOOLS))
    }
