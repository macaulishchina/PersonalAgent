from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import SearchSettings

from assistant_runtime.api.contracts import TaskRequest, SearchRequest


def create(api):
    from .plugin import SearchPlugin

    return SearchPlugin(api)


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=Manifest("search", ("ai_usage",), critical=False),
    create=create,
    uses=(TaskRequest,),
    provides=(SearchRequest,),
    resources=("usage",),
    config_key="search",
    validate=SearchSettings.parse,
    service="search",
    tools_factory="assistant_runtime.plugins.search.tools:create_server",
)
