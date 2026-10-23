from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import UsageSettings


def build(host):
    from .plugin import AIUsagePlugin

    host.ai_usage = AIUsagePlugin(host)
    return host.ai_usage


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=Manifest("ai_usage", (), critical=False),
    internal_factory=build,
    config_key="ai_usage",
    validate=UsageSettings.parse,
    service="ai_usage",
    tools_factory="assistant_runtime.core.ai_usage.tools:create_server",
)
