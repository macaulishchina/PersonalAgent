from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import ModelSettings


def build(host):
    from .plugin import ModelsPlugin

    return ModelsPlugin(host)


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    internal_factory=build,
    config_key="model_api",
    validate=ModelSettings.parse,
    service="models",
    tools_factory="assistant_runtime.core.models.tools:create_server",
)
