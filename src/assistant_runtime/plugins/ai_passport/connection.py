"""Bounded terminal protocol. Socket lifetime never owns an accepted execution."""

import asyncio
import json
import struct
import time
import uuid

from aiohttp import WSMsgType

from assistant_runtime.api.ai_usage import UsageScope
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import SpeechError, SpeechRequest, spoken_excerpt
from assistant_runtime.api.terminal import TerminalNotifications


class Connection:
    def __init__(self, owner, device, session, ws):
        self.owner, self.context = owner, owner.context
        self.device, self.session, self.ws = device, session, ws
        self.source = "terminal:" + device
        self.input = self.input_id = self.scope = None
        self.tasks = set()
        self.play_task = self.watch_task = self.finish_task = None
        self.generation = 0
        self.playing = None
        self.closed = False
        self.cursor = 0
        self.started = time.time()
        self.send_lock = asyncio.Lock()
        self.credit = 0
        self.credit_changed = asyncio.Event()
        self.last_state = None
        self.last_notices = None
        self.notice_before = None
        self.last_partial = ""
        self.seen = set()
        self.work = None

    async def send(self, kind, **data):
        if kind == "input.receipt" and "result" in data:
            result = data["result"]
            data["result"] = {
                k: result[k] for k in ("state", "request_id") if isinstance(result, dict) and k in result
            }
        async with self.send_lock:
            payload = {"type": kind, **data}
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 16384:
                raise SpeechError("terminal_message_limit")
            await asyncio.wait_for(self.ws.send_json(payload), 5)

    def spawn(self, coro, name):
        task = self.context.spawn(coro, "passport-" + name)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def conversation(self, action, **args):
        return await self.context.invoke(ConversationRequest(action, {"session_id": self.session, **args}))

    async def receipt(self, action, ident, **args):
        return await self.context.invoke(
            ConversationInputRequest(action, {"session_id": self.session, "request_id": ident, **args})
        )

    async def run(self):
        history = await self.conversation("history")
        self.cursor = history["cursor"]
        await self.send(
            "hello", protocol=1, nickname=self.owner.settings["nickname"], input_rate=16000, output_rate=24000
        )
        # Reconnection displays history without replaying historical speech.
        for message in history["messages"][-3:]:
            await self.present(message, speak=False)
        self.watch_task = self.spawn(self.watch(), "state")
        async for frame in self.ws:
            if frame.type not in {WSMsgType.TEXT, WSMsgType.BINARY}:
                break
            try:
                if frame.type == WSMsgType.BINARY:
                    if self.input is None or len(frame.data) < 6:
                        raise SpeechError("speech_input_not_started")
                    sequence = struct.unpack("!I", frame.data[:4])[0]
                    await self.input.accept(sequence, frame.data[4:])
                else:
                    data = json.loads(frame.data)
                    if not isinstance(data, dict):
                        raise ValueError("Invalid command")
                    await self.command(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.abort_input()
                await self.error(exc)

    async def error(self, exc):
        code = exc.code if isinstance(exc, SpeechError) else "terminal_request_failed"
        self.context.log.warning(
            "passport_request_failed device=%s type=%s code=%s", self.device, type(exc).__name__, code
        )
        await self.send("error", code=code, request_id=self.input_id)

    async def command(self, data):
        kind = data.get("type")
        allowed = {
            "input.begin": {"request_id", "mode"},
            "input.finish": {"request_id", "frames"},
            "input.cancel": {"request_id"},
            "input.receipt": {"request_id"},
            "playback.stop": set(),
            "playback.credit": {"generation", "bytes"},
            "playback.continue": {"message_id", "offset"},
            "playback.completed": {"generation"},
            "message.page": {"message_id", "offset"},
            "execution.cancel": {"request_id"},
            "notifications.list": {"before"},
            "notification.receipt": {"id", "revision", "action"},
            "notification.page": {"id", "revision", "offset"},
            "notification.listen": {"id", "revision", "offset"},
        }
        if kind not in allowed or set(data) - (allowed[kind] | {"type"}):
            raise ValueError("Invalid terminal command")
        if kind.startswith("input."):
            ident = str(uuid.UUID(data.get("request_id")))
            if kind == "input.receipt":
                await self.send("input.receipt", request_id=ident, **await self.receipt("receipt", ident))
                return
            if kind == "input.begin":
                if self.input or self.finish_task and not self.finish_task.done():
                    raise SpeechError("speech_busy")
                await self.stop_playback()
                result = await self.receipt(
                    "prepare", ident, source=self.source, mode=data.get("mode", "steer")
                )
                if result["state"] != "prepared":
                    await self.send("input.receipt", request_id=ident, **result)
                    return
                self.input_id = ident
                self.scope = UsageScope(result["work_id"], self.source)
                self.work = result["work_id"]
                self.input = await self.context.invoke(SpeechRequest("recognize", self.scope), timeout=35)
                await self.send("input.ready", request_id=ident)
                return
            if ident != self.input_id:
                raise ValueError("Input identity mismatch")
            if kind == "input.cancel":
                await self.abort_input()
                await self.send("input.cancelled", request_id=ident)
            elif self.input is not None:
                if type(data.get("frames")) is not int or data["frames"] != self.input.sequence:
                    raise SpeechError("speech_input_gap_or_limit")
                stream, self.input = self.input, None
                self.finish_task = self.spawn(self.finish(stream, ident), "recognition")
            else:
                raise SpeechError("speech_input_closed")
            return
        if kind == "playback.credit":
            credit = data.get("bytes")
            if type(credit) is not int or credit <= 0 or credit % 2 or credit > 8192:
                raise ValueError("Invalid playback credit")
            if data.get("generation") == self.generation:
                if self.credit + credit > 8192:
                    raise ValueError("Playback window exceeded")
                self.credit += credit
                self.credit_changed.set()
        elif kind == "playback.stop":
            await self.stop_playback()
        elif kind == "playback.completed":
            if self.playing and data.get("generation") == self.generation and self.playing.get("sent"):
                self.owner.devices.played(self.device, self.playing["id"], self.playing["revision"])
                if self.playing.get("notification"):
                    await self.context.invoke(
                        TerminalNotifications(
                            "played",
                            self.session,
                            self.device,
                            {
                                "id": self.playing["notification"],
                                "revision": self.playing["notification_revision"],
                            },
                        )
                    )
                await self.send("playback.receipt", generation=self.generation, played=True)
                self.playing = None
        elif kind in {"message.page", "playback.continue"}:
            message = await self.conversation(
                "message",
                message_id=data.get("message_id"),
                offset=data.get("offset", 0),
                limit=1000 if kind == "playback.continue" else 80,
            )
            if kind == "message.page":
                await self.send("message.page", **message)
            else:
                await self.start_playback(message, offset=data.get("offset", 0), explicit=True)
        elif kind == "execution.cancel":
            result = await self.conversation("cancel", request_id=data.get("request_id"))
            await self.send("execution.cancelled", result=result)
        elif kind in {"notification.page", "notification.listen"}:
            notice = await self.context.invoke(
                TerminalNotifications(
                    "get",
                    self.session,
                    self.device,
                    {
                        "id": data.get("id"),
                        "revision": data.get("revision"),
                        "offset": data.get("offset", 0),
                        "limit": 1000 if kind == "notification.listen" else 80,
                    },
                )
            )
            if kind == "notification.page":
                await self.send("notification.page", **{k: v for k, v in notice.items() if k != "work_id"})
            else:
                await self.start_playback(notice, offset=data.get("offset", 0), explicit=True)
        elif kind == "notifications.list":
            self.notice_before = data.get("before")
            await self.notices(self.notice_before)
        else:
            if data.get("action") not in {"read", "received", "played"}:
                raise ValueError("Invalid notification receipt")
            result = await self.context.invoke(
                TerminalNotifications(
                    data["action"],
                    self.session,
                    self.device,
                    {"id": data.get("id"), "revision": data.get("revision")},
                )
            )
            await self.send("notification.receipt", id=data["id"], result=result)

    async def finish(self, stream, ident):
        try:
            await self.send("recognizing", request_id=ident)
            ended = time.monotonic()
            text = await stream.finish()
            self.context.log.info(
                "passport_transcript_final device=%s input=%s elapsed_seconds=%.3f",
                self.device,
                ident,
                time.monotonic() - ended,
            )
            await self.send("transcript", request_id=ident, text=text[:400], more=len(text) > 400)
            result = await self.receipt("commit", ident, text=text)
            await self.send("input.receipt", request_id=ident, **result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)
        finally:
            await stream.close()

    async def abort_input(self):
        if self.input:
            stream, self.input = self.input, None
            await stream.close()
            if self.input_id:
                await self.receipt("cancel", self.input_id)
        # Once commit has begun, cancellation cannot assert that no execution occurred.

    async def watch(self):
        try:
            ticks = 0
            while not self.closed:
                if self.input and time.monotonic() - self.input.started > self.input.max_seconds + 10:
                    await self.abort_input()
                    await self.send("error", code="speech_input_deadline")
                if self.input and self.input.partial != self.last_partial:
                    self.last_partial = self.input.partial
                    await self.send(
                        "transcript.partial", request_id=self.input_id, text=self.last_partial[:240]
                    )
                state = await self.conversation("state")
                public = {
                    k: state.get(k)
                    for k in (
                        "active_request_id",
                        "task_running",
                        "question_id",
                        "context_compacting",
                        "configuration_required",
                    )
                }
                public["queued"] = len([r for r in state.get("queue", []) if r["state"] == "queued"])
                public["questions"] = (
                    "\n".join(
                        str(q.get("question", q.get("title", "")))
                        + (
                            " "
                            + " / ".join(
                                str(o.get("label", "")) for o in q.get("options", []) if isinstance(o, dict)
                            )
                            if q.get("options")
                            else ""
                        )
                        for q in state.get("questions", [])
                        if isinstance(q, dict)
                    )[:1000]
                    if state.get("question_id")
                    else ""
                )
                if public != self.last_state:
                    await self.send("state", **public)
                    self.last_state = public
                changes = await self.conversation("events", cursor=self.cursor)
                if changes["events"] is None:
                    history = await self.conversation("history")
                    self.cursor = history["cursor"]
                    for m in history["messages"][-3:]:
                        await self.present(m, speak=False)
                else:
                    for event in changes["events"]:
                        m = event.get("data")
                        if isinstance(m, dict) and "role" in m:
                            await self.present(m, speak=True)
                    self.cursor = changes["cursor"]
                if ticks % 10 == 0:
                    await self.notices(self.notice_before)
                ticks += 1
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)
            await self.ws.close(code=1011)

    async def present(self, message, speak):
        if (
            message.get("role") != "assistant"
            or message.get("status") not in {"complete", "failed", "cancelled"}
            or message.get("kind", "message") != "message"
            or not message.get("text")
        ):
            return
        key = (message["id"], message["updated"])
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 128:
            self.seen = {key}
        await self.send(
            "message",
            message_id=message["id"],
            text=message["text"][:80],
            more=len(message["text"]) > 80,
            next_offset=80 if len(message["text"]) > 80 else None,
            status=message["status"],
        )
        # Task reports have their own ownership and quiet-hours policy; never treat as a foreground answer.
        if (
            speak
            and message.get("source") != "notification"
            and (message.get("presentation") or {}).get("phase") in {None, "final"}
            and not message.get("task_id")
            and self.work
            and self.owner.settings["auto_speak"]
            and not self.input
        ):
            if self.owner.devices.claim_playback(self.device, message["id"], int(message["updated"] * 1000)):
                page = await self.conversation("message", message_id=message["id"], offset=0, limit=1000)
                await self.start_playback(page)

    async def start_playback(self, message, offset=0, explicit=False):
        if self.input:
            raise SpeechError("speech_busy")
        await self.stop_playback()
        text, end = spoken_excerpt(message["text"], 0)
        if not text:
            await self.send(
                "playback.unavailable",
                code="view_full_text",
                message_id=message.get("id", message.get("message_id")),
            )
            return
        if explicit:
            # Continued listening is a new operator action, not a replay of an unknown execution.
            scope = UsageScope(
                message.get("work_id") or ("listen:" + self.device + ":" + uuid.uuid4().hex), self.source
            )
        else:
            if not message.get("work_id"):
                raise SpeechError("speech_work_identity_unavailable")
            scope = UsageScope(message["work_id"], self.source)
        self.playing = {
            "id": message.get("id", message.get("message_id")),
            "revision": int(message.get("updated", 0) * 1000),
            "sent": False,
            "notification": message.get("notification_id"),
            "notification_revision": message.get("notification_revision"),
        }
        self.play_task = self.spawn(self.play(text, scope, self.generation, offset + end), "playback")

    async def play(self, text, scope, generation, offset):
        stream = None
        try:
            started = time.monotonic()
            first = True
            stream = await self.context.invoke(SpeechRequest("synthesize", scope, text), timeout=35)
            await self.send(
                "playback.start",
                generation=generation,
                rate=24000,
                offset=offset - len(text),
                next_offset=offset,
            )
            async for pcm in stream:
                if generation != self.generation:
                    return
                if first:
                    self.context.log.info(
                        "passport_first_audio device=%s generation=%s elapsed_seconds=%.3f",
                        self.device,
                        generation,
                        time.monotonic() - started,
                    )
                    first = False
                for start in range(0, len(pcm), 4096):
                    chunk = pcm[start : start + 4096]
                    async with asyncio.timeout(30):
                        while self.credit < len(chunk):
                            self.credit_changed.clear()
                            await self.credit_changed.wait()
                    if generation != self.generation:
                        return
                    self.credit -= len(chunk)
                    async with self.send_lock:
                        await asyncio.wait_for(
                            self.ws.send_bytes(struct.pack("!I", generation) + pcm[start : start + 4096]), 5
                        )
            if generation == self.generation:
                self.playing["sent"] = True
                await self.send("playback.end", generation=generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)
        finally:
            if stream:
                await stream.aclose()

    async def stop_playback(self):
        self.generation += 1
        self.credit = 0
        self.credit_changed.set()
        if self.play_task and not self.play_task.done():
            self.play_task.cancel()
            await asyncio.gather(self.play_task, return_exceptions=True)
        self.playing = None
        if not self.closed:
            await self.send("playback.stop", generation=self.generation)

    async def notices(self, before=None):
        if not self.context.available(TerminalNotifications):
            return
        page = await self.context.invoke(
            TerminalNotifications(
                "list",
                self.session,
                self.device,
                {"limit": 8, **({"before": before} if before is not None else {})},
            )
        )
        # Poll only emits changes; offline history is displayed, never automatically spoken.
        items = [
            {
                "id": i["id"],
                "revision": i["revision"],
                "title": i["payload"].get("title", "任务结果")[:100],
                "unread": i["unread"],
            }
            for i in page["items"]
        ]
        fresh = self.last_notices is not None
        if items != self.last_notices or before is not None:
            await self.send("notifications", items=items, before=page["before"], quiet=page["quiet"])
            self.last_notices = items
            for item in page["items"]:
                eligible = self.owner.devices.claim_playback(
                    self.device, "notification:" + item["id"], item["revision"]
                )
                if (
                    eligible
                    and fresh
                    and before is None
                    and item["updated"] >= self.started
                    and item["unread"]
                    and not item["expired"]
                    and not page["quiet"]
                    and self.owner.settings["auto_speak"]
                    and not self.input
                    and not self.playing
                    and not (self.last_state or {}).get("task_running")
                ):
                    notice = await self.context.invoke(
                        TerminalNotifications(
                            "get",
                            self.session,
                            self.device,
                            {"id": item["id"], "revision": item["revision"], "limit": 1000},
                        )
                    )
                    await self.start_playback(notice)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        try:
            await self.abort_input()
        finally:
            await self.ws.close()
