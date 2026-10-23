from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api import PluginDefinition, Manifest
from .settings import TaskSettings
from pathlib import Path


def build(host):
    from .plugin import TasksPlugin

    from assistant_runtime.core.pi.executor import TaskExecutor

    host.task_executor = lambda notify: TaskExecutor(host.backend, notify)
    return TasksPlugin(host)


DECLARATION = PluginDefinition(
    configuration=configuration_spec(__file__),
    manifest=Manifest(
        "tasks",
        ("execution", "pi", "notifications", "workfiles"),
        optional=("conversation", "devices"),
        critical=False,
    ),
    internal_factory=build,
    config_key="tasks",
    validate=TaskSettings.parse,
    skills=(Path(__file__).parent / "skills" / "event-driven-tasks",),
    service="tasks",
    tools_factory="assistant_runtime.core.tasks.tools:create_server",
)
