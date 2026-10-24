"""Optional personal-assistant presentation around a public conversation."""

import asyncio
import time
from assistant_runtime.api.contracts import Control
from assistant_runtime.api.speech import FeedbackIntent, RequestRef
from assistant_runtime.core.conversation.service import Conversation
from assistant_runtime.core.conversation.feedback import spoken_result


class AssistantConversation(Conversation):
    async def control(self, action):
        if action not in ("stop", "cancel"):
            raise ValueError("Unsupported conversation control")
        await self.host.runtime.invoke(Control(action))
        return self.state()

    def __init__(self, host):
        super().__init__(host)
        self.voice_pending = asyncio.Event()
        self.voice_context_lock = asyncio.Lock()
        self.feedback_lock = asyncio.Lock()
        self.voice_revision = 0
        self.spoken_focus = None
        self.voice_reply_focus = {}
        host.voice.on_conversation = self.media_event

    async def start_presentation(self):
        if getattr(self.host.voice, "available", True):
            self.context.spawn(self.sync_loop(), "voice-context")
            self.voice_pending.set()

    def state(self):
        return {
            **super().state(),
            "voice": self.host.mode if getattr(self.host.voice, "available", True) else "disabled",
            "speaking": self.host.player.playing if self.host.player else False,
            "quiet": self.host.quiet_active(),
        }

    async def prepare_dispatch(self, text):
        await self.sync_voice_to_task()
        if self.host.voice.opened:
            self.host.voice.silence_web_reply()
            await self.host.runtime.invoke(Control("stop"))
            await self.host.voice.append_context("Web input (context only; do not execute again):\n" + text)

    async def provider_notice(self, text, turn_id):
        await super().provider_notice(text, turn_id)
        if self.feedback_allowed(turn_id) and turn_id not in self.web_turns and not self.web_dispatching:
            await self.host.voice.say(text)

    def event_context(self, event, restored=False):
        kind, turn = event["kind"], event.get("turn_id")
        if kind == "turn_started" and not restored:
            self.feedback.bind(
                turn,
                self.session,
                getattr(self.host.voice, "epoch", 0),
                self.voice_revision,
                "web"
                if not getattr(self.host.voice, "available", True)
                or self.web_dispatching
                or turn in self.web_turns
                else "voice",
            )
        if kind == "turn_started" and self.web_dispatching:
            self.web_turns.add(turn)
            if self.dispatch_message:
                m = self.store.get(self.dispatch_message)
                self.store.put(
                    m["id"],
                    self.session,
                    role="user",
                    source="web",
                    text=m["text"],
                    status=m["status"],
                    attachments=m["attachments"],
                    turn_id=turn,
                )
        ident = event.get("message_id")
        if kind == "execution_input" and not restored and turn == self.host.backend.active_turn:
            renewed = self.feedback.accept_input(
                f"{self.session}:{ident}",
                turn,
                self.session,
                getattr(self.host.voice, "epoch", 0),
                self.voice_revision,
                "web"
                if not getattr(self.host.voice, "available", True)
                or self.web_dispatching
                or getattr(self.host.voice, "web_silent", False)
                else "voice",
            )
            if renewed:
                self.host.voice.execution_started(turn)
            receipt = self.feedback.get(turn)
            if receipt and event.get("text"):
                focus = (receipt["epoch"], receipt["revision"])
                if self.ack_allowed(turn, focus):
                    self.spawn(self.acknowledge_input(turn, focus, event["text"]), "voice-acceptance")

        if kind == "message":
            summary = event.get("tool_summary") or {}
            binding = self.feedback.get(turn) if turn else None
            if (
                not restored
                and summary.get("kind") == "create_task"
                and summary.get("outcome") == "completed"
                and binding
                and binding.get("source") == "voice"
                and binding.get("epoch") == self.host.voice.epoch
                and hasattr(self.host, "voice_control")
            ):
                self.host.voice_control.bind(turn, summary["task_id"])

    async def finish_presentation(self, turn_id, text, status):
        if not getattr(self.host.voice, "available", True):
            self.feedback.finish(turn_id, "context")
            return
        try:
            await self.deliver_result(turn_id, text, status)
        except Exception:
            self.sync_error = "任务结果已保存，语音交付未确认；未自动重播，可在网页查看结果。"
            self.store.emit(self.session, "notice", {"text": self.sync_error})
        finally:
            receipt = self.feedback.get(turn_id)
            if receipt and receipt["state"] != "unknown":
                self.host.voice.finish_execution(turn_id)

    def media_event(self, data, epoch):
        kind = data.get("kind")
        if kind == "closed":
            # Startup can fail before the provider establishes a logical session.
            # Cleanup must not emit a session event or mask the original failure.
            if self.session is None:
                return
            self.feedback.close(self.session, epoch)
            self.voice_revision += 1
            for (mid,) in self.store.db.execute(
                "SELECT id FROM chat_messages WHERE session_id=? AND source='voice' AND status='streaming'",
                (self.session,),
            ).fetchall():
                message = self.store.get(mid)
                self.store.put(
                    mid,
                    self.session,
                    role=message["role"],
                    source="voice",
                    text=message["text"],
                    status="interrupted",
                )
            return
        ident = data.get("message_id")
        if not ident:
            return
        message_id = f"voice:{self.session}:{epoch}:{ident}"
        old = self.store.get(message_id)
        if data.get("role") == "user" and old is None:
            self.voice_revision += 1
        if kind == "interrupted":
            if old:
                self.store.put(
                    message_id,
                    self.session,
                    role=old["role"],
                    source="voice",
                    text=old["text"],
                    status="interrupted",
                )
            return
        role = data.get("role") or (old["role"] if old else None)
        if role not in ("user", "assistant"):
            return
        if role == "assistant" and old is None:
            self.voice_reply_focus[message_id] = (epoch, self.voice_revision)
            if len(self.voice_reply_focus) > 128:
                del self.voice_reply_focus[next(iter(self.voice_reply_focus))]
        text = (
            data.get("text", "")
            if kind != "message_delta"
            else (old["text"] if old else "") + data.get("delta", "")
        )
        self.store.put(
            message_id,
            self.session,
            role=role,
            source="voice",
            text=text,
            status=data["status"],
        )
        if data["status"] == "complete" and text:
            if role == "assistant":
                focus = self.voice_reply_focus.pop(message_id, None)
                if focus == (epoch, self.voice_revision):
                    self.spoken_focus = focus
            self.voice_pending.set()

    async def sync_loop(self):
        while True:
            await self.voice_pending.wait()
            self.voice_pending.clear()
            try:
                await self.sync_voice_to_task()
                self.sync_error = None
            except Exception:
                self.sync_error = "语音上下文同步暂时中断，正在恢复。"
                await asyncio.sleep(5)
                self.voice_pending.set()

    async def sync_voice_to_task(self):
        if not getattr(self.host.voice, "available", True):
            return
        async with self.voice_context_lock:
            while True:
                ids = self.store.db.execute(
                    "SELECT id FROM chat_messages WHERE session_id=? AND source='voice' "
                    "AND status='complete' AND id NOT IN (SELECT message_id FROM chat_context) "
                    "ORDER BY ordinal LIMIT 20",
                    (self.session,),
                ).fetchall()
                if not ids:
                    return
                pending = [self.store.get(row[0]) for row in ids]
                await self.host.backend.import_voice_messages(pending)
                with self.store.db:
                    self.store.db.executemany("INSERT OR IGNORE INTO chat_context VALUES(?)", ids)

    def ack_allowed(self, turn, focus):
        receipt = self.feedback.get(turn)
        return bool(
            self.feedback_allowed(turn)
            and receipt["source"] == "voice"
            and focus == (receipt["epoch"], receipt["revision"])
            and focus != self.spoken_focus
            and not self.web_dispatching
            and not getattr(self.host.voice, "web_silent", False)
            and not getattr(self.host.player, "manual_muted", False)
        )

    async def deliver_ack(self, turn, focus, text):
        # Only speech delivery waits here; provider events and device work continue.
        async with self.feedback_lock:
            if not self.ack_allowed(turn, focus):
                self.feedback.acknowledge(turn, *focus, "skipped")
                return
            try:
                await self.speech_feedback(turn, focus, "accepted", text)
            except asyncio.CancelledError:
                self.feedback.acknowledge(turn, *focus, "unknown")
                raise
            except Exception:
                self.feedback.acknowledge(turn, *focus, "unknown")
                self.sync_error = "接单说明的语音交付未确认，任务继续执行；未自动重播。"
                self.store.emit(self.session, "notice", {"text": self.sync_error})
            else:
                self.feedback.acknowledge(turn, *focus, "submitted")

    async def acknowledge_input(self, turn, focus, text):
        # Structured provider acceptance, independent of web commentary. Fast
        # completion closes the receipt before this delay and needs no extra ack.
        await asyncio.sleep(0.8)
        if self.ack_allowed(turn, focus) and self.feedback.acknowledge(turn, *focus, "dispatching"):
            spoken = "我来处理“" + text + "”。" if len(text) <= 65 else "请求已收到，我先核对具体操作。"
            await self.deliver_ack(turn, focus, spoken)

    async def speech_feedback(self, turn, focus, kind, text, *, delivery_id=None):
        interaction = getattr(self.host, "interaction", None)
        if interaction and interaction.ready:
            if len(text) > 240:
                text = "这次返回的结果比较长，需要我展开说明吗？"
            intent = FeedbackIntent(
                RequestRef(delivery_id or turn, focus[1]), kind, text, time.monotonic() + 30
            )
            result = await interaction.output.say(intent, lambda: self.feedback_allowed(turn))
            if result != "completed":
                raise RuntimeError("Speech delivery " + result)
        else:
            await self.host.voice.say(
                text, execution_id=turn, **({"interim": True} if kind == "accepted" else {})
            )

    async def deliver_result(self, turn_id, text, status):
        async with self.feedback_lock:
            receipt = self.feedback.get(turn_id)
            if not receipt or receipt["state"] != "bound":
                return
            if not self.feedback_allowed(turn_id):
                self.feedback.finish(turn_id, "superseded")
                return
            spoken = spoken_result(text, status)
            if not spoken:
                self.feedback.finish(turn_id, "empty" if not text.strip() else "interrupted")
                return
            # Commit intent BEFORE the RPC: a lost acknowledgement is uncertain,
            # never a reason to speak the same result again.
            self.feedback.finish(turn_id, "dispatching")
            try:
                if receipt["source"] == "web":
                    await self.host.voice.append_context(
                        "网页任务的实际回复（仅同步上下文，无需播报）：\n" + text
                    )
                else:
                    await self.speech_feedback(
                        turn_id, (receipt["epoch"], receipt["revision"]), "result", spoken
                    )
            except Exception:
                self.feedback.finish(turn_id, "unknown")
                raise
            self.feedback.finish(turn_id, "context" if receipt["source"] == "web" else "submitted")

    def feedback_allowed(self, turn):
        receipt = self.feedback.get(turn)
        voice = self.host.voice
        return bool(
            receipt
            and receipt["state"] in {"bound", "dispatching"}
            and voice.opened
            and not getattr(voice, "closing", False)
            and not self.host.quiet_active()
            and receipt["epoch"] == getattr(voice, "epoch", 0)
            and receipt["revision"] == self.voice_revision
        )
