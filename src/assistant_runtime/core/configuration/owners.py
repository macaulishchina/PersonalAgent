"""Owner validation shared by startup and configuration candidate validation."""

from assistant_runtime.api.configuration import ConfigurationSpec
from assistant_runtime.core.plugin_catalog import OWNERS, declaration
from .catalog import catalog
from .schema import resolve, validate_structure


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
                getattr(config, namespace, {})
                if namespace
                else {key: getattr(config, key) for key in description["schema"].get("properties", {})}
            )
            if item["id"] not in config.enabled_plugins:
                validate_structure(ConfigurationSpec(**description), value)
                continue
            value = resolve(ConfigurationSpec(**description), value)
            if namespace:
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
            if spec.configuration is None or dict(spec.configuration.schema) != description["schema"]:
                raise ValueError("Plugin runtime and packaged configuration descriptions differ")
            resolve_definition(spec, value)
    known = {item["id"] for item in inventory["plugins"]}
    if (set(config.plugin_settings) - known) & set(config.enabled_plugins):
        raise ValueError("Configuration references a plugin that is not installed")
