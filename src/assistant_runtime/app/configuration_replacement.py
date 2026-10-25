"""Local replacement for bundled owners with an explicit drain contract."""

from assistant_runtime.api.configuration import ConfigurationPending, ConfigurationUpdate
from assistant_runtime.api.configuration_io import resource_view
import os

REPLACEABLE = frozenset(
    {
        "search",
        "media",
        "decisions",
        "apple_home",
        "observations",
        "hid_buttons",
        "airplay",
        "go2rtc",
        "v4l2",
        "pipewire",
    }
)


async def prepare(host, name, config):
    old = host.config
    previous_manifest = os.environ.get("PERSONAL_AGENT_RESOURCE_MANIFEST")
    runtime = host.runtime
    plugin = runtime.plugins[name]
    callback = getattr(getattr(plugin, "instance", None), "prepare_configuration", None)
    if callback:
        from assistant_runtime.api.backends import select_backend
        from assistant_runtime.api.configuration import scoped_update
        from assistant_runtime.core.plugin_catalog import declaration
        from .bundled import settings_for

        spec = declaration(name)
        backend = select_backend(
            name, spec.backends, runtime.platform.name, config.plugin_backends.get(name, {})
        )
        if backend == plugin.public.backend:
            value = settings_for(config, name, spec)
            update = await callback(value)
            if update is not NotImplemented:
                return scoped_update(update, plugin.public, value)

    owned_devices = {
        ident
        for settings in (old, config)
        for ident, row in settings.devices.items()
        if row["adapter"] == name
    }
    if name == "pipewire":
        owned_devices.update((old.input_device, old.default_output))

    def busy():
        plugin = runtime.plugins[name]
        context = runtime.contexts[name]
        leases = getattr(getattr(host, "device_resources", None), "held", {})
        assistant = getattr(host, "assistant", None)
        voice_busy = (
            name == "pipewire"
            and assistant is not None
            and (assistant.active or assistant.transition.locked())
        )
        return (
            voice_busy
            or plugin.maintenance_pending()
            or any(not call.done() for call in context.calls)
            or any(ident in owned_devices for ident, resource in leases)
        )

    if busy():
        raise ConfigurationPending()

    def factory(candidate):
        from .bundled import create

        before = host.config
        try:
            host.config = candidate
            return create(host, name)
        finally:
            host.config = before

    attempted = False

    async def replace(candidate):
        # Registrations during startup must see this owner's candidate device definitions.
        # Public admission is suspended by the update coordinator until all owners commit.
        bindings = []
        for attr, key in (
            ("devices", "config"),
            ("audio_inputs", "devices"),
            ("audio_outputs", "devices"),
            ("video_inputs", "devices"),
            ("camera_controls", "devices"),
        ):
            target = getattr(host, attr, None)
            if target is not None:
                bindings.append((target, key, getattr(target, key)))
                setattr(target, key, candidate.devices)
        try:
            await runtime.replace(name, lambda: factory(candidate))
            if name == runtime.bindings.get("audio") and getattr(host, "assistant", None) is not None:
                host.audio_plugin = runtime.plugins[name]
                host.assistant.route = getattr(host.audio_plugin, "route", None)
                host.assistant.player = getattr(host.audio_plugin, "player", None)
            devices = runtime.plugins.get("devices")
            if devices is not None:
                devices.register_operations()
        finally:
            for target, key, value in bindings:
                setattr(target, key, value)

    async def commit():
        nonlocal attempted
        if busy():
            raise ConfigurationPending()
        attempted = True
        await replace(config)

    async def rollback():
        if attempted:
            with resource_view(previous_manifest):
                await replace(old)

    return ConfigurationUpdate(commit, rollback)
