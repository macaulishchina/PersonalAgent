from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import IntegrationSettings


def build(host):
    from .plugin import IntegrationsPlugin

    return IntegrationsPlugin(host, host.mcp_registry, lambda: host.backend.integrations)


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
    config_key="external_mcp",
    validate=IntegrationSettings.parse,
    service="integrations",
    tools_factory="assistant_runtime.core.integrations.tools:create_server",
    background_tools=("list_mcp_servers", "inspect_mcp_server", "verify_mcp_server"),
)
