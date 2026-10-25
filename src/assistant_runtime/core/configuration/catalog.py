"""Static configuration inventory: never construct a plugin to display its settings."""

import json
from pathlib import Path
from functools import lru_cache
from copy import deepcopy
from importlib.metadata import entry_points
from assistant_runtime.core.plugin_catalog import OWNERS, platforms, backends
from assistant_runtime.app.plugin_metadata import installed_platforms, installed_backends
from assistant_runtime.app.versions import compatibility, framework


def release_status(read):
    try:
        data = read()
        error = compatibility(data)
        return {
            "version": data["version"],
            "requires_core": data["requires_core"],
            "version_compatible": not error,
            "version_error": error,
            "requires": list(data["requires"]),
            "dependency_versions": {**data["requires"], **data["optional"]},
        }
    except (ValueError, OSError, TypeError, KeyError):
        return {
            "version": None,
            "version_compatible": False,
            "version_error": "缺少或无效的版本声明，请更新插件",
        }


def platform_status(read):
    from assistant_runtime.api.system import system

    try:
        certified = read()
    except ValueError as exc:
        return {"supported_platforms": [], "platform_supported": False, "platform_error": str(exc)}
    return {"supported_platforms": list(certified), "platform_supported": system.name in certified}


def backend_status(read):
    try:
        policy = read()
        return {"backends": policy.describe() if policy else None}
    except (ValueError, TypeError, OSError) as exc:
        return {"backends": None, "backend_error": str(exc), "platform_supported": False}


@lru_cache(maxsize=128)
def _read_description(path):
    content = path.read_text(encoding="utf-8")
    if len(content.encode()) > 256 * 1024:
        raise ValueError("Configuration description exceeds 256 KiB")
    description = json.loads(content)
    if description.get("version") != 1:
        return {"error": "Unsupported configuration specification version"}
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(description["schema"])
    from .schema import expand

    expand(description["schema"])
    return description


def read_description(path):
    # Package metadata is immutable during a process; installing upgrades requires restart.
    return deepcopy(_read_description(path))


def installed_descriptions(names=None):
    from assistant_runtime.app.plugin_metadata import installed_release

    result = []
    seen = set()
    for entry in entry_points(group="personal_agent.plugins"):
        if names is not None and entry.name not in names:
            continue
        if entry.name in OWNERS or entry.name in seen:
            problem = {"error": "Ambiguous installed plugin identity"}
            previous = next((item for item in result if item["id"] == entry.name), None)
            if previous:
                previous.update(configuration=problem, description_available=False, platform_supported=False)
            else:
                result.append(
                    {
                        "id": entry.name,
                        "kind": "extension",
                        "configuration": problem,
                        "description_available": False,
                        "supported_platforms": [],
                        "platform_supported": False,
                    }
                )
            continue
        seen.add(entry.name)
        description = None
        resource = entry.value.split(":")[0].replace(".", "/") + "/configuration.json"
        if entry.dist:
            matches = [f for f in entry.dist.files or () if str(f) == resource]
            if matches:
                from jsonschema import SchemaError

                try:
                    description = read_description(entry.dist.locate_file(matches[0]))
                except (ValueError, OSError, TypeError, KeyError, SchemaError):
                    description = {"error": "Invalid packaged configuration description"}
                # An external package owns only its own configuration namespace.
                description["namespace"] = "plugin_settings." + entry.name
        result.append(
            {
                "id": entry.name,
                "kind": "extension",
                "configuration": description,
                "description_available": description is not None,
                "requires": description.get("requires", []) if description else [],
                **platform_status(lambda: installed_platforms(entry)),
                **backend_status(lambda: installed_backends(entry)),
                **release_status(lambda: installed_release(entry)),
            }
        )
    return result


def catalog(*, external_names=None):
    from assistant_runtime.core.plugin_catalog import release

    root = Path(__file__).resolve().parents[2]
    result = []
    for name, module in OWNERS.items():
        relative = module.removeprefix("assistant_runtime.").split(".")
        path = root.joinpath(*relative, "configuration.json")
        description = read_description(path) if path.is_file() else None
        result.append(
            {
                "id": name,
                "kind": "core" if module.startswith("assistant_runtime.core.") else "extension",
                "configuration": description,
                "description_available": description is not None,
                "requires": description.get("requires", []) if description else [],
                **platform_status(lambda: platforms(name)),
                **backend_status(lambda: backends(name)),
                **release_status(lambda: release(name)),
            }
        )
    installed = installed_descriptions(external_names)
    from assistant_runtime.app.presets import default_plugins

    application = read_description(root / "app/configuration.json")
    application["schema"]["properties"]["plugins"]["default"] = default_plugins()
    inventory = {
        "application": application,
        "plugins": result + [item for item in installed if item["id"] not in OWNERS],
        "issues": [
            {"id": item["id"], "error": item["configuration"]["error"]}
            for item in installed
            if item.get("configuration") and "error" in item["configuration"]
        ]
        + [
            {"id": item["id"], "error": item["platform_error"]}
            for item in result + installed
            if item.get("platform_error")
        ]
        + [
            {"id": item["id"], "error": item["backend_error"]}
            for item in result + installed
            if item.get("backend_error")
        ],
    }
    from assistant_runtime.api.system import system

    inventory["platform"] = system.name
    inventory["core_release"] = framework()
    from .interface import describe

    inventory["pages"] = describe(inventory)
    return inventory


def specifications(inventory=None):
    inventory = inventory or catalog()
    return [
        inventory["application"],
        *(
            p["configuration"]
            for p in inventory["plugins"]
            if p.get("configuration") and "error" not in p["configuration"]
        ),
    ]


def describe_definition(spec):
    description = spec.configuration
    return {
        "id": spec.manifest.name,
        "key": spec.config_key,
        "schema": dict(description.schema) if description else {"type": "object"},
        "requires": list(spec.manifest.requires),
        "optional": list(spec.manifest.optional),
        "supported_platforms": list(spec.supported_platforms),
        "title": description.title if description else spec.manifest.name,
        "group": description.group if description else "extensions",
        "sensitive": list(description.sensitive) if description else [],
        "read_only": list(description.read_only) if description else [],
        "application": "restart",
    }
