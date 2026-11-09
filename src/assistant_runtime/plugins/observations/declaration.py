from pathlib import Path
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, plugin_manifest

from assistant_runtime.api.contracts import EventRequest, TaskRequest, ObservationRequest


def create(api):
    from .plugin import ObservationsPlugin

    return ObservationsPlugin(api, api.resource("observation_providers"))


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=plugin_manifest(__file__),
    configuration=configuration_spec(__file__),
    create=create,
    uses=(
        EventRequest,
        TaskRequest,
    ),
    provides=(ObservationRequest,),
    resources=("registry_root", "observation_providers"),
)
