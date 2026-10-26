from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec


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
        lambda: bool(host.assistant and host.assistant.active)
        or host.backend.busy
        or bool(
            getattr(host, "conversations", None)
            and any(chat.backend.busy or chat.queue() for chat in host.conversations.opened.values())
        ),
    )


DECLARATION = PluginDefinition(
    plugin_manifest(__file__),
    internal_factory=build,
    configuration=configuration_spec(__file__),
    service="memory",
    tools_factory="assistant_runtime.core.memory.tools:create_server",
    task_context=False,
)
