from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    return host.device_services.plugin


DECLARATION = PluginDefinition(
    Manifest("devices", (), critical=False),
    internal_factory=build,
    service="device",
    tools_factory="assistant_runtime.core.devices.tools:create_server",
    task_context=False,
)
