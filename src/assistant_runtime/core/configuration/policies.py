"""Static field update policies shared by runtime previews and the operator UI."""

MODES = {"immediate", "boundary", "restart"}


def application_for(spec, relative):
    policies = spec.get("application", {})
    selected, length = "restart", -1
    for pattern, mode in policies.items():
        if mode not in MODES:
            raise ValueError("Unknown configuration application mode")
        parts = pattern.split(".") if pattern != "*" else []
        if len(parts) <= len(relative) and all(a == "*" or a == b for a, b in zip(parts, relative)):
            if len(parts) > length:
                selected, length = mode, len(parts)
    return selected


def changes(before, after, path=()):
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            if key not in before or key not in after:
                yield [*path, key]
            else:
                yield from changes(before[key], after[key], (*path, key))
    else:
        yield list(path)


def field_policy(inventory, path):
    from .catalog import specifications

    for spec in specifications(inventory):
        prefix = spec.get("namespace", "").split(".") if spec.get("namespace") else []
        relative = path[len(prefix) :]
        schema = spec["schema"]
        dynamic = (
            prefix and isinstance(schema.get("additionalProperties"), dict) and not schema.get("properties")
        )
        if path[: len(prefix)] == prefix and (
            dynamic or relative and relative[0] in schema.get("properties", {})
        ):
            return application_for(spec, relative)
    return "restart"
