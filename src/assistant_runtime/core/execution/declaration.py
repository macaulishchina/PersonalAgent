from assistant_runtime.api.configuration import configuration_default, configuration_spec
from assistant_runtime.api import Manifest, PluginDefinition, ExecutionRequest


def settings(raw):
    if not isinstance(raw, dict) or set(raw) - {"concurrency", "queue_size"}:
        raise ValueError("Unknown execution settings")
    result = {
        name: raw.get(name, configuration_default(__file__, name)) for name in ("concurrency", "queue_size")
    }
    for name, upper in (("concurrency", 8), ("queue_size", 1024)):
        if type(result[name]) is not int or not 1 <= result[name] <= upper:
            raise ValueError(f"Execution {name} must be 1..{upper}")
    return result


def build(host):
    from .plugin import ExecutionPlugin

    return ExecutionPlugin(host)


DECLARATION = PluginDefinition(
    Manifest("execution", ("pi", "ai_usage", "workfiles"), critical=True),
    internal_factory=build,
    configuration=configuration_spec(__file__),
    provides=(ExecutionRequest,),
    config_key="execution",
    validate=settings,
)
