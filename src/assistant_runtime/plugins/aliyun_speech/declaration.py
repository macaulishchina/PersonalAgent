from assistant_runtime.api import PluginDefinition, plugin_manifest
from assistant_runtime.api.configuration import configuration_spec
from assistant_runtime.api.speech import RecognitionRequest, SynthesisRequest


def create(context):
    from .plugin import AliyunSpeech

    return AliyunSpeech(context)


DECLARATION = PluginDefinition(
    manifest=plugin_manifest(__file__),
    create=create,
    supported_platforms=("linux",),
    configuration=configuration_spec(__file__),
    credentials=("DASHSCOPE_API_KEY",),
    resources=("usage",),
    provides=(RecognitionRequest, SynthesisRequest),
)
