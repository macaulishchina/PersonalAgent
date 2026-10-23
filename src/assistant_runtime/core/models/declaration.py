from assistant_runtime.api import PluginDefinition, Manifest
from .settings import ModelSettings


def build(host):
    from .plugin import ModelsPlugin

    return ModelsPlugin(host)


DECLARATION = PluginDefinition(
    Manifest("models", ("ai_usage",), optional=("workfiles",), critical=False),
    internal_factory=build,
    config_key="model_api",
    validate=ModelSettings.parse,
    service="models",
    tools_factory="assistant_runtime.core.models.tools:create_server",
)
