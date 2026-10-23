"""Pure registry configuration validation, shared by runtime and operator transactions."""

from copy import deepcopy
from .settings import definition, name, strings


def validate_registry(value, max_servers=64):
    data = deepcopy(value)
    if (
        not isinstance(data, dict)
        or set(data) != {"revision", "servers", "removed"}
        or type(data["revision"]) is not int
        or data["revision"] < 0
    ):
        raise ValueError("Invalid MCP registry")
    if not isinstance(data["servers"], dict) or len(data["servers"]) > max_servers:
        raise ValueError("Too many MCP servers")
    data["removed"] = strings(data["removed"], "removed servers", 256)
    for ident in data["removed"]:
        name(ident)
    for ident, row in data["servers"].items():
        name(ident)
        if (
            not isinstance(row, dict)
            or set(row) != {"revision", "definition"}
            or type(row["revision"]) is not int
            or not 1 <= row["revision"] <= data["revision"]
        ):
            raise ValueError("Invalid MCP server revision")
        row["definition"] = definition(row["definition"])
    if set(data["removed"]) & data["servers"].keys():
        raise ValueError("MCP server cannot also be removed")
    return data


def prepare_registry(previous, candidate):
    previous = validate_registry(previous)
    # The editor may submit the full resource or just a named definition map.
    entries = candidate.get("servers", candidate)
    if not isinstance(entries, dict) or len(entries) > 64:
        raise ValueError("Invalid MCP servers")
    revision = previous["revision"] + 1
    servers = {
        name(ident): {"revision": revision, "definition": definition(row.get("definition", row))}
        for ident, row in entries.items()
    }
    removed = sorted(
        (set(previous["removed"]) | (previous["servers"].keys() - servers.keys())) - servers.keys()
    )
    data = {"revision": revision, "servers": servers, "removed": removed}
    return validate_registry(data)
