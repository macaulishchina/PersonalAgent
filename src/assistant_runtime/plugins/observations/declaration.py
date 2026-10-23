from assistant_runtime.api import PluginDefinition, Manifest

from assistant_runtime.api.contracts import EventRequest, TaskRequest, ObservationRequest


def create(api):
    from .plugin import ObservationsPlugin

    return ObservationsPlugin(api, api.resource("observation_providers"))


DECLARATION = PluginDefinition(
    Manifest("observations", ("events",), critical=False),
    create=create,
    uses=(
        EventRequest,
        TaskRequest,
    ),
    provides=(ObservationRequest,),
    resources=("registry_root", "observation_providers"),
)
