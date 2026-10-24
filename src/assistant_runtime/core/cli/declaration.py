from assistant_runtime.api import PluginDefinition, Manifest, ConversationRequest


class CliPlugin:
    manifest = Manifest("cli", ("conversation",), critical=True)

    async def start(self, context):
        pass  # Terminal clients attach explicitly through the private operator IPC.


DECLARATION = PluginDefinition(
    CliPlugin.manifest,
    internal_factory=lambda host: CliPlugin(),
    uses=(ConversationRequest,),
)
