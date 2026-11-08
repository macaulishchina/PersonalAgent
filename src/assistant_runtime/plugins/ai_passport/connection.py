"""Bounded terminal protocol. Socket lifetime never owns an accepted execution."""

import asyncio
import base64
import json
import struct
import time
import uuid

from aiohttp import WSMsgType

from assistant_runtime.api.ai_usage import UsageScope
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import SpeechError, SpeechRequest, spoken_excerpt, spoken_sentence
from assistant_runtime.api.terminal import TerminalNotifications
from .settings import LOCAL_FIELDS


class Connection:
    def __init__(self, owner, device, session, ws):
        self.owner, self.context = owner, owner.context
        self.device, self.session, self.ws = device, session, ws
        self.source = "terminal:" + device
        self.input = self.input_id = self.scope = None
        self.failed_input_id = None
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
        self.work = None
        self.confirmation = None
        self.committing = False
        self.changed = asyncio.Event()
        self.changed.set()
        self.speech_changed = asyncio.Event()
        self.segment_completed = asyncio.Event()
        self.voice = None
        self.thumbnail = None

    async def send(self, event_type, **data):
        if event_type == "input.receipt" and "result" in data:
            result = data["result"]
            data["result"] = {
                k: result[k] for k in ("state", "request_id") if isinstance(result, dict) and k in result
            }
        async with self.send_lock:
            payload = {"type": event_type, **data}
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

    def settings(self):
        return self.owner.settings_for(self.device)

    async def send_configuration(self):
        await self.send("configuration", fields=self.owner.configuration_for(self.device))

    async def run(self):
        history = await self.conversation("history_index", limit=3)
        self.cursor = history["cursor"]
        await self.send(
            "hello",
            protocol=2,
            input_rate=16000,
            output_rate=24000,
            binding=self.session,
            server_time=int(time.time()),
        )
        await self.send_configuration()
        await self.send("history.index", **history)
        await self.cache_index(history)
        # Reconnection displays history without replaying historical speech.
        for message in history["items"]:
            await self.restore_message(message)
        self.watch_task = self.spawn(self.watch(), "state")
        self.spawn(self.watch_notices(), "notifications")
        async for frame in self.ws:
            if frame.type not in {WSMsgType.TEXT, WSMsgType.BINARY}:
                break
            operation = None
            try:
                if frame.type == WSMsgType.BINARY:
                    operation = "input.pcm"
                    if self.input is None and self.failed_input_id:
                        # Already reported this prepared input's failure. Drain
                        # residual PCM until the next explicit input.begin.
                        continue
                    if self.input is None or len(frame.data) < 6:
                        raise SpeechError("speech_input_not_started")
                    sequence = struct.unpack("!I", frame.data[:4])[0]
                    await self.input.accept(sequence, frame.data[4:])
                else:
                    data = json.loads(frame.data)
                    if not isinstance(data, dict):
                        raise ValueError("Invalid command")
                    operation = data.get("type") if isinstance(data.get("type"), str) else None
                    await self.command(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                input_failure = (
                    frame.type == WSMsgType.BINARY
                    or (
                        operation in {"input.finish", "input.confirm", "input.cancel"}
                        and data.get("request_id") == self.input_id
                    )
                    or (
                        operation == "input.begin"
                        and data.get("request_id") == self.input_id
                        and not (isinstance(exc, SpeechError) and exc.code == "speech_busy")
                    )
                )
                ident = self.input_id if input_failure else None
                if input_failure:
                    self.failed_input_id = ident
                    if self.input:
                        self.context.log.warning(
                            "passport_input_rejected input=%s operation=%s frames=%s audio_bytes=%s max_audio_bytes=%s",
                            ident,
                            operation or "pcm",
                            self.input.sequence,
                            self.input.size,
                            self.input.max_bytes,
                        )
                    await self.abort_input()
                await self.error(exc, request_id=ident, operation=operation)

    async def error(self, exc, request_id=None, operation=None):
        code = exc.code if isinstance(exc, SpeechError) else "terminal_request_failed"
        self.context.log.warning(
            "passport_request_failed device=%s input=%s operation=%s type=%s code=%s",
            self.device,
            request_id,
            operation,
            type(exc).__name__,
            code,
        )
        await self.send("error", code=code, request_id=request_id, operation=operation)

    async def command(self, data):
        kind = data.get("type")
        allowed = {
            "input.begin": {"request_id", "mode"},
            "input.finish": {"request_id", "frames", "confirmation_required"},
            "input.confirm": {"request_id"},
            "input.cancel": {"request_id"},
            "input.receipt": {"request_id"},
            "input.transcript": {"request_id", "offset"},
            "playback.stop": set(),
            "playback.credit": {"generation", "bytes"},
            "playback.completed": {"generation"},
            "message.page": {"message_id", "offset", "prefetch"},
            "history.index": {"before", "after", "limit"},
            "execution.cancel": {"request_id"},
            "notifications.list": {"before"},
            "notification.receipt": {"id", "revision", "action"},
            "notification.page": {"id", "revision", "offset"},
            "configuration.reload": set(),
            "configuration.applied": {"versions", "actual"},
            "image.open": {"message_id", "image_id", "width", "height"},
            "image.chunk": {"version", "offset"},
            "image.close": set(),
        }
        if not isinstance(kind, str) or kind not in allowed or set(data) - (allowed[kind] | {"type"}):
            raise ValueError("Invalid terminal command")
        if kind.startswith("input."):
            ident = str(uuid.UUID(data.get("request_id")))
            if kind == "input.receipt":
                await self.input_receipt(ident, await self.receipt("receipt", ident))
                return
            if kind == "input.begin":
                if self.input or self.confirmation or self.finish_task and not self.finish_task.done():
                    raise SpeechError("speech_busy")
                await self.stop_playback()
                result = await self.receipt(
                    "prepare", ident, source=self.source, mode=data.get("mode", "steer")
                )
                if result["state"] != "prepared":
                    await self.input_receipt(ident, result)
                    return
                self.input_id = ident
                self.failed_input_id = None
                self.confirmation = None
                self.last_partial = ""
                self.scope = UsageScope(result["work_id"], self.source)
                self.work = result["work_id"]
                self.input = await self.context.invoke(SpeechRequest("recognize", self.scope), timeout=35)
                self.spawn(self.watch_transcript(self.input, ident), "transcript")
                await self.send("input.ready", request_id=ident, max_audio_bytes=self.input.max_bytes)
                return
            if ident != self.input_id:
                if ident == self.failed_input_id:
                    if kind == "input.cancel":
                        await self.input_receipt(ident, await self.receipt("receipt", ident))
                    else:
                        await self.error(SpeechError("speech_input_closed"), ident, kind)
                    return
                raise SpeechError("terminal_input_identity_mismatch")
            if kind == "input.transcript":
                page = await self.receipt("transcript", ident, offset=data.get("offset", 0), limit=80)
                await self.send("input.transcript", request_id=ident, **page)
            elif kind == "input.cancel":
                await self.abort_input()
                await self.send("input.receipt", request_id=ident, **await self.receipt("receipt", ident))
            elif kind == "input.confirm":
                self.committing = True
                try:
                    result = await self.receipt("confirm", ident)
                    self.confirmation = None
                    await self.send("input.receipt", request_id=ident, **result)
                finally:
                    self.committing = False
            elif self.input is not None:
                if type(data.get("frames")) is not int or data["frames"] != self.input.sequence:
                    raise SpeechError("speech_input_gap")
                confirm = data.get("confirmation_required", False)
                if type(confirm) is not bool:
                    raise ValueError("Invalid input confirmation")
                confirm = confirm or self.input.size >= self.input.max_bytes
                stream, self.input = self.input, None
                self.finish_task = self.spawn(self.finish(stream, ident, confirm), "recognition")
            else:
                raise SpeechError("speech_input_closed")
            return
        if kind == "image.open":
            self.thumbnail = None
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
            raw = image["data"][offset : offset + 2048]
            await self.send(
                "image.chunk",
                version=image["version"],
                offset=offset,
                data=base64.b64encode(raw).decode(),
                next_offset=offset + len(raw) if offset + len(raw) < image["bytes"] else None,
            )
        elif kind == "image.close":
            self.thumbnail = None
        elif kind == "configuration.reload":
            await self.send_configuration()
        elif kind == "configuration.applied":
            versions, actual = data.get("versions", {}), data.get("actual", {})
            if (
                not isinstance(versions, dict)
                or not isinstance(actual, dict)
                or len(versions) > 16
                or set(actual) - set(LOCAL_FIELDS)
                or any(type(k) is not str for k in versions)
            ):
                raise ValueError("Invalid configuration receipt")
            if (
                "volume" in actual
                and (type(actual["volume"]) is not int or not 0 <= actual["volume"] <= 100)
                or "keep_online" in actual
                and type(actual["keep_online"]) is not bool
                or "input_mode" in actual
                and actual["input_mode"] not in {"steer", "queue"}
            ):
                raise ValueError("Invalid device preferences")
            self.owner.devices.configuration_receipt(self.device, versions, actual)
            await self.send("configuration.receipt")
        elif kind == "history.index":
            page = await self.conversation(
                "history_index", **{k: data[k] for k in ("before", "after", "limit") if k in data}
            )
            await self.send("history.index", **page)
        elif kind == "playback.credit":
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
                if not self.playing.get("foreground"):
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
                self.credit = 0
                self.playing = None
                self.segment_completed.set()
        elif kind == "message.page":
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
        elif kind == "execution.cancel":
            result = await self.conversation("cancel", request_id=data.get("request_id"))
            await self.send("execution.cancelled", result=result)
        elif kind == "notification.page":
            notice = await self.context.invoke(
                TerminalNotifications(
                    "get",
                    self.session,
                    self.device,
                    {
                        "id": data.get("id"),
                        "revision": data.get("revision"),
                        "offset": data.get("offset", 0),
                        "limit": 80,
                    },
                )
            )
            # The common delivery record also carries a synthetic speech-message
            # id. The terminal page contract uses the canonical notification id.
            await self.send(
                "notification.page",
                id=notice["notification_id"],
                revision=notice["notification_revision"],
                text=notice["text"],
                offset=notice["offset"],
                next_offset=notice["next_offset"],
                updated=notice["updated"],
            )
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

    async def commit(self, ident, text):
        self.committing = True
        try:
            result = await self.receipt("commit", ident, text=text)
            await self.send("input.receipt", request_id=ident, **result)
        finally:
            self.committing = False

    async def input_receipt(self, ident, result):
        # Restore only an idle connection, never replace an unrelated live input.
        if result["state"] == "review" and not self.input and not self.committing:
            if not self.input_id or self.input_id == ident:
                self.input_id = ident
                self.confirmation = True
                self.work = result["work_id"]
        await self.send("input.receipt", request_id=ident, **result)

    async def finish(self, stream, ident, confirmation_required=False):
        try:
            await self.send("recognizing", request_id=ident)
            ended = time.monotonic()
            text = await stream.finish()
            if self.input_id != ident or self.closed:
                return
            self.context.log.info(
                "passport_transcript_final device=%s input=%s elapsed_seconds=%.3f",
                self.device,
                ident,
                time.monotonic() - ended,
            )
            if confirmation_required:
                result = await self.receipt("review", ident, text=text)
                if result["state"] != "review":
                    await self.input_receipt(ident, result)
                    return
                self.confirmation = True
            # Projection is bounded; submission and confirmation retain the full text.
            offset = max(0, len(text) - 240)
            await self.send(
                "transcript", request_id=ident, text=text[offset:], offset=offset, total=len(text)
            )
            if confirmation_required:
                await self.send(
                    "input.confirmation", request_id=ident, reason="recording_limit", total=len(text)
                )
            else:
                await self.commit(ident, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc, request_id=ident, operation="input.finish")
        finally:
            await stream.close()

    async def abort_input(self, *, preserve_review=False):
        # Cancelling recognition is safe before commit. Once commit starts its
        # durable receipt, not socket lifetime, determines whether it executed.
        if self.committing:
            return
        ident = self.input_id
        if (
            self.finish_task
            and self.finish_task is not asyncio.current_task()
            and not self.finish_task.done()
        ):
            self.finish_task.cancel()
            await asyncio.gather(self.finish_task, return_exceptions=True)
        if self.input:
            stream, self.input = self.input, None
            await stream.close()
        self.confirmation = None
        self.input_id = None
        if ident:
            if preserve_review and (await self.receipt("receipt", ident))["state"] == "review":
                return
            await self.receipt("cancel", ident)

    async def watch(self):
        try:
            while not self.closed:
                try:
                    await asyncio.wait_for(self.changed.wait(), 30)
                except TimeoutError:
                    # Recovery only: durable cursor is authoritative if the
                    # bounded shared event bus dropped a wake-up under load.
                    pass
                self.changed.clear()
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
                    history = await self.conversation("history_index", limit=3)
                    self.cursor = history["cursor"]
                    await self.cache_index(history)
                    for m in history["items"]:
                        await self.restore_message(m)
                else:
                    refresh_history = False
                    for event in changes["events"]:
                        m = event.get("data")
                        if isinstance(m, dict) and "role" in m:
                            await self.present(m, speak=True)
                            refresh_history = refresh_history or m.get("status") != "streaming"
                    self.cursor = changes["cursor"]
                    if refresh_history:
                        # Keep the small directory current for offline navigation.
                        # Its snapshot cursor must not skip undelivered events.
                        index = await self.conversation("history_index", limit=3)
                        await self.send("history.index", pushed=True, **index)
                        await self.cache_index(index)
                    if len(changes["events"]) >= 100:
                        self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)
            await self.ws.close(code=1011)

    async def watch_transcript(self, stream, ident):
        try:
            previous = ""
            async with asyncio.timeout(stream.max_seconds + 10):
                while self.input is stream and self.input_id == ident:
                    text = await stream.wait_update(previous)
                    if self.input is not stream or self.input_id != ident:
                        return
                    previous = text
                    await self.send(
                        "transcript.partial",
                        request_id=ident,
                        text=text[-240:],
                        offset=max(0, len(text) - 240),
                    )
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        except Exception as exc:
            if self.input is stream:
                await self.abort_input()
                await self.error(
                    SpeechError("speech_input_deadline") if isinstance(exc, TimeoutError) else exc,
                    request_id=ident,
                )

    async def watch_notices(self):
        try:
            while not self.closed:
                await self.notices(self.notice_before)
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)

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

    async def present(self, message, speak):
        presentation = message.get("presentation") or {}
        terminal = (
            message.get("status") in {"failed", "cancelled", "interrupted", "unknown"}
            or (presentation.get("turn_status") in {"failed", "interrupted", "unknown"})
            or message.get("status") == "complete"
            and presentation.get("phase") in {"commentary", "tool"}
        )
        if terminal and self.voice and self.voice["id"] == message["id"]:
            await self.stop_playback()
        if (
            message.get("role") != "assistant"
            or message.get("status")
            not in {"streaming", "complete", "failed", "cancelled", "interrupted", "unknown"}
            or (message.get("kind", "message") != "message" and not message.get("attachments"))
            or not (message.get("text") or message.get("attachments"))
        ):
            return
        # Durable event cursors and playback claims own deduplication. A
        # presentation update may deliberately keep the original timestamp.
        text_body = message["text"] if message.get("kind") != "detail" else ""
        offset = max(0, len(text_body) - 160) if message["status"] == "streaming" else 0
        await self.send(
            "message",
            message_id=message["id"],
            role=message["role"],
            text=text_body[offset : offset + 160],
            total=len(text_body),
            offset=offset,
            more_before=offset > 0,
            more=len(text_body) > offset + 160,
            next_offset=offset + 160 if len(text_body) > offset + 160 else None,
            status=message["status"],
            updated=message.get("updated", ""),
            kind="image" if message.get("kind") == "detail" and message.get("attachments") else "message",
            images=[
                {k: ref[k] for k in ("id", "type") if k in ref}
                for ref in message.get("attachments", [])
                if ref.get("type", "").startswith("image/")
            ][:4],
        )
        if terminal:
            return
        # Task reports have their own ownership and quiet-hours policy; never treat as a foreground answer.
        if (
            speak
            and message.get("source") != "notification"
            and not message.get("task_id")
            and message.get("kind", "message") == "message"
            and self.work
            and (self.voice and self.voice["id"] == message["id"] or self.settings()["speech_mode"] != "off")
            and not self.input
        ):
            ranges = [
                dict(r)
                for r in presentation.get("text_ranges", [])
                if r.get("phase") == "final" and r.get("evidence") == "provider"
            ]
            complete = message["status"] == "complete" and presentation.get("phase") in {None, "final"}
            if (
                complete
                and not ranges
                and not any(
                    r.get("evidence") in {"conflict", "provider"} for r in presentation.get("text_ranges", [])
                )
            ):
                total = (
                    presentation.get("result_length")
                    if presentation.get("body") == "excerpt" and message.get("result_ref")
                    else len(message["text"])
                )
                ranges = [{"start": 0, "end": total}]
            if presentation.get("range_limit"):
                if self.voice and self.voice["id"] == message["id"]:
                    await self.stop_playback()
                await self.send("playback.unavailable", code="view_full_text", message_id=message["id"])
                return
            if self.voice and self.voice["id"] == message["id"]:
                if any(r.get("evidence") == "conflict" for r in presentation.get("text_ranges", [])):
                    await self.stop_playback()
                    return
                self.voice.update(ranges=ranges, complete=complete)
                self.speech_changed.set()
            elif ranges and self.owner.devices.claim_playback(self.device, message["id"], 0):
                await self.stop_playback()
                self.voice = {
                    "id": message["id"],
                    "ranges": ranges,
                    "complete": complete,
                    "offset": ranges[0]["start"],
                    "spoken": 0,
                    "settings": {k: self.settings().get(k) for k in ("speech_mode", "voice", "rate")},
                }
                await self.send("playback.pending", generation=self.generation, message_id=message["id"])
                self.play_task = self.spawn(self.speak_foreground(self.voice), "foreground-speech")

    async def speak_foreground(self, voice):
        generation = self.generation
        reason = "complete"
        try:
            while not self.closed and self.voice is voice and generation == self.generation:
                self.speech_changed.clear()
                span = next((r for r in voice["ranges"] if r["end"] > voice["offset"]), None)
                if not span:
                    if voice["complete"]:
                        break
                    await self.speech_changed.wait()
                    continue
                offset = max(span["start"], voice["offset"])
                remaining = 200 - voice["spoken"] if voice["settings"]["speech_mode"] == "brief" else 200
                if remaining <= 0:
                    reason = "brief_limit"
                    break
                page = await self.conversation(
                    "message", message_id=voice["id"], offset=offset, limit=min(remaining, 200)
                )
                if generation != self.generation or self.voice is not voice:
                    return
                text = page["text"][: max(0, span["end"] - offset)]
                sentence = spoken_sentence(
                    text, final=voice["complete"] and offset + len(text) >= span["end"], limit=remaining
                )
                if not sentence:
                    if len(text) >= remaining or voice["complete"]:
                        reason = "view_text"
                        break
                    await self.speech_changed.wait()
                    continue
                if not page.get("work_id"):
                    raise SpeechError("speech_work_identity_unavailable")
                self.segment_completed.clear()
                self.playing = {"id": voice["id"], "revision": 0, "sent": False, "foreground": True}
                await self.play(
                    sentence,
                    UsageScope(page["work_id"], self.source),
                    generation,
                    offset + len(sentence),
                    voice["settings"],
                )
                if generation != self.generation:
                    return
                await asyncio.wait_for(self.segment_completed.wait(), 30)
                voice["offset"] = offset + len(sentence)
                voice["spoken"] += len(sentence)
            if generation == self.generation:
                self.owner.devices.played(self.device, voice["id"], 0)
                await self.send("playback.finished", generation=generation, reason=reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.owner.devices.playback_state(self.device, voice["id"], 0, "unavailable")
            await self.send(
                "playback.unavailable",
                code=exc.code if isinstance(exc, SpeechError) else "speech_playback_failed",
            )
        finally:
            if generation == self.generation:
                self.playing = None
                self.voice = None
                self.credit = 0

    async def start_playback(self, message, offset=0):
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
        await self.send("playback.pending", generation=self.generation, message_id=self.playing["id"])
        self.play_task = self.spawn(self.play(text, scope, self.generation, offset + end), "playback")

    async def play(self, text, scope, generation, offset, settings=None):
        stream = None
        try:
            started = time.monotonic()
            first = True
            settings = settings or self.settings()
            stream = await self.context.invoke(
                SpeechRequest(
                    "synthesize", scope, text, voice=settings.get("voice") or None, rate=settings.get("rate")
                ),
                timeout=35,
            )
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
            if self.playing and self.playing.get("foreground"):
                raise
            self.playing = None
            self.credit = 0
            await self.send(
                "playback.unavailable",
                code=exc.code if isinstance(exc, SpeechError) else "speech_playback_failed",
            )
        finally:
            if stream:
                await stream.aclose()

    async def stop_playback(self):
        self.generation += 1
        if self.voice:
            self.owner.devices.playback_state(self.device, self.voice["id"], 0, "stopped")
        self.credit = 0
        self.credit_changed.set()
        if self.play_task and not self.play_task.done():
            self.play_task.cancel()
            await asyncio.gather(self.play_task, return_exceptions=True)
        self.playing = None
        self.voice = None
        self.speech_changed.set()
        self.segment_completed.set()
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
                    and self.settings()["speech_mode"] != "off"
                    and not self.input
                    and not self.confirmation
                    and not self.committing
                    and (not self.finish_task or self.finish_task.done())
                    and not self.playing
                    and (not self.play_task or self.play_task.done())
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
        self.thumbnail = None
        try:
            for task in tuple(self.tasks):
                task.cancel()
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            await self.abort_input(preserve_review=True)
        finally:
            await self.ws.close()
