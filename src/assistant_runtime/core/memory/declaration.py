from assistant_runtime.api import PluginDefinition, Manifest


def build(host):
    from .service import MemoryPlugin
    from .mem0 import Mem0Memory
    from .inference import complete

    return MemoryPlugin(
        host.config,
        host.store,
        Mem0Memory(
            host.config.state_dir / "mem0",
            host.config.models / "memory-embedding",
            lambda messages, schema: complete(host, messages, schema),
            host.config.workspace,
        ),
        lambda: bool(host.assistant and host.assistant.active) or host.backend.busy,
    )


DECLARATION = PluginDefinition(
    Manifest("memory", (), critical=False),
    internal_factory=build,
    service="memory",
    tools_factory="assistant_runtime.core.memory.tools:create_server",
    task_context=False,
)
