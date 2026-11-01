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


def updated_definition(previous, changes, *, partial_transport=False):
    """Merge explicit updates; shared validation owns defaults and supported policies."""
    if not isinstance(changes, dict):
        raise ValueError("MCP changes must be an object")
    base = deepcopy(previous) if previous else {}
    if partial_transport and "transport" in changes:
        changes = deepcopy(changes)
        transport = changes["transport"]
        if not isinstance(transport, dict):
            raise ValueError("Invalid MCP transport update")
        old = base.get("transport", {})
        if transport.get("type", old.get("type")) == old.get("type"):
            changes["transport"] = {**old, **transport}
        changes["transport"] = {k: v for k, v in changes["transport"].items() if v is not None}
    return definition({**base, **changes})


def patch_registry(previous, change):
    """Apply bounded named edits while retaining every unedited private field."""
    data = validate_registry(previous)
    if not isinstance(change, dict) or set(change) != {"revision", "changes"}:
        raise ValueError("Invalid MCP patch")
    if type(change["revision"]) is not int or change["revision"] != data["revision"]:
        raise ValueError("MCP registry changed; reload configuration")
    operations = change["changes"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= 64:
        raise ValueError("Invalid MCP changes")
    seen = set()
    revision = data["revision"] + 1
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) - {"name", "op", "changes"}:
            raise ValueError("Invalid MCP operation")
        ident = name(operation.get("name"))
        if ident in seen:
            raise ValueError("Duplicate MCP edit")
        seen.add(ident)
        previous_row = data["servers"].get(ident)
        if operation.get("op") == "remove":
            if not previous_row or "changes" in operation:
                raise ValueError("MCP server not found")
            del data["servers"][ident]
            if ident not in data["removed"]:
                data["removed"].append(ident)
        elif operation.get("op") == "configure":
            changes = operation.get("changes", {})
            old = previous_row["definition"] if previous_row else None
            spec = updated_definition(old, changes, partial_transport=True)
            if not previous_row or spec != old:
                data["servers"][ident] = {"revision": revision, "definition": spec}
            data["removed"] = [item for item in data["removed"] if item != ident]
        else:
            raise ValueError("Unknown MCP edit operation")
    if data != previous:
        data["revision"] = revision
    return validate_registry(data)


def tool_permission(spec, tool, context=None, allowed=None):
    """Effective call policy, also used by verification and operator projections."""
    if not spec["enabled"]:
        return "disabled"
    if context is not None and context not in spec["contexts"]:
        return "scope_excluded"
    if allowed is not None and tool not in allowed:
        return "execution_excluded"
    if spec["approval"] == "reject":
        return "rejected"
    if tool in spec["deny_tools"] or spec["tool_approvals"].get(tool) == "reject":
        return "denied"
    if spec["allow_tools"] is not None and tool not in spec["allow_tools"]:
        return "not_selected"
    return "allowed"


def public_entry(ident, row):
    """Connection arguments, paths/queries and literal values never leave the owner."""
    from urllib.parse import urlsplit

    spec = deepcopy(row["definition"])
    t = spec["transport"]
    if t["type"] == "stdio":
        transport = {
            "type": "stdio",
            "command": t["command"],
            "cwd": t.get("cwd"),
            "argument_count": len(t["args"]),
            "env_names": sorted(t["env"]),
            "env_vars": t["env_vars"],
        }
    else:
        parsed = urlsplit(t["url"])
        transport = {
            "type": "http",
            "origin": parsed.scheme + "://" + parsed.netloc,
            "header_names": sorted(t["headers"]),
            "env_headers": t["env_headers"],
            "bearer_token_env": t.get("bearer_token_env"),
            "url_configured": True,
        }
    spec["transport"] = transport
    return {
        "name": ident,
        "runtime_name": "external_" + ident,
        "revision": row["revision"],
        "definition": spec,
    }


def audit_fields(operation):
    """Only validated identities and known field names; never transport contents."""
    if operation.get("op") != "patch":
        return []
    fields = []
    for change in operation["value"]["changes"]:
        ident = name(change["name"])
        if change["op"] == "remove":
            fields.append("mcp." + ident + ".removed")
        else:
            fields.extend("mcp." + ident + "." + key for key in change.get("changes", {}))
    return fields
