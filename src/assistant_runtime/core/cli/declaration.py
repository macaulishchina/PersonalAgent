from assistant_runtime.api import PluginDefinition, plugin_manifest, ConversationRequest


class CliPlugin:
    manifest = plugin_manifest(__file__)

    async def start(self, context):
        pass  # Terminal clients attach explicitly through the private operator IPC.


DECLARATION = PluginDefinition(
    CliPlugin.manifest,
    internal_factory=lambda host: CliPlugin(),
    uses=(ConversationRequest,),
)
