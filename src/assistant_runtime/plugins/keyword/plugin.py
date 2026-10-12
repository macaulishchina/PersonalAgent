from ...kernel.runtime import Manifest
from .recognition import Recognizer


class KeywordPlugin:
    manifest = Manifest("keyword")

    def __init__(self, config):
        self.recognizer = Recognizer(config)

    async def start(self, context):
        pass
