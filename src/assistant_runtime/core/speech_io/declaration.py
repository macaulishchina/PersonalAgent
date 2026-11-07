from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.speech import SpeechRequest, RecognitionRequest, SynthesisRequest


def create(context):
    from .service import SpeechService

    service = SpeechService(context)
    service.manifest = DECLARATION.manifest
    return service


DECLARATION = PluginDefinition(
    manifest=plugin_manifest(__file__),
    create=create,
    supported_platforms=("linux",),
    config_key="speech_io",
    configuration=configuration_spec(__file__),
    provides=(SpeechRequest,),
    uses=(RecognitionRequest, SynthesisRequest),
)
