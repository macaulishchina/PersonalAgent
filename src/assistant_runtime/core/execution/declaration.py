from assistant_runtime.api import plugin_manifest
from assistant_runtime.api.configuration import configuration_default, configuration_spec
from assistant_runtime.api import PluginDefinition, ExecutionRequest


def settings(raw):
    if not isinstance(raw, dict) or set(raw) - {"concurrency", "queue_size", "controllers"}:
        raise ValueError("Unknown execution settings")
    result = {
        name: raw.get(name, configuration_default(__file__, name)) for name in ("concurrency", "queue_size")
    }
    for name, upper in (("concurrency", 8), ("queue_size", 1024)):
        if type(result[name]) is not int or not 1 <= result[name] <= upper:
            raise ValueError(f"Execution {name} must be 1..{upper}")
    import re

    controllers = raw.get("controllers", {})
    if not isinstance(controllers, dict) or len(controllers) > 32:
        raise ValueError("Execution controllers must be a bounded service-to-owner mapping")
    for principal, owners in controllers.items():
        if not isinstance(principal, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", principal):
            raise ValueError("Execution controller must identify an installed plugin")
        if (
            not isinstance(owners, list)
            or not owners
            or len(owners) > 256
            or any(
                not isinstance(owner, str)
                or not owner
                or owner == "*"
                or len(owner.encode()) > 200
                or any(ord(c) < 32 for c in owner)
                for owner in owners
            )
            or len(owners) != len(set(owners))
        ):
            raise ValueError("Execution controller requires explicit unique resource owners")
    result["controllers"] = {key: list(value) for key, value in controllers.items()}
    return result


def build(host):
    from .plugin import ExecutionPlugin

    return ExecutionPlugin(host)


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
    configuration=configuration_spec(__file__),
    provides=(ExecutionRequest,),
    config_key="execution",
    validate=settings,
)
