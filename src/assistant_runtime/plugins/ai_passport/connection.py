"""Bounded terminal protocol. Socket lifetime never owns an accepted execution."""

import asyncio
import json
import logging
import struct
import time
import uuid
import zlib
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from aiohttp import WSMsgType

from assistant_runtime.api.ai_usage import UsageScope
from assistant_runtime.api.conversation import ConversationInputRequest, ConversationRequest
from assistant_runtime.api.speech import (
    PlaybackStream,
    SpeechError,
    SpeechRequest,
)
from assistant_runtime.api.terminal import TerminalNotifications
from . import protocol
from .display import TerminalDisplay
from .settings import LOCAL_FIELDS
from .speech_text import CHARACTER_LIMIT, readable_text, source_prefix


@dataclass(frozen=True)
class _SpeechSegment:
    text: str
    end: int
    scope: UsageScope


@dataclass
class _PreparedSpeech:
    stream: PlaybackStream
    chunks: AsyncIterator[bytes]
    first: bytes
    first_latency: float


class Connection:
    def __init__(self, owner, device, session, ws):
        self.owner, self.context = owner, owner.context
        self.device, self.session, self.ws = device, session, ws
        self.source = "terminal:" + device
        self.input = self.input_id = self.scope = None
        self.input_audio_id = None
        self.failed_input_id = None
        self.refused_input_id = None
        self.tasks = set()
        self.play_task = self.watch_task = self.finish_task = None
        self.pending_finish_stream = None
        self.generation = 0
        self.playing = None
        self.closed = False
        self.closing = None
        self.frame_lock = asyncio.Lock()
        self.cursor = 0
        self.started = time.time()
        self.send_lock = asyncio.Lock()
        self.credit = 0
        self.credit_changed = asyncio.Event()
        self.last_state = None
        self.last_notices = None
        self.legacy_notice_before = None
        self.last_partial = ""
        self.work = None
        self.confirmation = None
        self.committing = False
        self.changed = asyncio.Event()
        self.changed.set()
        self.speech_changed = asyncio.Event()
        self.playback_completed = asyncio.Event()
        self.voice = None
        self.display = TerminalDisplay(self.conversation, self.send, self.settings, self.send_binary)

    async def send(self, event_type, **data):
        text = protocol.encode_event(event_type, data)
        async with self.send_lock:
            await asyncio.wait_for(self.ws.send_str(text), 5)

    async def send_binary(self, data):
        async with self.send_lock:
            await asyncio.wait_for(self.ws.send_bytes(data), 5)

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
            protocol=protocol.VERSION,
            input_rate=protocol.INPUT_RATE,
            output_rate=protocol.OUTPUT_RATE,
            binding=self.session,
            server_time=int(time.time()),
            notification_list_ids=True,
            input_audio_ids=True,
            image_strips=True,
            playback_window=protocol.PLAYBACK_MAX_WINDOW,
            playback_metrics=True,
        )
        await self.send_configuration()
        await self.send("history.index", **history)
        await self.display.cache_index(history)
        # Reconnection displays history without replaying historical speech.
        for message in history["items"]:
            await self.display.restore_message(message)
        if self.closed:
            return
        self.watch_task = self.spawn(self.watch(), "state")
        self.spawn(self.watch_notices(), "notifications")
        async for frame in self.ws:
            async with self.frame_lock:
                if self.closed or frame.type not in {WSMsgType.TEXT, WSMsgType.BINARY}:
                    break
                await self.receive(frame)

    async def receive(self, frame):
        operation = None
        data = {}
        try:
            if frame.type == WSMsgType.BINARY:
                operation = "input.pcm"
                audio = frame.data
                if audio.startswith(protocol.INPUT_AUDIO_ID_TAG):
                    offset = len(protocol.INPUT_AUDIO_ID_TAG)
                    end = offset + protocol.INPUT_AUDIO_ID_BYTES
                    if self.input is None or self.input_audio_id != audio[offset:end]:
                        return  # Late data or audio for a start rejected before prepare.
                    audio = audio[end:]
                elif self.input_audio_id is not None:
                    return
                if self.input is None and self.failed_input_id:
                    return  # The failed input may still have PCM queued on the device.
                if self.input is None or len(audio) < 6:
                    raise SpeechError("speech_input_not_started")
                sequence = struct.unpack("!I", audio[:4])[0]
                await self.input.accept(sequence, audio[4:])
            else:
                payload = json.loads(frame.data)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid command")
                data = payload
                operation = data.get("type") if isinstance(data.get("type"), str) else None
                await self.command(data)
        except Exception as exc:
            request_id = data.get("request_id")
            if (frame.type == WSMsgType.BINARY and self.input is not None) or (
                operation in {"input.begin", "input.finish", "input.confirm", "input.cancel"}
                and self.input_id is not None
                and request_id == self.input_id
            ):
                await self.fail_capture(exc, self.input_id, operation)
            else:
                ident = (
                    request_id[:36]
                    if operation == "notifications.list" and isinstance(request_id, str)
                    else None
                )
                if (
                    operation
                    and operation.startswith("image.stream.")
                    and type(request_id) is int
                    and 0 < request_id < 2**32
                ):
                    ident = request_id
                await self.error(exc, request_id=ident, operation=operation)

    async def fail_capture(self, exc, ident, operation):
        self.failed_input_id = ident
        if self.input:
            self.context.log.warning(
                "passport_input_rejected input=%s operation=%s frames=%s audio_bytes=%s max_audio_bytes=%s",
                ident,
                operation,
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
        kind = protocol.command_type(data)
        if kind.startswith("input."):
            ident = str(uuid.UUID(data.get("request_id")))
            if kind == "input.receipt":
                await self.input_receipt(ident, await self.receipt("receipt", ident))
                return
            if kind == "input.begin":
                audio_ids = data.get("audio_ids", False)
                if type(audio_ids) is not bool:
                    raise ValueError("Invalid input audio framing")
                if self.input or self.confirmation or self.finish_task and not self.finish_task.done():
                    if ident != self.input_id:
                        self.refused_input_id = ident
                        await self.error(SpeechError("speech_busy"), request_id=ident, operation=kind)
                    return
                await self.stop_playback()
                result = await self.receipt(
                    "prepare", ident, source=self.source, mode=data.get("mode", "steer")
                )
                if result["state"] != "prepared":
                    await self.input_receipt(ident, result)
                    return
                self.input_id = ident
                self.input_audio_id = uuid.UUID(ident).bytes if audio_ids else None
                self.failed_input_id = None
                self.confirmation = None
                self.last_partial = ""
                self.scope = UsageScope(result["work_id"], self.source)
                self.work = result["work_id"]
                self.input = await self.context.invoke(SpeechRequest("recognize", self.scope), timeout=35)
                self.spawn(self.watch_transcript(self.input, ident), "transcript")
                if audio_ids:
                    self.spawn(self.send_input_ready(self.input, ident), "input-ready")
                else:
                    await self.send("input.ready", request_id=ident, max_audio_bytes=self.input.max_bytes)
                return
            if ident != self.input_id:
                if kind == "input.cancel" and ident == self.refused_input_id:
                    # A legacy client can cancel a start that was refused before prepare.
                    await self.send("input.receipt", request_id=ident, state="cancelled")
                    return
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
                self.pending_finish_stream = stream
                self.finish_task = self.spawn(self.finish(stream, ident, confirm), "recognition")
            else:
                raise SpeechError("speech_input_closed")
            return
        if kind.startswith("image."):
            await self.display.image(kind, data)
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
            await self.display.history_index(data)
        elif kind == "playback.credit":
            credit = data.get("bytes")
            if type(credit) is not int or credit <= 0 or credit % 2 or credit > protocol.PLAYBACK_MAX_WINDOW:
                raise ValueError("Invalid playback credit")
            if data.get("generation") == self.generation:
                if self.credit + credit > protocol.PLAYBACK_MAX_WINDOW:
                    raise ValueError("Playback window exceeded")
                self.credit += credit
                self.credit_changed.set()
        elif kind == "playback.stop":
            await self.stop_playback()
        elif kind == "playback.completed":
            if self.playing and data.get("generation") == self.generation and self.playing.get("sent"):
                if "pcm" in data:
                    self.verify_playback_pcm(protocol.playback_pcm(data["pcm"]))
                if not self.playing.get("foreground") and not self.playing.get("failed"):
                    self.owner.devices.played(self.device, self.playing["id"], self.playing["revision"])
                if self.playing.get("notification") and not self.playing.get("failed"):
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
                await self.send(
                    "playback.receipt",
                    generation=self.generation,
                    played=not self.playing.get("failed", False),
                )
                self.credit = 0
                self.playing = None
                self.playback_completed.set()
        elif kind == "message.page":
            await self.display.message_page(data)
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
            request_id = data.get("request_id")
            if request_id is not None:
                request_id = str(uuid.UUID(request_id))
            else:
                self.legacy_notice_before = data.get("before")
            await self.notices(data.get("before"), requested=True, request_id=request_id)
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

    async def send_input_ready(self, stream, ident):
        if self.closed or self.input is not stream:
            return
        try:
            await self.send("input.ready", request_id=ident, max_audio_bytes=stream.max_bytes)
        except (ConnectionError, TimeoutError):
            await self.ws.close(code=1011)

    async def commit(self, ident, text):
        self.committing = True
        try:
            result = await self.receipt("commit", ident, text=text)
            if not self.closed:
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
        # Until this task actually starts, abort_input owns the parked stream.
        self.pending_finish_stream = None
        try:
            await self.send("recognizing", request_id=ident)
            ended = time.monotonic()
            try:
                text = await stream.finish()
            except Exception as exc:
                await self.receipt("cancel", ident)
                self.failed_input_id, self.input_id = ident, None
                if isinstance(exc, TimeoutError):
                    exc = SpeechError("speech_provider_timeout")
                await self.error(exc, request_id=ident, operation="input.finish")
                return
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
        if self.pending_finish_stream is not None:
            stream, self.pending_finish_stream = self.pending_finish_stream, None
            await stream.close()
        if self.input:
            stream, self.input = self.input, None
            self.context.log.info(
                "passport_capture_cancelled input=%s frames=%s audio_bytes=%s",
                ident,
                stream.sequence,
                stream.size,
            )
            await stream.close()
        self.confirmation = None
        self.input_id = None
        if ident:
            if preserve_review and (await self.receipt("receipt", ident))["state"] in {
                "review",
                "accepted",
                "unknown",
            }:
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
                    await self.display.cache_index(history)
                    for m in history["items"]:
                        await self.display.restore_message(m)
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
                        await self.display.cache_index(index)
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
            async with self.frame_lock:
                if not self.closed and self.input is stream and self.input_id == ident:
                    await self.fail_capture(
                        SpeechError("speech_input_deadline") if isinstance(exc, TimeoutError) else exc,
                        ident,
                        "input.pcm",
                    )

    async def watch_notices(self):
        try:
            while not self.closed:
                if self.legacy_notice_before is None:
                    await self.notices()
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.error(exc)

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

    async def next_speech_segment(self, voice, offset, spoken):
        while not self.closed and self.voice is voice:
            self.speech_changed.clear()
            span = next((r for r in voice["ranges"] if r["end"] > offset), None)
            if not span:
                if voice["complete"]:
                    return None, "complete"
                await self.speech_changed.wait()
                continue
            offset = max(span["start"], offset)
            limit = (
                CHARACTER_LIMIT - spoken if voice["settings"]["speech_mode"] == "brief" else CHARACTER_LIMIT
            )
            if limit <= 0:
                return None, "brief_limit"
            # A full-mode page is bounded by the provider's single-request limit.
            page = await self.conversation("message", message_id=voice["id"], offset=offset, limit=limit)
            if self.closed or self.voice is not voice:
                break
            text = page["text"][: span["end"] - offset]
            end = source_prefix(
                text, limit, complete=voice["complete"], end_of_span=offset + len(text) == span["end"]
            )
            if end:
                speech = readable_text(text[:end])
                if not speech:
                    offset += end
                    continue
                if not page.get("work_id"):
                    raise SpeechError("speech_work_identity_unavailable")
                return _SpeechSegment(speech, offset + end, UsageScope(page["work_id"], self.source)), None
            if voice["complete"]:
                return None, "view_text"
            await self.speech_changed.wait()
        return None, "stopped"

    async def prepare_next_speech(self, voice, offset, spoken):
        segment, reason = await self.next_speech_segment(voice, offset, spoken)
        if not segment:
            return None, reason, None
        opened_at = time.monotonic()
        try:
            stream = await self.open_synthesis(segment.text, segment.scope, voice["settings"])
        except SpeechError as exc:
            if exc.code != "speech_busy":
                raise
            # Admission declined before dispatch. Open after returning the current stream's slot.
            return segment, None, None
        try:
            chunks = aiter(stream)
            first = await self.first_pcm(chunks)
            return segment, None, _PreparedSpeech(stream, chunks, first, time.monotonic() - opened_at)
        except TimeoutError as exc:
            await stream.aclose()
            raise SpeechError("speech_provider_timeout") from exc
        except BaseException:
            await stream.aclose()
            raise

    @staticmethod
    async def first_pcm(chunks):
        while (chunk := await anext(chunks, None)) is not None:
            if chunk:
                return chunk
        raise SpeechError("speech_empty_audio")

    async def speech_audio(self, text, scope, settings, generation, prepared=None):
        stream = prepared.stream if prepared else None
        try:
            started = time.monotonic()
            if stream is None:
                stream = await self.open_synthesis(text, scope, settings)
                chunks = aiter(stream)
                first = await self.first_pcm(chunks)
                latency = time.monotonic() - started
            else:
                chunks, first, latency = prepared.chunks, prepared.first, prepared.first_latency
            self.context.log.info(
                "passport_first_audio device=%s generation=%s elapsed_seconds=%.3f",
                self.device,
                generation,
                latency,
            )
            yield first
            while (chunk := await anext(chunks, None)) is not None:
                if chunk:
                    yield chunk
        except TimeoutError as exc:
            raise SpeechError("speech_provider_timeout") from exc
        finally:
            if stream:
                await stream.aclose()

    async def foreground_audio(self, voice, generation):
        pending = prepared = None
        try:
            segment, reason = await self.next_speech_segment(voice, voice["offset"], voice["spoken"])
            while segment:
                spoken = voice["spoken"] + len(segment.text)
                current, prepared = prepared, None
                chunks = self.speech_audio(
                    segment.text, segment.scope, voice["settings"], generation, current
                )
                async with aclosing(chunks):
                    async for pcm in chunks:
                        if pending is None:
                            pending = asyncio.create_task(
                                self.prepare_next_speech(voice, segment.end, spoken)
                            )
                        yield pcm
                voice["offset"], voice["spoken"] = segment.end, spoken
                segment, reason, prepared = await asyncio.shield(pending)
                pending = None
            voice["reason"] = reason
        finally:
            if pending:
                pending.cancel()
                (result,) = await asyncio.gather(pending, return_exceptions=True)
                if isinstance(result, tuple):
                    _, _, prepared = result
            if prepared:
                await prepared.stream.aclose()

    async def speak_foreground(self, voice):
        generation = self.generation
        self.playback_completed.clear()
        playing = self.playing = {"id": voice["id"], "revision": 0, "sent": False, "foreground": True}
        try:
            started = await self.send_audio(
                self.foreground_audio(voice, generation),
                generation,
                voice["offset"],
                end_offset=lambda: voice["offset"],
            )
            if started and generation == self.generation:
                await asyncio.wait_for(self.playback_completed.wait(), 30)
                if playing.get("integrity_failed"):
                    raise SpeechError("speech_playback_integrity")
            if not self.closed and self.voice is voice and generation == self.generation:
                self.owner.devices.playback_state(
                    self.device, voice["id"], 0, "played" if started else "unavailable"
                )
                await self.send("playback.finished", generation=generation, reason=voice["reason"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed and generation == self.generation:
                self.owner.devices.playback_state(self.device, voice["id"], 0, "unavailable")
                await self.playback_unavailable(exc, generation)
        finally:
            if generation == self.generation:
                self.playing = self.voice = None
                self.credit = 0

    async def start_playback(self, message, offset=0):
        if self.input:
            raise SpeechError("speech_busy")
        await self.stop_playback()
        source = message["text"]
        prefix = source[:CHARACTER_LIMIT]
        end = source_prefix(prefix, CHARACTER_LIMIT, complete=True, end_of_span=len(prefix) == len(source))
        text = readable_text(prefix[:end])
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
        self.play_task = self.spawn(self.play(text, scope, self.generation, offset, offset + end), "playback")

    async def open_synthesis(self, text, scope, settings):
        return await self.context.invoke(
            SpeechRequest(
                "synthesize", scope, text, voice=settings.get("voice") or None, rate=settings.get("rate")
            ),
            timeout=35,
        )

    def verify_playback_pcm(self, pcm):
        sent = self.playing["sent_pcm"]
        received = pcm["received_bytes"], pcm["received_crc32"]
        written = pcm["i2s_bytes"], pcm["i2s_crc32"]
        intact = sent == received == written
        if not intact:
            self.playing["integrity_failed"] = self.playing["failed"] = True
        self.context.log.log(
            logging.INFO if intact else logging.WARNING,
            "passport_playback_pcm device=%s generation=%s window=%d sent=%d crc=%08x "
            "received=%d crc=%08x i2s=%d crc=%08x underruns=%d intact=%s",
            self.device, self.generation, pcm["window_bytes"], *sent, *received, *written,
            pcm["underruns"], intact,
        )

    async def finish_audio(self, generation, end_offset, sent_pcm):
        self.playing["sent"] = True
        self.playing["sent_pcm"] = sent_pcm
        self.context.log.info(
            "passport_playback_sent device=%s generation=%s bytes=%d crc=%08x",
            self.device, generation, *sent_pcm,
        )
        await self.send("playback.end", generation=generation, next_offset=end_offset())

    async def send_audio(self, audio, generation, offset, *, end_offset):
        started = False
        sent_bytes = sent_crc = 0
        async with aclosing(audio):
            while True:
                try:
                    pcm = await anext(audio, None)
                    if pcm is not None and (not isinstance(pcm, bytes) or len(pcm) % 2):
                        raise SpeechError("speech_invalid_pcm")
                except Exception:
                    if started and generation == self.generation and not self.closed:
                        # The provider stopped after sending a prefix. Drain that prefix once.
                        self.playing["failed"] = True
                        await self.finish_audio(generation, end_offset, (sent_bytes, sent_crc))
                        try:
                            await asyncio.wait_for(self.playback_completed.wait(), 30)
                        except TimeoutError:
                            pass
                    raise
                if generation != self.generation or self.closed:
                    return False
                if pcm is None:
                    break
                if not started:
                    await self.send(
                        "playback.start",
                        generation=generation,
                        rate=protocol.OUTPUT_RATE,
                        offset=offset,
                        next_offset=offset,
                    )
                    started = True
                cursor = 0
                while cursor < len(pcm):
                    async with asyncio.timeout(30):
                        while not self.credit:
                            self.credit_changed.clear()
                            await self.credit_changed.wait()
                    if generation != self.generation or self.closed:
                        return False
                    count = min(self.credit, len(pcm) - cursor, protocol.PLAYBACK_CHUNK)
                    self.credit -= count
                    chunk = pcm[cursor : cursor + count]
                    try:
                        await self.send_binary(struct.pack("!I", generation) + chunk)
                    except (ConnectionError, TimeoutError):
                        self.context.log.warning(
                            "passport_playback_send_interrupted device=%s generation=%s bytes=%d crc=%08x",
                            self.device, generation, sent_bytes, sent_crc,
                        )
                        raise
                    sent_bytes += count
                    sent_crc = zlib.crc32(chunk, sent_crc)
                    cursor += count
        if started:
            await self.finish_audio(generation, end_offset, (sent_bytes, sent_crc))
        return started

    async def playback_unavailable(self, exc, generation):
        code = exc.code if isinstance(exc, SpeechError) else "speech_playback_failed"
        self.context.log.warning("passport_playback_unavailable generation=%s code=%s", generation, code)
        await self.send("playback.unavailable", generation=generation, code=code)

    async def play(self, text, scope, generation, offset, end):
        self.playback_completed.clear()
        try:
            await self.send_audio(
                self.speech_audio(text, scope, self.settings(), generation),
                generation,
                offset,
                end_offset=lambda: end,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation == self.generation and not self.closed:
                self.playing = None
                self.credit = 0
                await self.playback_unavailable(exc, generation)

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
        self.playback_completed.set()
        if not self.closed:
            await self.send("playback.stop", generation=self.generation)

    async def notices(self, before=None, *, requested=False, request_id=None):
        if not self.context.available(TerminalNotifications):
            if requested:
                raise SpeechError("terminal_notifications_unavailable")
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
        payload = {"items": items, "before": page["before"], "quiet": page["quiet"]}
        if requested:
            if request_id is not None:
                payload["request_id"] = request_id
            await self.send("notifications", **payload)
            return
        fresh = self.last_notices is not None
        if items != self.last_notices:
            await self.send("notifications", **payload)
            self.last_notices = items
            for item in page["items"]:
                eligible = self.owner.devices.claim_playback(
                    self.device, "notification:" + item["id"], item["revision"]
                )
                if (
                    eligible
                    and fresh
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
        if self.closing is None:
            self.closed = True
            self.closing = asyncio.create_task(self._close())
        await asyncio.shield(self.closing)

    async def _close(self):
        # Let an already accepted frame finish before touching its input receipt.
        async with self.frame_lock:
            pass
        self.display.close_image()
        try:
            tasks = tuple(self.tasks)
            for task in tasks:
                if task is self.finish_task and self.committing:
                    # Dispatch has crossed the durable receipt boundary. Wait for
                    # the capability call to settle even if the radio dropped.
                    continue
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.abort_input(preserve_review=True)
        finally:
            await self.ws.close()
