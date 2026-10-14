"""One durable conversation; provider events and entrances share this owner."""

import asyncio
import hashlib
import json
import uuid

from ..kernel.runtime import Manifest
from ..kernel.contracts import Control
from .store import ConversationStore


class Conversation:
    manifest = Manifest("conversation", ("assistant",), critical=True)

    def __init__(self, host):
        self.host = host
        self.store = ConversationStore(host.store)
        self.context = None
        self.submit_lock = asyncio.Lock()
        self.voice_pending = asyncio.Event()
        self.sync_error = None
        self.dispatch_message = None
        self.voice_context_lock = asyncio.Lock()
        self.web_turns = set()
        self.web_dispatching = False
        self.last_state = None
        self.tasks = set()
        host.backend.on_conversation = self.provider_event
        host.voice.on_conversation = self.media_event
        host.backend.on_conversation_done = self.task_done
        host.backend.on_conversation_notice = self.provider_notice

    @property
    def session(self):
        return self.host.backend.session_id

    async def start(self, context):
        self.context = context
        self.store.recover(self.session)
        self.web_turns = {
            row[0]
            for row in self.store.db.execute(
                "SELECT DISTINCT turn_id FROM chat_messages WHERE source='web' AND turn_id IS NOT NULL"
            )
        }
        for event in self.host.backend.restored_conversation_events():
            ident = event.get("message_id")
            if not ident or self.store.get(f"task:{self.session}:{ident}"):
                self.provider_event(event)
        key = "chat.legacy_imported"
        if not self.host.store.get(key):
            for ident, timestamp, role, text in self.host.store.db.execute(
                "SELECT id,time,role,text FROM transcripts WHERE role IN ('user','assistant') ORDER BY id"
            ).fetchall():
                self.store.put(
                    f"legacy:{ident}",
                    self.session,
                    role=role,
                    source="legacy",
                    text=text,
                    status="complete",
                    kind="legacy",
                )
            self.host.store.set(key, True)
        context.spawn(self.watch_state(), "state")
        context.spawn(self.sync_loop(), "voice-context")
        self.voice_pending.set()

    def state(self):
        h = self.host
        return {
            "session_id": self.session,
            "name": h.config.assistant_name,
            "voice": h.mode,
            "speaking": h.player.playing,
            "task_running": h.backend.busy,
            "quiet": h.quiet_active(),
            "backend_ready": h.backend.client.process is not None,
            "web_dispatching": self.web_dispatching,
            "sync_error": self.sync_error,
        }

    async def watch_state(self):
        while True:
            state = self.state()
            if state != self.last_state:
                self.store.emit(self.session, "state", state)
                self.last_state = state
            await asyncio.sleep(0.3)

    def spawn(self, coro, label):
        return self.context.spawn(coro, label)

    def media_event(self, data, epoch):
        kind = data.get("kind")
        if kind == "closed":
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

    def provider_event(self, event):
        if not getattr(self.host.backend, "session_id", None):
            return
        kind, turn = event["kind"], event.get("turn_id")
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
        if kind == "message_delta" and ident:
            mid = f"task:{self.session}:{ident}"
            old = self.store.get(mid)
            self.store.put(
                mid,
                self.session,
                text=(old["text"] if old else "") + event["delta"],
                kind="message" if turn in self.web_turns else "detail",
                turn_id=turn,
            )
        elif kind == "message" and ident:
            self.store.put(
                f"task:{self.session}:{ident}",
                self.session,
                text=event["text"],
                kind="detail" if event["detail"] or turn not in self.web_turns else "message",
                turn_id=turn,
                status=event["status"],
            )
        elif kind == "turn_completed":
            status = event["status"]
            if status in ("interrupted", "failed") and turn in self.web_turns:
                self.store.put(
                    f"outcome:{self.session}:{turn}",
                    self.session,
                    text="任务已取消。"
                    if status == "interrupted"
                    else "任务未完成。请查看执行详情，系统不会自动重试。",
                    status="complete",
                    turn_id=turn,
                )
            for (mid,) in self.store.db.execute(
                "SELECT id FROM chat_messages WHERE session_id=? AND turn_id=? AND status='streaming'",
                (self.session, turn),
            ).fetchall():
                m = self.store.get(mid)
                self.store.put(
                    mid,
                    self.session,
                    text=m["text"],
                    kind=m["kind"],
                    turn_id=turn,
                    status="complete" if status == "completed" else status,
                )

    async def provider_notice(self, text, turn_id):
        self.store.put(
            "notice:" + uuid.uuid4().hex, self.session, text=text, status="complete", turn_id=turn_id
        )
        if turn_id not in self.web_turns and not self.web_dispatching and self.host.voice.opened:
            await self.host.voice.say(text)

    async def task_done(self, turn_id, text, status):
        try:
            await self.deliver_result(turn_id, text, status)
        except Exception:
            self.sync_error = "任务结果已保存，语音上下文同步失败；可在网页查看结果。"
            self.store.emit(self.session, "notice", {"text": self.sync_error})

    async def deliver_result(self, turn_id, text, status):
        if not self.host.voice.opened:
            return
        if status != "completed":
            text = f"任务状态：{status}。" + text
        if turn_id in self.web_turns:
            # An assistant item is quiet context, not another user request.
            await self.host.voice.append_context("网页任务的实际回复（仅同步上下文，无需播报）：\n" + text)
        else:
            await self.host.voice.instruct(
                "刚才委派给 Codex 的任务已结束。根据下列真实状态和结果简短回应用户，不重复执行：\n" + text
            )

    async def submit(self, request_id, text, attachments):
        if not isinstance(request_id, str):
            raise ValueError("发送标识必须是 UUID")
        uuid.UUID(request_id)
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError("文字最多 32000 字符")
        if not text.strip() and not attachments:
            raise ValueError("请输入内容或添加图片")
        if (
            not isinstance(attachments, list)
            or len(attachments) > 4
            or any(not isinstance(a, str) for a in attachments)
        ):
            raise ValueError("每次最多发送四张图片")
        fingerprint = hashlib.sha256(json.dumps([text, attachments], ensure_ascii=False).encode()).hexdigest()
        async with self.submit_lock:
            db = self.store.db
            existing = db.execute(
                "SELECT fingerprint,message_id,state FROM chat_requests WHERE id=?", (request_id,)
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError("发送标识已用于其他消息")
                return {"message": self.store.get(existing[1]), "state": existing[2]}
            media = []
            for ident in attachments:
                row = db.execute(
                    "SELECT filename,media_type FROM chat_uploads WHERE id=?", (ident,)
                ).fetchone()
                if not row:
                    raise ValueError("图片已过期，请重新上传")
                media.append({"id": ident, "name": row[0], "type": row[1]})
            mid = f"web:{request_id}"
            self.store.put(
                mid, self.session, role="user", source="web", text=text, status="sending", attachments=media
            )
            with db:
                db.execute(
                    "INSERT INTO chat_requests VALUES(?,?,?,?,?)",
                    (request_id, self.session, fingerprint, "sending", mid),
                )
                for ident in attachments:
                    db.execute("UPDATE chat_uploads SET attached=1 WHERE id=?", (ident,))
            self.host.store.transcript("user", text)
            self.spawn(self.dispatch(request_id, mid, text, media), "web-message")
            return {"message": self.store.get(mid), "state": "sending"}

    async def dispatch(self, request_id, mid, text, media):
        # Serialize dispatch only, not a whole model turn. Further messages steer it.
        async with self.submit_lock:
            self.web_dispatching = True
            self.dispatch_message = mid
            try:
                if self.host.memory:
                    self.host.memory.preempt()
                await self.sync_voice_to_task()
                if self.host.voice.opened:
                    self.host.voice.silence_web_reply()
                    await self.host.runtime.invoke(Control("stop"))
                    await self.host.voice.append_context(
                        "用户刚从网页发来消息，正在由同一助手处理。仅同步，不要再次执行或播报：\n" + text
                    )
                images = [str(self.host.config.state_dir / "web-uploads" / m["id"]) for m in media]
                await self.host.backend.submit(text, images=images)
                turn_id = self.host.backend.active_turn or self.host.backend.last_turn
                if turn_id:
                    self.web_turns.add(turn_id)
                self.store.put(
                    mid,
                    self.session,
                    role="user",
                    source="web",
                    text=text,
                    status="complete",
                    turn_id=turn_id,
                    attachments=media,
                )
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE chat_requests SET state='accepted' WHERE id=?", (request_id,)
                    )
            except Exception:
                self.store.put(
                    mid,
                    self.session,
                    role="user",
                    source="web",
                    text=text,
                    status="unknown",
                    attachments=media,
                )
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE chat_requests SET state='unknown' WHERE id=?", (request_id,)
                    )
                self.store.emit(
                    self.session,
                    "notice",
                    {"text": "消息提交未能确认。请先核对任务状态，系统不会自动重复执行。"},
                )
            finally:
                self.web_dispatching = False
                self.dispatch_message = None

    async def control(self, action):
        if action not in ("stop", "cancel"):
            raise ValueError("Unsupported conversation control")
        await self.host.runtime.invoke(Control(action))
        return self.state()
