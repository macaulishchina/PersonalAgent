from pathlib import Path
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api import PluginDefinition, Manifest

from assistant_runtime.api.contracts import EventRequest, TaskRequest, ObservationRequest


def create(api):
    from .plugin import ObservationsPlugin

    return ObservationsPlugin(api, api.resource("observation_providers"))


DECLARATION = PluginDefinition(
    supported_platforms=plugin_platforms(Path(__file__).parent),
    manifest=Manifest("observations", ("events",), critical=False),
    create=create,
    uses=(
        EventRequest,
        TaskRequest,
    ),
    provides=(ObservationRequest,),
    resources=("registry_root", "observation_providers"),
)
