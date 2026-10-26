"""Injected control-plane handle; never a capability exposed to model tools."""


class ExecutionControl:
    def __init__(self, request):
        self.__request = request

    async def request(self, action, arguments):
        return await self.__request(action, arguments)
