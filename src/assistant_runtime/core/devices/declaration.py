from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec


def build(host):
    return host.device_services.plugin


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
    configuration=configuration_spec(__file__),
    service="device",
    tools_factory="assistant_runtime.core.devices.tools:create_server",
    task_context=False,
)
