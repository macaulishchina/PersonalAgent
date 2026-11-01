from assistant_runtime.api.ai_usage import UsageRequest
from assistant_runtime.api.contracts import TaskRequest, ConversationFeedbackRequest, NotificationRequest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import UsageSettings


def build(host):
    from .plugin import AIUsagePlugin

    host.ai_usage = AIUsagePlugin(host)
    return host.ai_usage


DECLARATION = PluginDefinition(
    provides=(UsageRequest,),
    uses=(
        TaskRequest,
        ConversationFeedbackRequest,
        NotificationRequest,
    ),
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
    config_key="ai_usage",
    validate=UsageSettings.parse,
    service="ai_usage",
    tools_factory="assistant_runtime.core.ai_usage.tools:create_server",
)
