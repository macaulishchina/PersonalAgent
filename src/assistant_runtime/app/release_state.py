"""Monotonic instance release guard, separate from configuration revision history."""

import json
from pathlib import Path
from datetime import datetime, timezone

from assistant_runtime.app.versions import framework, read_json, version, CompatibilityError
from assistant_runtime.api.system import system


def read(root):
    path = Path(root) / "state/release-state.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
        if data.get("schema_version") != 1 or type(data.get("data_format")) is not int:
            raise ValueError()
        version(data["highest_core_version"])
        highest = data["highest_plugin_versions"]
        if not isinstance(highest, dict) or len(highest) > 128:
            raise ValueError()
        for name, value in highest.items():
            import re

            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError()
            version(value)
        last = data.get("last_successful")
        if last is not None:
            if not isinstance(last, dict) or not isinstance(last.get("plugins"), dict):
                raise ValueError()
            if version(last["core_version"]) > version(data["highest_core_version"]):
                raise ValueError()
            for name, value in last["plugins"].items():
                if name not in highest or version(value) > version(highest[name]):
                    raise ValueError()
            datetime.fromisoformat(last["at"])
        return data
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise CompatibilityError(
            "Instance release record is invalid; restore a verified backup before startup"
        ) from exc


def check(root, release=None, plugins=None):
    release = release or framework()
    saved = read(root)
    if saved:
        if saved["data_format"] != release["data_format"]:
            raise CompatibilityError("Instance data format requires an explicit migration or matching backup")
        if version(release["version"]) < version(saved["highest_core_version"]):
            raise CompatibilityError(
                f"Instance was opened by core {saved['highest_core_version']}; downgrade requires a matching pre-upgrade backup"
            )
        for name, actual in (plugins or {}).items():
            highest = saved["highest_plugin_versions"].get(name)
            if highest and version(actual) < version(highest):
                raise CompatibilityError(
                    f"Plugin {name}: instance was opened by {highest}; downgrade to {actual} requires a matching backup"
                )
    return saved


def attempted(root, plugins=None):
    release = framework()
    previous = check(root, plugins=plugins)
    value = {
        "schema_version": 1,
        "highest_core_version": release["version"],
        "data_format": release["data_format"],
        "last_successful": (previous or {}).get("last_successful"),
        "highest_plugin_versions": {**(previous or {}).get("highest_plugin_versions", {}), **(plugins or {})},
    }
    system.atomic_write(Path(root) / "state/release-state.json", json.dumps(value).encode())


def successful(root, plugins):
    value = check(root)
    if value is None:
        raise CompatibilityError("Missing startup release guard")
    value["last_successful"] = {
        "core_version": framework()["version"],
        "plugins": plugins,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    system.atomic_write(Path(root) / "state/release-state.json", json.dumps(value).encode())
