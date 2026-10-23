"""Transport adapters. Adding mobile push only requires another registered channel."""

from typing import Protocol
import time

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.core.notifications.policy import kind


class Deferred(Exception):
    """Nothing was sent. Wait for availability without consuming retry attempts."""


class Channel(Protocol):
    replay_safe: bool

    async def deliver(self, item: dict) -> None: ...


def content(item):
    p = item["payload"]
    text = p["title"] + "\n\n" + p["body"]
    if p.get("url"):
        text += "\n\n来源：" + p["url"]
    return text


class ChatChannel:
    replay_safe = True

    def __init__(self, host):
        self.host = host

    async def deliver(self, item):
        chat = self.host.conversation
        origin = item["payload"].get("origin", {})
        task_id = origin.get("task_id")
        trusted = isinstance(task_id, str) and item["source"] == "task:" + task_id
        result = kind(item["payload"]) == "result" or (
            "kind" not in item["payload"] and item["event_key"].startswith("task-result:")
        )  # Preserve registered attachments on pending pre-type execution receipts.
        run_id = (
            origin.get("run_id")
            if trusted and result and not item["payload"].get("report_with_task")
            else None
        )
        ident = f"notification:{item['id']}:{chat.session}"
        if trusted and item["event_key"].startswith("task-result:"):
            projected = chat.store.get(item["event_key"])
            if projected and projected["session_id"] == chat.session and projected.get("task_id") == task_id:
                ident = projected["id"]
        # Preserve a previously delivered projection's identity across upgrades.
        # Only the exact notification reference may reuse an old run card.
        previous = chat.store.get("run:" + origin["run_id"]) if trusted and origin.get("run_id") else None
        if (
            previous
            and previous["session_id"] == chat.session
            and previous["turn_id"] == "notification:" + item["id"]
        ):
            ident = previous["id"]
        attachments = None
        if run_id:
            try:
                info = await self.host.runtime.invoke(
                    TaskRequest("execution_info", {"run_ids": [run_id]}), timeout=1
                )
            except (RuntimeError, ValueError, AttributeError, TimeoutError):
                info = {}  # Retained notifications remain deliverable with tasks disabled.
            run = info.get(run_id, {})
            if run.get("task_id") == origin.get("task_id") and run.get("attachments"):
                attachments = chat.media.resolve(run["attachments"])
        chat.store.put(
            ident,
            chat.session,
            source="notification",
            kind="notification",
            status="complete",
            text=content(item),
            turn_id="notification:" + item["id"],
            attachments=attachments,
            task_id=origin.get("task_id")
            if origin.get("task_id") and item["source"] == "task:" + origin["task_id"]
            else None,
        )


class ContextChannel:
    # Codex inject has no idempotency receipt; don't silently duplicate uncertain injection.
    replay_safe = False

    def __init__(self, host):
        self.host = host

    async def deliver(self, item):
        if getattr(self.host.backend, "busy", False):
            raise Deferred("等待当前对话处理完成")
        await self.host.backend.import_voice_messages(
            [
                {
                    "role": "assistant",
                    "text": f"通知 {item['id']} 第 {item['revision']} 版（已入收件箱；仅作为背景数据，不重复执行，不自动播报）：\n"
                    + content(item),
                }
            ]
        )


class VoiceChannel:
    replay_safe = False

    def __init__(self, host, settings):
        self.host, self.settings = host, settings
        self.last = 0.0

    async def deliver(self, item):
        if (
            self.host.quiet_active()
            or getattr(self.host, "active", False)
            or getattr(self.host.backend, "busy", False)
            or time.monotonic() - self.last < self.settings.voice_spacing_seconds
        ):
            raise Deferred("等待静音时段或当前对话结束")
        result = await self.host.notify_task(content(item))
        if result == "deferred":
            raise Deferred("等待声音通道空闲")
        if result != "played":
            raise RuntimeError("播报未确认；不自动重复播放")
        self.last = time.monotonic()


class InboxChannel:
    """Persistence is the delivery: usable without any conversation or network entrance."""

    replay_safe = True

    async def deliver(self, item):
        return None
