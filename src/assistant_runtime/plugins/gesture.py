"""An explicit simulated trigger, useful before a camera/perception plugin exists."""

from ..kernel.contracts import Trigger
from ..kernel.runtime import Manifest


class GesturePlugin:
    manifest = Manifest("gesture")

    async def start(self, context):
        self.context = context

    async def simulate(self, gesture):
        if gesture != "wave":
            raise ValueError("Only the explicit wave trigger is configured")
        return await self.context.runtime.invoke(Trigger("gesture", "wake"))
