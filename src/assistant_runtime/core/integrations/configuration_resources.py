"""Registry resource declaration and semantic checks owned by integrations."""

import json
from pathlib import Path


def registered(root, data):
    from assistant_runtime.core.configuration.resources import Resource

    path = (
        root
        / Path(data.get("external_mcp", {}).get("registry_path") or "config/mcp/servers.json").expanduser()
    )
    return [Resource("mcp", path, "mcp", "外部 MCP 注册表", True)]


def validate_candidate(config, values):
    if "integrations" in config.enabled_plugins and values.get("mcp") is not None:
        from .configuration import validate_registry

        validate_registry(json.loads(values["mcp"]), config.external_mcp.get("max_servers", 32))
