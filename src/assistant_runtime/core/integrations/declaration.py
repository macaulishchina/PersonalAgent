from assistant_runtime.api import PluginDefinition, Manifest
from .settings import IntegrationSettings


def build(host):
    from .plugin import IntegrationsPlugin

    return IntegrationsPlugin(host, host.mcp_registry, lambda: host.backend.integrations)


DECLARATION = PluginDefinition(
    Manifest("integrations", (), critical=False),
    internal_factory=build,
    config_key="external_mcp",
    validate=IntegrationSettings.parse,
    service="integrations",
    tools_factory="assistant_runtime.core.integrations.tools:create_server",
    background_tools=("list_mcp_servers", "inspect_mcp_server", "verify_mcp_server"),
)
