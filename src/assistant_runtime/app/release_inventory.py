"""Offline version inspection usable even when business configuration cannot start."""

from pathlib import Path

from assistant_runtime.app.versions import (
    CompatibilityError,
    compatibility,
    check_dependencies,
    framework,
)
from assistant_runtime.api.backends import BackendSpec, select_backend
from assistant_runtime.api.system import system
from assistant_runtime.core.plugin_catalog import OWNERS, release
from .plugin_metadata import installed_release


def releases():
    result, errors = {}, {}
    for name in OWNERS:
        try:
            result[name] = release(name)
        except (ValueError, OSError, TypeError, KeyError):
            errors[name] = "Missing or invalid release declaration"
    from .extensions import entry_points

    entries = list(entry_points(group="personal_agent.plugins"))
    for entry in entries:
        name = entry.name
        if name in OWNERS or sum(e.name == name for e in entries) != 1:
            errors[name] = "Ambiguous installed plugin identity"
            continue
        try:
            result[name] = installed_release(entry)
        except (ValueError, OSError, TypeError, KeyError):
            errors[name] = "Missing or invalid release declaration; update the plugin"
    return result, errors


def inspect(data, records, errors=None, *, target=None, core_names=None):
    target = target or framework()
    errors = dict(errors or {})
    enabled = data.get("plugins")
    if enabled is None:
        from .presets import default_plugins

        enabled = default_plugins()
    if (
        not isinstance(enabled, list)
        or any(not isinstance(n, str) for n in enabled)
        or len(set(enabled)) != len(enabled)
    ):
        raise ValueError("Invalid plugin selection")
    defaults = data.get("plugin_backends", {})
    rows = []
    for name in sorted(set(records) | set(errors) | set(enabled)):
        info = records.get(name)
        backend = None
        error = errors.get(name)
        if not info:
            error = error or "Plugin not installed"
        elif not error:
            error = compatibility(info, target)
            if system.name not in info.get("supported_platforms", []):
                error = error or f"Plugin {name}: platform {system.name} is not certified"
            if not error and name in enabled:
                try:
                    policy = (
                        BackendSpec(info["backends"], info.get("default_backends", {}))
                        if "backends" in info
                        else None
                    )
                    backend = select_backend(name, policy, system.name, defaults.get(name, {}))
                except (ValueError, TypeError, KeyError, AttributeError):
                    error = "Invalid implementation selection for current platform"
                for kind in ("requires", "optional"):
                    for dep, limits in info[kind].items():
                        from assistant_runtime.app.versions import constraint, version

                        if dep not in enabled:
                            if kind == "requires":
                                error = f"Plugin {name} requires enabled plugin {dep} {limits}"
                        elif dep not in records or version(records[dep]["version"]) not in constraint(limits):
                            error = f"Plugin {name} requires {dep} {limits}; installed {(records.get(dep) or {}).get('version', 'unknown')}"
        rows.append(
            {
                "name": name,
                "kind": "core"
                if name
                in (
                    core_names
                    if core_names is not None
                    else {n for n, m in OWNERS.items() if m.startswith("assistant_runtime.core.")}
                )
                else "extension",
                "enabled": name in enabled,
                "version": info["version"] if info else None,
                "requires_core": info["requires_core"] if info else None,
                "api_version": info["api_version"] if info else None,
                "supported_platforms": info.get("supported_platforms", []) if info else [],
                "platform_supported": bool(info and system.name in info["supported_platforms"]),
                "backend": backend,
                "requires": info["requires"] if info else {},
                "optional": info["optional"] if info else {},
                "compatible": not error,
                "error": error,
            }
        )
    by_name = {r["name"]: r for r in rows}
    for _ in rows:
        changed = False
        for row in rows:
            if not row["enabled"] or row["error"]:
                continue
            info = records[row["name"]]
            bad = next(
                (
                    dep
                    for dep in {**info["requires"], **info["optional"]}
                    if dep in enabled and by_name[dep]["error"]
                ),
                None,
            )
            if bad:
                row.update(compatible=False, error=f"Dependency {bad} is incompatible")
                changed = True
        if not changed:
            break
    blockers = [{"plugin": r["name"], "reason": r["error"]} for r in rows if r["enabled"] and r["error"]]
    if not blockers:
        try:
            check_dependencies(records, enabled)
        except CompatibilityError as exc:
            blockers.append({"plugin": None, "reason": str(exc)})
    return {
        "core": target,
        "platform": system.name,
        "compatible": not blockers,
        "blockers": blockers,
        "plugins": rows,
    }


def inventory(entry):
    from .config import read_config
    from .release_state import check

    entry = Path(entry).resolve()
    data = read_config(entry)
    root = Path(data.get("root", entry.parent)).expanduser()
    root = (entry.parent / root).resolve() if not root.is_absolute() else root.resolve()
    records, errors = releases()
    result = inspect(data, records, errors)
    try:
        result["instance_release"] = check(
            root,
            plugins={
                row["name"]: row["version"] for row in result["plugins"] if row["enabled"] and row["version"]
            },
        )
    except CompatibilityError as exc:
        result["instance_release"] = None
        result["blockers"].append({"plugin": None, "reason": str(exc)})
        result["compatible"] = False
    return result
