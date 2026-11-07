"""Bind only an owner's declared configuration, repositories and public resources."""

from copy import deepcopy
from types import SimpleNamespace
from assistant_runtime.core.plugin_catalog import declaration
from .plugin_context import (
    environment,
    BoundExtension,
    UsageAccess,
    ScopedState,
    DeviceRegistration,
    ResourceRegistration,
)

# Provisioned before startup. This is availability, not a reverse lifecycle edge:
# devices waits for its selected adapters to register during their own startup.
RESOURCE_OWNERS = {
    "usage": "ai_usage",
    "conversation": "conversation",
    **{
        key: "devices"
        for key in (
            "directory",
            "voice_binding",
            "audio_outputs",
            "audio_inputs",
            "video_inputs",
            "camera_controls",
        )
    },
}


def settings_for(config, name, spec):
    raw = getattr(config, spec.config_key) if spec.config_key else {}
    settings = spec.validate(raw) if spec.validate else raw
    if spec.config_fields or name in {"airplay", "go2rtc", "v4l2"}:
        values = {key: deepcopy(getattr(config, key)) for key in spec.config_fields}
        values["devices"] = deepcopy(
            {
                key: row
                for key, row in config.devices.items()
                if row["adapter"] == name
                or name == "pipewire"
                and key in {config.input_device, config.default_output}
            }
        )
        values["output_devices"] = {
            key: row
            for key, row in values["devices"].items()
            if row["kind"] == "speaker" and row.get("enabled", True)
        }
        settings = SimpleNamespace(**values)
    return settings


def dependencies(host, name):
    spec = declaration(name)
    settings = settings_for(host.config, name, spec)
    resources = resources_for(host, name, spec.resources)
    credentials = (
        tuple(row["api_key_env"] for row in settings.providers.values())
        if name == "search"
        else ("TYPESAFE_API_KEY",)
        if name == "decisions"
        else spec.credentials
    )
    from assistant_runtime.api.backends import select_backend

    backend = select_backend(
        name, spec.backends, host.runtime.platform.name, host.config.plugin_backends.get(name, {})
    )
    return environment(
        host,
        name,
        settings,
        resources=resources,
        uses=spec.uses,
        provides=spec.provides,
        credentials=credentials,
        backend=backend,
    )


def resources_for(host, name, keys):
    resources = {}
    for key in keys:
        required = RESOURCE_OWNERS.get(key)
        attribute = {"usage": "ai_usage", "directory": "devices", "voice_binding": "voice_io"}.get(key, key)
        if required and getattr(host, attribute, None) is None:
            raise ValueError(f"Plugin {name}: resource {key} requires enabled plugin {required}")
        if key == "system_io":
            from assistant_runtime.api.system import system

            resources[key] = system
        elif key == "http_frontend":
            from .http_frontends import scoped

            resources[key] = scoped(host, name)
        elif key == "execution_control":
            from assistant_runtime.api.control import ExecutionControl

            async def control(action, arguments, principal=name):
                service = getattr(host.backend, "executions", None)
                if service is None:
                    raise RuntimeError("Execution control is unavailable")
                return await service.control(principal, action, arguments)

            resources[key] = ExecutionControl(control)
        elif key == "usage":
            usage = host.ai_usage
            resources[key] = UsageAccess(usage.scope, usage.service.admit, usage.service.settle)
        elif key == "conversation":
            from assistant_runtime.core.conversation.access import ConversationAccess

            resources[key] = ConversationAccess(
                lambda: host.conversation,
                lambda messages: host.notifications.project_chat(messages)
                if getattr(host, "notifications", None)
                else None,
            )
        elif key in {"notifications_enabled", "tasks_enabled"}:
            resources[key] = key.removesuffix("_enabled") in host.config.plugins
        elif key == "registry_root":
            resources[key] = host.config.root
        elif key == "observation_providers":
            from assistant_runtime.plugins.reyee.events import ReyeeEvents
            from assistant_runtime.plugins.adb_tv.events import TelevisionEvents

            resources[key] = {
                "reyee": ReyeeEvents(host.config.state_dir / "observations"),
                "adb_tv": TelevisionEvents(),
            }
            if host.assistant is not None:
                from assistant_runtime.core.speech.wake_events import WakeEvents

                resources[key]["wake"] = WakeEvents(host.assistant)
        elif key == "state":
            resources[key] = ScopedState(
                host.store.get,
                host.store.set,
                keys=("volume",) if name == "pipewire" else (),
                prefixes=("sensors.apple_home.",) if name == "apple_home" else (),
            )
        elif key == "directory":
            resources[key] = DeviceRegistration(host.devices, name)
        elif key == "voice_binding":
            resources[key] = host.voice_io
        elif key in {"audio_outputs", "audio_inputs", "video_inputs", "camera_controls"}:
            resources[key] = ResourceRegistration(
                getattr(host, key),
                name,
                (host.config.input_device, host.config.default_output) if name == "pipewire" else (),
            )
        else:
            raise ValueError("Unknown public resource: " + key)
    return resources


def context_for(host, name):
    return dependencies(host, name)[0]


def create(host, name, factory=None):
    public, attach = dependencies(host, name)
    plugin = BoundExtension((factory or declaration(name).create)(public), public, attach)
    from .configuration_replacement import REPLACEABLE, prepare

    if name in REPLACEABLE and hasattr(plugin, "maintenance_pending"):
        plugin.prepare_configuration = lambda config: prepare(host, name, config)
    return plugin
