"""Read candidate wheels as data. Never import, extract or install their contents."""

import configparser
from email.parser import Parser
import hashlib
from pathlib import Path
import re
import sys
import zipfile

from packaging.utils import parse_wheel_filename
from packaging.tags import sys_tags
from packaging.specifiers import SpecifierSet

from assistant_runtime.app.versions import descriptor, version
from assistant_runtime.api.releases import decode_json


def read_wheel(path, *, framework_package=False):
    try:
        return _read_wheel(path, framework_package=framework_package)
    except (zipfile.BadZipFile, configparser.Error, UnicodeError) as exc:
        raise ValueError("Invalid candidate wheel metadata") from exc


def _read_wheel(path, *, framework_package=False):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("Candidate must be a wheel smaller than 512 MiB")
    package_name, package_version, _, tags = parse_wheel_filename(path.name)
    if not tags.intersection(sys_tags()):
        raise ValueError("Candidate wheel is not installable on this Python/platform")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Candidate wheel contains duplicate paths")

        def read(name):
            if archive.getinfo(name).file_size > 256 * 1024:
                raise ValueError("Candidate metadata is too large")
            return archive.read(name).decode("utf-8")

        metadata = [n for n in names if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Candidate wheel must contain one distribution")
        info = Parser().parsestr(read(metadata[0]))
        from packaging.utils import canonicalize_name

        if (
            canonicalize_name(info["Name"] or "") != package_name
            or version(info["Version"]) != package_version
        ):
            raise ValueError("Wheel filename and distribution release differ")
        requires_python = info.get("Requires-Python", "")
        if requires_python and not SpecifierSet(requires_python).contains(
            ".".join(map(str, sys.version_info[:3]))
        ):
            raise ValueError("Candidate requires a different Python version")
        records, core_names = {}, set()
        release = None
        if framework_package:
            if package_name != "personal-agent":
                raise ValueError("Expected a personal-agent framework wheel")
            release = decode_json(read("assistant_runtime/release.json"))
            release["version"] = read("assistant_runtime/VERSION").strip()
            if version(release["version"]) != package_version or release.get("schema_version") != 1:
                raise ValueError("Framework and distribution release differ")
            if any(type(release.get(k)) is not int or release[k] < 1 for k in ("api_version", "data_format")):
                raise ValueError("Invalid framework compatibility metadata")
            owners = decode_json(read("assistant_runtime/owners.json"))
            if not isinstance(owners, dict) or not owners or len(owners) > 128:
                raise ValueError("Invalid bundled plugin catalog")
            for name, module in owners.items():
                if not isinstance(module, str) or not re.fullmatch(
                    r"assistant_runtime\.(core|plugins)\.[a-z][a-z0-9_]*", module
                ):
                    raise ValueError("Invalid bundled plugin path")
                core = module.startswith("assistant_runtime.core.")
                relative = module.replace(".", "/")
                resource = relative + (
                    ".plugin.json" if relative + ".plugin.json" in names else "/plugin.json"
                )
                data = descriptor(decode_json(read(resource)), core=core, core_version=release["version"])
                if data["id"] != name:
                    raise ValueError("Bundled release identity differs from catalog")
                records[name] = data
                if core:
                    core_names.add(name)
        else:
            if package_name == "personal-agent":
                raise ValueError("Use the framework wheel as the main candidate")
            entries = configparser.ConfigParser(interpolation=None)
            entries.optionxform = str
            entries.read_string(read(metadata[0].rsplit("/", 1)[0] + "/entry_points.txt"))
            if "personal_agent.plugins" not in entries:
                raise ValueError("Candidate declares no PA plugins")
            for name, entry in entries["personal_agent.plugins"].items():
                module = entry.split(":")[0].strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
                    raise ValueError("Invalid plugin entry point")
                candidates = [
                    module.replace(".", "/") + suffix for suffix in ("/plugin.json", ".plugin.json")
                ]
                paths = [p for p in candidates if p in names]
                if len(paths) != 1:
                    raise ValueError("Missing or ambiguous plugin release descriptor")
                data = descriptor(decode_json(read(paths[0])))
                if data["id"] != name or version(data["version"]) != package_version:
                    raise ValueError("Plugin and distribution release differ")
                records[name] = data
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": str(path),
        "distribution": package_name,
        "sha256": digest,
        "release": release,
        "plugins": records,
        "core_names": core_names,
    }


