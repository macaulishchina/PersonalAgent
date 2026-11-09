"""An explicit simulated trigger, useful before a camera/perception plugin exists."""

from .declaration import DECLARATION

from assistant_runtime.api.contracts import Trigger


class GesturePlugin:
    manifest = DECLARATION.manifest

    async def start(self, context):
        self.context = context

    async def simulate(self, gesture):
        if gesture != "wave":
            raise ValueError("Only the explicit wave trigger is configured")
        return await self.context.invoke(Trigger("gesture", "wake"))
