from assistant_runtime.api import PluginDefinition, Manifest
from .settings import DecisionSettings

from assistant_runtime.api.contracts import (
    TaskRequest,
    NotificationRequest,
    DecisionRequest,
    DecisionQuery,
    DecisionOutcome,
)


def create(api):
    from .plugin import DecisionsPlugin

    return DecisionsPlugin(api)


DECLARATION = PluginDefinition(
    Manifest("decisions", ("ai_usage",), critical=False),
    create=create,
    uses=(
        TaskRequest,
        NotificationRequest,
    ),
    provides=(
        DecisionRequest,
        DecisionQuery,
        DecisionOutcome,
    ),
    resources=("usage",),
    config_key="decisions",
    validate=DecisionSettings.parse,
    service="decisions",
    tools_factory="assistant_runtime.plugins.decisions.tools:create_server",
)
