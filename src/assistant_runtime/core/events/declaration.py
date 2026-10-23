from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import EventSettings
from pathlib import Path


def build(host):
    from .plugin import EventsPlugin

    return EventsPlugin(host)


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=Manifest("events", (), critical=False),
    internal_factory=build,
    config_key="events",
    validate=EventSettings.parse,
    skills=(Path(__file__).parent / "skills" / "event-management",),
    service="events",
    tools_factory="assistant_runtime.core.events.tools:create_server",
)
