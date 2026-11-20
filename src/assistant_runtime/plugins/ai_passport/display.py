"""Read-only conversation views bounded for the terminal display and offline cache."""

import base64
import logging

from assistant_runtime.api.speech import SpeechError
from . import protocol


class TerminalDisplay:
    def __init__(self, conversation, send, settings, send_binary):
        self.conversation = conversation
        self.send = send
        self.settings = settings
        self.send_binary = send_binary
        self.stream = None

    async def deliver_previews(self, message, images):
        """Deliver each offline attachment independently within the live event."""
        for ref in images:
            try:
                preview = await self.conversation(
                    "thumbnail", message_id=message["id"], image_id=ref["id"], width=64, height=32
                )
            except Exception as exc:
                # A missing/deleted asset cannot suppress the message or its other images.
                logging.getLogger(__name__).warning("PP preview unavailable: %s", type(exc).__name__)
                continue
            await self.send(
                "message.preview",
                message_id=message["id"],
                group=message.get("turn_id") or message["id"],
                updated=message.get("updated", 0),
                image_id=ref["id"],
                width=preview["width"],
                height=preview["height"],
                version=preview["version"],
                pixels=base64.b64encode(preview["data"]).decode("ascii"),
            )

    def close_image(self):
        self.stream = None

    async def image_stream(self, kind, data):
        ident = data.get("request_id")
        if type(ident) is not int or not 0 < ident < 2**32:
            raise ValueError("Invalid image request ID")
        if kind == "image.stream.open":
            self.close_image()
            if not self.settings().get("show_images", True):
                raise SpeechError("terminal_images_disabled")
            width, height = data.get("width"), data.get("height")
            if (
                type(width) is not int
                or not 16 <= width <= protocol.IMAGE_STRIP_WIDTH
                or type(height) is not int
                or not 16 <= height <= protocol.IMAGE_STRIP_HEIGHT
            ):
                raise ValueError("Invalid image viewport")
            image = await self.conversation(
                "image_raster",
                message_id=data.get("message_id"),
                image_id=data.get("image_id"),
                width=width,
                height=height,
            )
            self.stream = (ident, 0, image)
            await self.send(
                "image.stream.open",
                request_id=ident,
                message_id=data.get("message_id"),
                **{k: v for k, v in image.items() if k != "data"},
            )
            return
        if self.stream is None or ident != self.stream[0]:
            return  # A command for an already replaced view cannot advance or close its successor.
        if kind == "image.stream.close":
            self.stream = None
            return
        _, expected, image = self.stream
        row = data.get("row")
        if type(row) is not int or row != expected or not 0 <= row < image["height"]:
            raise ValueError("Invalid image row")
        rows = min(protocol.IMAGE_STRIP_ROWS, image["height"] - row)
        stride = 2 * image["width"]
        pixels = image["data"][row * stride : (row + rows) * stride]
        await self.send_binary(protocol.image_strip(ident, row, rows, pixels))
        self.stream = (ident, row + rows, image) if row + rows < image["height"] else None
