"""Static declaration without hardware or firmware-toolchain imports."""

from pathlib import Path

from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.plugins import plugin_platforms
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import SpeechRequest
from assistant_runtime.api.terminal import TerminalNotifications
from .settings import validate


def create(context):
    from .plugin import AiPassportPlugin

    return AiPassportPlugin(context)


DECLARATION = PluginDefinition(
    manifest=plugin_manifest(__file__),
    supported_platforms=plugin_platforms(Path(__file__).parent),
    configuration=configuration_spec(__file__),
    create=create,
    validate=validate,
    resources=("http_frontend",),
    uses=(ConversationRequest, ConversationInputRequest, SpeechRequest, TerminalNotifications),
)
