"""Read-only conversation views bounded for the terminal display and offline cache."""

import base64

from assistant_runtime.api.speech import SpeechError
from . import protocol


class TerminalDisplay:
    def __init__(self, conversation, send, settings):
        self.conversation = conversation
        self.send = send
        self.settings = settings
        self.thumbnail = None

    def close_image(self):
        self.thumbnail = None

    async def image(self, kind, data):
        if kind == "image.open":
            self.close_image()
            if not self.settings().get("show_images", True):
                raise SpeechError("terminal_images_disabled")
            self.thumbnail = await self.conversation(
                "thumbnail",
                message_id=data.get("message_id"),
                image_id=data.get("image_id"),
                **{k: data[k] for k in ("width", "height") if k in data},
            )
            await self.send(
                "image.open",
                message_id=data.get("message_id"),
                **{k: v for k, v in self.thumbnail.items() if k != "data"},
            )
        elif kind == "image.chunk":
            offset = data.get("offset")
            image = self.thumbnail
            if (
                not image
                or data.get("version") != image["version"]
                or type(offset) is not int
                or not 0 <= offset < image["bytes"]
                or offset % 2
            ):
                raise ValueError("Invalid image page")
            raw = image["data"][offset : offset + protocol.IMAGE_CHUNK]
            await self.send(
                "image.chunk",
                version=image["version"],
                offset=offset,
                data=base64.b64encode(raw).decode(),
                next_offset=offset + len(raw) if offset + len(raw) < image["bytes"] else None,
            )
        elif kind == "image.close":
            self.close_image()

    async def history_index(self, data):
        page = await self.conversation(
            "history_index", **{k: data[k] for k in ("before", "after", "limit") if k in data}
        )
        await self.send("history.index", **page)

    async def message_page(self, data):
        if "prefetch" in data and type(data["prefetch"]) is not bool:
            raise ValueError("Invalid history prefetch flag")
        try:
            message = await self.conversation(
                "message",
                message_id=data.get("message_id"),
                offset=data.get("offset", 0),
                limit=80,
            )
        except Exception:
            if not data.get("prefetch"):
                raise
            # Cache warming is a bounded read, never a foreground failure
            # or an excuse to replay the conversation/tool operation.
            await self.send(
                "cache.prefetch_unavailable",
                message_id=data.get("message_id"),
                offset=data.get("offset", 0),
            )
            return
        if message.get("kind") == "detail":
            message.update(text="", next_offset=None, kind="image")
        await self.send("message.page", prefetch=data.get("prefetch", False), **message)

    async def cache_index(self, history):
        # An authoritative recent window is independent of the user's browsing
        # cursor. It contains metadata only, with no model or image inference.
        if self.settings().get("offline_cache", True):
            await self.send(
                "cache.index",
                items=[
                    {k: item[k] for k in ("id", "cursor", "role", "status", "updated", "kind") if k in item}
                    for item in history["items"]
                    if item.get("status") != "streaming"
                ],
            )

    async def restore_message(self, message):
        if message["role"] != "assistant" or message["status"] not in {"complete", "failed", "cancelled"}:
            return
        page = await self.conversation("message", message_id=message["id"], limit=80)
        if page.get("kind") == "detail":
            page.update(text="", next_offset=None, kind="image")
        await self.send(
            "message",
            restored=True,
            **{
                k: page[k]
                for k in (
                    "message_id",
                    "role",
                    "text",
                    "total",
                    "offset",
                    "next_offset",
                    "updated",
                    "status",
                    "images",
                    "kind",
                )
            },
        )
