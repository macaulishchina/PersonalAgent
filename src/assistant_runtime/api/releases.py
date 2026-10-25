"""Static release contracts. No plugin imports, devices or installation actions."""

import json
from pathlib import Path
import re


from assistant_runtime.kernel.runtime import Manifest
from assistant_runtime.kernel.events import API_VERSION

ROOT = Path(__file__).resolve().parents[1]
CORE_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


class CompatibilityError(ValueError):
    """Public diagnostics contain only validated release identifiers and constraints."""


def version(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value
    ):
        raise ValueError("Version must be a stable major.minor.patch")
    return tuple(map(int, value.split(".")))


def constraint(value):
    # Public contracts are stdlib-only; the composition layer validates range semantics.
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError("Missing or invalid version range")
    return value


def decode_json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate descriptor field")
            result[key] = value
        return result

    if len(content) > 65536:
        raise ValueError("Release descriptor exceeds 64 KiB")
    data = json.loads(content, object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise ValueError("Release descriptor must be an object")
    return data


def read_json(path):
    return decode_json(Path(path).read_bytes())


def framework():
    data = read_json(ROOT / "release.json")
    version(CORE_VERSION)
    if (
        type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
        or data.get("api_version") != API_VERSION
        or type(data.get("data_format")) is not int
        or data["data_format"] < 1
    ):
        raise ValueError("Invalid framework release descriptor")
    return {**data, "version": CORE_VERSION}


def descriptor(data, *, core=False, core_version=CORE_VERSION):
    data = dict(data)
    name = data.get("id")
    if (
        type(data.get("schema_version")) is not int
        or data.get("schema_version") != 1
        or not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
    ):
        raise ValueError("Missing or invalid plugin release identity")
    if core:
        if data.get("version") != "core" or data.get("requires_core") != "core":
            raise ValueError("Core plugins must follow the framework release")
        data["version"], data["requires_core"] = core_version, "==" + core_version
    version(data.get("version"))
    constraint(data.get("requires_core"))
    if type(data.get("api_version")) is not int or data["api_version"] < 1:
        raise ValueError("Plugin must explicitly declare its API version")
    if type(data.get("critical")) is not bool or data["critical"] and not core:
        raise ValueError("Invalid critical plugin declaration")
    platforms = data.get("supported_platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or any(p not in ("linux", "windows") for p in platforms)
        or len(set(platforms)) != len(platforms)
    ):
        raise ValueError("Missing or invalid supported platforms")
    from .backends import BackendSpec

    if "backends" in data:
        BackendSpec(data["backends"], data.get("default_backends", {}))
    elif "default_backends" in data:
        raise ValueError("Default implementations require backend declarations")
    seen = set()
    for key in ("requires", "optional"):
        values = data.get(key)
        if not isinstance(values, dict) or len(values) > 64:
            raise ValueError("Plugin must declare dependency ranges")
        values = dict(values)
        for dep, limits in values.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", dep) or dep == name or dep in seen:
                raise ValueError("Invalid or duplicate plugin dependency")
            seen.add(dep)
            if limits == "core" and core:
                limits = values[dep] = "==" + core_version
            constraint(limits)
        data[key] = values
    return data


def read_descriptor(path, *, core=False):
    return descriptor(read_json(path), core=core)


def manifest(data):
    return Manifest(
        data["id"],
        tuple(data["requires"]),
        api_version=data["api_version"],
        version=data["version"],
        critical=data["critical"],
        optional=tuple(data["optional"]),
        dependency_versions=tuple({**data["requires"], **data["optional"]}.items()),
    )


def plugin_manifest(owner_file):
    path = Path(owner_file).resolve()
    source = path.with_name("plugin.json")
    if path.name.endswith("_declaration.py"):
        source = path.with_name(path.name.removesuffix("_declaration.py") + ".plugin.json")
    return manifest(read_descriptor(source, core=path.is_relative_to(ROOT / "core")))
