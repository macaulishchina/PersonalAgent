from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, plugin_manifest
from .settings import SearchSettings

from assistant_runtime.api.contracts import TaskRequest, SearchRequest


def create(api):
    from .plugin import SearchPlugin

    return SearchPlugin(api)


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    manifest=plugin_manifest(__file__),
    create=create,
    uses=(TaskRequest,),
    provides=(SearchRequest,),
    resources=("usage",),
    config_key="search",
    validate=SearchSettings.parse,
    service="search",
    tools_factory="assistant_runtime.plugins.search.tools:create_server",
)
