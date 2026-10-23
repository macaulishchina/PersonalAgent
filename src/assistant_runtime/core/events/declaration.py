from assistant_runtime.api import PluginDefinition, Manifest
from .settings import EventSettings
from pathlib import Path


def build(host):
    from .plugin import EventsPlugin

    return EventsPlugin(host)


DECLARATION = PluginDefinition(
    Manifest("events", (), critical=False),
    internal_factory=build,
    config_key="events",
    validate=EventSettings.parse,
    skills=(Path(__file__).parent / "skills" / "event-management",),
    service="events",
    tools_factory="assistant_runtime.core.events.tools:create_server",
)
