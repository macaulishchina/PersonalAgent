"""Version policy belongs to composition, outside stdlib-only plugin contracts."""

import re
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from assistant_runtime.api.releases import (
    CORE_VERSION as CORE_VERSION,
    CompatibilityError as CompatibilityError,
    framework as framework,
    manifest as manifest,
    read_json as read_json,
    descriptor as contract_descriptor,
)


def version(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value
    ):
        raise ValueError("Version must be a stable major.minor.patch")
    return Version(value)


def constraint(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError("Missing or invalid version range")
    try:
        result = SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise ValueError("Invalid version range") from exc
    if not result or any(s.operator not in {">=", ">", "<=", "<", "==", "!="} for s in result):
        raise ValueError("Use explicit stable version comparisons")
    for spec in result:
        version(spec.version)
    return result


def descriptor(data, *, core=False, core_version=CORE_VERSION):
    value = contract_descriptor(data, core=core, core_version=core_version)
    supported = constraint(value["requires_core"])
    if not any(s.operator in {"<", "<=", "=="} for s in supported):
        raise ValueError("Core compatibility requires an upper bound")
    for limits in {**value["requires"], **value["optional"]}.values():
        constraint(limits)
    return value


def read_descriptor(path, *, core=False):
    return descriptor(read_json(path), core=core)


def compatibility(data, release=None):
    release = release or framework()
    if data["api_version"] != release["api_version"]:
        return f"Plugin {data['id']}: API {data['api_version']} required; framework provides {release['api_version']}"
    if version(release["version"]) not in constraint(data["requires_core"]):
        return f"Plugin {data['id']} {data['version']} requires core {data['requires_core']}; current {release['version']}"
    return None


def check_dependencies(releases, enabled, *, require_all=True):
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise CompatibilityError("Plugin dependency cycle: " + name)
        if name in visited:
            return
        if name not in releases:
            raise CompatibilityError("Plugin not installed: " + name)
        visiting.add(name)
        data = releases[name]
        for key in ("requires", "optional"):
            for dep, limits in data[key].items():
                if dep not in enabled:
                    if key == "optional" or not require_all:
                        continue
                    raise CompatibilityError(f"Plugin {name} requires enabled plugin {dep} {limits}")
                if dep not in releases:
                    raise CompatibilityError("Plugin not installed: " + dep)
                actual = releases[dep]["version"]
                if version(actual) not in constraint(limits):
                    raise CompatibilityError(f"Plugin {name} requires {dep} {limits}; installed {actual}")
                visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in enabled:
        visit(name)
