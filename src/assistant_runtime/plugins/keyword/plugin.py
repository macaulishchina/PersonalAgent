from .declaration import DECLARATION
from .recognition import Recognizer


class KeywordPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, config):
        self.recognizer = Recognizer(config)

    async def start(self, context):
        pass
