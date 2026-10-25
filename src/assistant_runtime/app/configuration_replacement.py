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
    if name == "pipewire" and old.devices == config.devices:
        # A volume policy applies to the next volume operation without closing ongoing streams.
        from assistant_runtime.core.configuration.updates import attributes

        plugin = runtime.plugins[name]
        changes = [(plugin.route.config, "minimum_volume", config.minimum_volume)]
        provider = getattr(getattr(host, "audio_outputs", None), "providers", {}).get(old.default_output)
        if provider is not None and hasattr(provider, "minimum_volume"):
            changes.append((provider, "minimum_volume", config.minimum_volume))
        return attributes(changes)

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
                host.assistant.route = host.audio_plugin.route
                host.assistant.player = host.audio_plugin.player
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
