"""Validate an isolated file candidate through the same owners as application startup."""

from pathlib import Path
import tempfile
from types import SimpleNamespace

from assistant_runtime.app.config import Config
from assistant_runtime.app.extensions import discover
from assistant_runtime.core.plugin_catalog import OWNERS, declaration
from assistant_runtime.kernel.runtime import Runtime
from .schema import resolve_definition


def validate_candidate(entry_name, contents, *, root, management=None, entry_directory=None):
    import tomllib
    import tomlkit

    original = tomllib.loads(contents[entry_name].decode())
    directory = Path(entry_directory or root).resolve()
    declared = Path(original.get("root", str(directory))).expanduser()
    actual = (directory / declared).resolve() if not declared.is_absolute() else declared.resolve()
    if actual != Path(root).resolve():
        raise ValueError("Changing instance root requires a data migration")
    with tempfile.TemporaryDirectory(prefix="personal-agent-config-") as folder:
        candidate = Path(folder)
        for name, content in contents.items():
            path = candidate / name
            if Path(name).is_absolute() or not path.resolve().is_relative_to(candidate):
                raise ValueError("Invalid candidate file path")
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == entry_name:
                document = tomlkit.parse(content.decode())
                document["root"] = str(root)
                content = tomlkit.dumps(document).encode()
            path.write_bytes(content)
            path.chmod(0o600)
        config = Config.load(candidate / entry_name)
        config.root = Path(root).resolve()
        external = [name for name in config.enabled_plugins if name not in OWNERS]
        specs = {name: declaration(name) for name in config.enabled_plugins if name in OWNERS}
        specs.update(discover(external, OWNERS) if external else {})
        graph = Runtime(None)
        for name, spec in specs.items():
            graph.add(SimpleNamespace(manifest=spec.manifest))
            if spec.validate or spec.configuration:
                if spec.config_key:
                    value = getattr(config, spec.config_key)
                elif name not in OWNERS:
                    value = config.plugin_settings.get(name, {})
                else:
                    continue
                resolve_definition(spec, value)
        graph.order()
        for binding, owner in config.bindings.items():
            # Existing optional bindings may point at disabled owners; startup ignores them.
            if owner in config.enabled_plugins and owner not in specs:
                raise ValueError("Unavailable capability binding: " + binding)
        from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort

        models = ModelSettings.parse(config.model_api)
        if models.enabled:
            if config.main_model != "unconfigured":
                if config.main_model not in models.entries:
                    raise ValueError("Main model alias is not configured")
                reasoning_effort(models.entries[config.main_model], config.main_reasoning_effort)
        validate_ports(config, management)
        return config


def validate_ports(config, management=None):
    """Reject known TCP bind collisions without opening sockets or probing devices."""
    import ipaddress

    listeners = []
    if management:
        listeners.append(("management", management.host, management.port))
    from assistant_runtime.api.configuration import ConfigurationSpec
    from .catalog import catalog
    from .schema import resolve

    for plugin in catalog(external_names=set(config.enabled_plugins) - set(OWNERS))["plugins"]:
        description = plugin.get("configuration")
        if plugin["id"] not in config.enabled_plugins or not description or "error" in description:
            continue
        namespace = description["namespace"]
        raw = (
            config.plugin_settings.get(plugin["id"], {})
            if namespace.startswith("plugin_settings.")
            else getattr(config, namespace, {})
        )
        values = resolve(ConfigurationSpec(**description), raw)
        for endpoint in description["schema"].get("x-listeners", []):
            if endpoint.get("enabled") and not values.get(endpoint["enabled"], False):
                continue
            listeners.append((plugin["id"], values[endpoint["host"]], values[endpoint["port"]]))
    for index, (owner, host, port) in enumerate(listeners):
        address = ipaddress.ip_address(host)
        for other, peer, number in listeners[:index]:
            previous = ipaddress.ip_address(peer)
            if port == number and (address == previous or address.is_unspecified or previous.is_unspecified):
                raise ValueError(f"Listener conflict: {owner} and {other} on port {port}")