def preflight(entry, wheel, plugin_wheels=()):
    from .config import read_config
    from .release_inventory import releases, inspect
    from .release_state import check
    from assistant_runtime.app.versions import CompatibilityError, CORE_VERSION
    from assistant_runtime.core.plugin_catalog import OWNERS
    from .extensions import entry_points
    from packaging.utils import canonicalize_name

    entry = Path(entry).expanduser().resolve()
    data = read_config(entry)
    root = Path(data.get("root", entry.parent)).expanduser()
    root = (entry.parent / root).resolve() if not root.is_absolute() else root.resolve()
    candidate = read_wheel(wheel, framework_package=True)
    records, errors = releases()
    before = dict(records)
    records = {n: r for n, r in records.items() if n not in OWNERS}
    errors = {n: e for n, e in errors.items() if n not in OWNERS}
    installed_entries = list(entry_points(group="personal_agent.plugins"))
    collisions = {e.name for e in installed_entries} & candidate["plugins"].keys()
    records.update(candidate["plugins"])
    artifacts = [{"path": candidate["path"], "sha256": candidate["sha256"]}]
    replaced = set()
    distributions = set()
    for path in plugin_wheels:
        plugin = read_wheel(path)
        if plugin["distribution"] in distributions or set(plugin["plugins"]) & (
            candidate["plugins"].keys() | replaced
        ):
            raise ValueError("Duplicate or shadowed candidate plugin identity")
        distributions.add(plugin["distribution"])
        for installed in installed_entries:
            metadata = getattr(installed.dist, "metadata", {})
            if canonicalize_name(metadata.get("Name", "")) == plugin["distribution"]:
                records.pop(installed.name, None)
                errors.pop(installed.name, None)
        replaced.update(plugin["plugins"])
        records.update(plugin["plugins"])
        for name in plugin["plugins"]:
            errors.pop(name, None)
        artifacts.append({"path": plugin["path"], "sha256": plugin["sha256"]})
    for installed in installed_entries:
        metadata = getattr(installed.dist, "metadata", {})
        if installed.name in replaced and canonicalize_name(metadata.get("Name", "")) not in distributions:
            errors[installed.name] = "Candidate plugin conflicts with an installed distribution"
    for name in collisions:
        errors[name] = "Installed extension shadows a target bundled plugin"
    result = inspect(data, records, errors, target=candidate["release"], core_names=candidate["core_names"])
    result.update(
        current_core=CORE_VERSION, artifacts=artifacts, restart_required=True, installation_performed=False
    )
    result["changes"] = [
        {"plugin": n, "from": before.get(n, {}).get("version"), "to": records.get(n, {}).get("version")}
        for n in sorted(before.keys() | records.keys())
        if before.get(n, {}).get("version") != records.get(n, {}).get("version")
    ]
    try:
        saved = check(
            root,
            candidate["release"],
            plugins={
                row["name"]: row["version"] for row in result["plugins"] if row["enabled"] and row["version"]
            },
        )
        result["data"] = {
            "state": "compatible" if saved else "untracked",
            "target_format": candidate["release"]["data_format"],
        }
    except CompatibilityError as exc:
        result["data"] = {"state": "blocked", "reason": str(exc)}
        result["blockers"].append({"plugin": None, "reason": str(exc)})
    if version(candidate["release"]["version"]) < version(CORE_VERSION):
        result["blockers"].append(
            {"plugin": None, "reason": "Downgrade requires a matching pre-upgrade backup"}
        )
    result["compatible"] = not result["blockers"]
    result["next_steps"] = [
        "Review in-flight work and stop the instance",
        "Back up the complete instance and original environment",
        "Install the exact checked wheels and resolve locked dependencies",
        "Restart management and business; verify health before resuming work",
    ]
    result["limits"] = [
        "Compatibility preflight does not execute candidate code or verify devices/providers",
        "Configuration and module-owned migrations must be validated in an isolated candidate environment",
        "Untracked legacy data requires a stopped-instance backup before its first upgrade",
    ]
    return result
