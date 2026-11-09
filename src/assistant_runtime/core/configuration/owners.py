"""Owner validation shared by startup and configuration candidate validation."""

from assistant_runtime.api.configuration import ConfigurationSpec
from assistant_runtime.core.plugin_catalog import OWNERS, declaration
from .catalog import catalog
from .schema import resolve, validate_structure


def check_definition(spec, description):
    if spec.configuration is None or dict(spec.configuration.schema) != description["schema"]:
        raise ValueError("Plugin runtime and packaged configuration descriptions differ")
    if description.get("interface", {}).get("page"):
        from dataclasses import asdict
        import json

        # Page ownership, action permissions and resources must be the same contract.
        def encoded(value):
            return json.dumps(asdict(value), sort_keys=True)

        if encoded(spec.configuration) != encoded(ConfigurationSpec(**description)):
            raise ValueError("Plugin page and runtime configuration contracts differ")


def validate_stored(config, external_resolver):
    selected = (set(config.plugin_settings) | set(config.enabled_plugins)) - set(OWNERS)
    inventory = catalog(external_names=selected)
    # Dynamic paths are supplied by the installation, not JSON defaults.
    raw = {
        name: getattr(config, name)
        for name in inventory["application"]["schema"]["properties"]
        if name not in {"root"}
    }
    resolve(ConfigurationSpec(**inventory["application"]), raw)
    for item in inventory["plugins"]:
        description = item.get("configuration")
        if item["id"] in OWNERS:
            if not description:
                continue
            namespace = description["namespace"]
            value = (
                config.plugin_settings.get(item["id"], {})
                if namespace.startswith("plugin_settings.")
                else getattr(config, namespace, {})
                if namespace
                else {key: getattr(config, key) for key in description["schema"].get("properties", {})}
            )
            if item["id"] not in config.enabled_plugins:
                validate_structure(ConfigurationSpec(**description), value)
                continue
            value = resolve(ConfigurationSpec(**description), value)
            if namespace.startswith("plugin_settings."):
                config.plugin_settings[item["id"]] = value
            elif namespace:
                setattr(config, namespace, value)
            else:
                for key, item_value in value.items():
                    setattr(config, key, item_value)
            spec = declaration(item["id"])
            if spec.validate:
                spec.validate(value)
        else:
            value = config.plugin_settings.get(item["id"])
            if value is None:
                continue
            if item["id"] not in config.enabled_plugins:
                # Keep disabled extension settings without importing its runtime.
                # Uncertified owners must be removable without deleting private settings.
                if description and "error" not in description:
                    validate_structure(ConfigurationSpec(**description), value)
                continue
            if not description or "error" in description:
                raise ValueError("Installed plugin has no supported configuration description")
            value = resolve(ConfigurationSpec(**description), value)
            config.plugin_settings[item["id"]] = value
            # Explicitly configured owners must also satisfy their semantic validator.
            from .schema import resolve_definition

            spec = external_resolver([item["id"]], OWNERS)[item["id"]]
            check_definition(spec, description)
            resolve_definition(spec, value)
    known = {item["id"] for item in inventory["plugins"]}
    if (set(config.plugin_settings) - known) & set(config.enabled_plugins):
        raise ValueError("Configuration references a plugin that is not installed")


def validate_changed(contents, previous):
    """Changed dormant settings receive full validation too, without starting plugins."""
    import tomllib
    from .fields import merge, lookup
    from .schema import resolve_definition
    from assistant_runtime.app.extensions import discover

    def combined(files):
        data = {}
        for raw in files.values():
            merge(data, tomllib.loads(raw.decode()))
        return data

    candidate = combined(contents)
    try:
        before = combined(previous)
    except tomllib.TOMLDecodeError:
        before = {}  # Repair validates every new owner, even when the previous file is damaged.
    for item in catalog()["plugins"]:
        description = item.get("configuration") or {}
        namespace = description.get("namespace", "")
        if not namespace or "error" in description:
            continue
        path = namespace.split(".")
        raw = lookup(candidate, path, {})
        if raw == lookup(before, path, {}):
            continue
        if item["id"] in candidate.get("plugins", []):
            continue  # Config.load already ran enabled owner validation.
        spec = declaration(item["id"]) if item["id"] in OWNERS else discover([item["id"]], OWNERS)[item["id"]]
        check_definition(spec, description)
        resolve_definition(spec, raw)
