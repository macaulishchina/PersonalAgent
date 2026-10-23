from assistant_runtime.api import Manifest, PluginDefinition, ExecutionRequest


def settings(raw):
    if not isinstance(raw, dict) or set(raw) - {"concurrency", "queue_size"}:
        raise ValueError("Unknown execution settings")
    result = {"concurrency": 4, "queue_size": 64, **raw}
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
    provides=(ExecutionRequest,),
    config_key="execution",
    validate=settings,
)
