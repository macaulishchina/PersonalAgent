"""DashScope duplex transport. One metered attempt, no agent or implicit retries."""

import asyncio
import json
import math
import re
import uuid
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError

from assistant_runtime.api.speech import SpeechError, RecognitionRequest, SynthesisRequest
from .declaration import DECLARATION


class Duplex:
    def __init__(self, owner, request, ticket):
        self.owner, self.request, self.ticket = owner, request, ticket
        self.id = uuid.uuid4().hex
        self.ws = None
        self.sent = self.bytes = 0
        self.done = self.closed = False
        self.sentences = {}
        self.reader = None
        self.partial = ""
        self.updated = asyncio.Event()
        self.deadline = time.monotonic() + (
            request.max_seconds + 45 if request.action == "recognize" else 120
        )

    async def command(self, action, payload):
        await self.ws.send_json(
            {"header": {"action": action, "task_id": self.id, "streaming": "duplex"}, "payload": payload}
        )

    async def event(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SpeechError("speech_stream_deadline")
        message = await self.ws.receive(timeout=min(remaining, self.owner.settings["timeout_seconds"]))
        if message.type == WSMsgType.BINARY:
            if len(message.data) % 2:
                raise SpeechError("speech_invalid_pcm")
            return message.data
        if message.type != WSMsgType.TEXT:
            raise SpeechError("speech_stream_disconnected")
        try:
            event = json.loads(message.data)
            header = event["header"]
        except (ValueError, KeyError, TypeError):
            raise SpeechError("speech_invalid_response") from None
        if header.get("task_id") != self.id:
            raise SpeechError("speech_response_identity_mismatch")
        if header.get("event") == "task-failed":
            # Never echo upstream message, which can contain submitted text or credentials.
            code = str(header.get("error_code", ""))
            category = "auth" if code in {"InvalidApiKey", "AccessDenied"} else "provider"
            safe_code = code if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", code) else "unspecified"
            raise SpeechError("speech_" + category + "_rejected:" + safe_code)
        return event

    async def open(self):
        self.ws = await self.owner.client.ws_connect(
            self.owner.settings["endpoint"],
            headers={"Authorization": "Bearer " + self.owner.key},
            max_msg_size=512 * 1024,
            heartbeat=15,
        )
        is_asr = self.request.action == "recognize"
        parameters = {"format": "pcm", "sample_rate": 16000 if is_asr else 24000}
        if not is_asr:
            parameters.update(
                voice=self.request.voice or self.owner.settings["voice"],
                rate=self.request.rate if self.request.rate is not None else self.owner.settings["rate"],
            )
        self.sent = 1  # A failed send may still have reached the provider.
        await self.command(
            "run-task",
            {
                "task_group": "audio",
                "task": "asr" if is_asr else "tts",
                "function": "recognition" if is_asr else "SpeechSynthesizer",
                "model": self.owner.settings["recognition_model" if is_asr else "synthesis_model"],
                "parameters": parameters,
                "input": {},
            },
        )
        event = await self.event()
        if not isinstance(event, dict) or event["header"].get("event") != "task-started":
            raise SpeechError("speech_not_started")
        if is_asr:
            self.reader = asyncio.create_task(self.read_recognition())
        return self

    async def read_recognition(self):
        while True:
            event = await self.event()
            if not isinstance(event, dict):
                raise SpeechError("speech_invalid_response")
            kind = event["header"].get("event")
            if kind == "task-finished":
                self.done = True
                self.updated.set()
                return "".join(self.sentences.values())
            if kind == "result-generated":
                sentence = event.get("payload", {}).get("output", {}).get("sentence", {})
                partial = sentence.get("text", "")
                if not isinstance(partial, str):
                    raise SpeechError("speech_invalid_response")
                if sentence.get("sentence_end") is True:
                    text = sentence.get("text")
                    if not isinstance(text, str) or len(text) > 16000:
                        raise SpeechError("speech_invalid_response")
                    key = str(sentence.get("sentence_id", sentence.get("begin_time", "")))
                    self.sentences[key] = text
                    if sum(map(len, self.sentences.values())) > 16000:
                        raise SpeechError("speech_transcript_limit")
                self.partial = "".join(self.sentences.values()) + (
                    "" if sentence.get("sentence_end") is True else partial
                )
                if len(self.partial) > 16000:
                    raise SpeechError("speech_transcript_limit")
                self.updated.set()

    async def wait_update(self, previous):
        while self.partial == previous:
            if self.reader.done():
                await self.reader
                raise StopAsyncIteration
            self.updated.clear()
            waiter = asyncio.create_task(self.updated.wait())
            try:
                await asyncio.wait((waiter, self.reader), return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        return self.partial

    async def send(self, pcm):
        if self.closed or self.done:
            raise SpeechError("speech_input_closed")
        if self.reader.done():
            await self.reader
            raise SpeechError("speech_input_closed")
        self.bytes += len(pcm)
        if self.bytes > self.request.max_seconds * 32000:
            raise SpeechError("speech_input_limit")
        await self.ws.send_bytes(pcm)

    async def finish(self):
        await self.command("finish-task", {"input": {}})
        return await self.reader

    async def playback(self):
        try:
            await self.command("continue-task", {"input": {"text": self.request.text}})
            await self.command("finish-task", {"input": {}})
            while True:
                event = await self.event()
                if isinstance(event, bytes):
                    self.bytes += len(event)
                    if self.bytes > 24000 * 2 * 90:
                        raise SpeechError("speech_output_limit")
                    yield event
                elif event["header"].get("event") == "task-finished":
                    self.done = True
                    return
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.reader and not self.reader.done():
                self.reader.cancel()
            if self.reader:
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.ws:
                await self.ws.close()
        finally:
            self.owner.streams.discard(self)
            state = "completed" if self.done else "unknown" if self.sent else "not_sent"
            usage = {"input_tokens": 0, "output_tokens": 0}
            if self.done:
                usage.update(
                    {"audio_seconds": math.ceil(self.bytes / 32000)}
                    if self.request.action == "recognize"
                    else {"text_characters": len(self.request.text)}
                )
            await self.owner.usage.settle(
                self.ticket["id"], state=state, usage=usage, provider_request_id=self.id
            )


class Synthesis:
    """Closing before first iteration must still release the admitted provider stream."""

    def __init__(self, duplex):
        self.duplex = duplex
        self.iterator = duplex.playback()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.iterator.__anext__()

    async def aclose(self):
        try:
            await self.iterator.aclose()
        finally:
            await self.duplex.close()


class AliyunSpeech:
    manifest = DECLARATION.manifest

    def __init__(self, context):
        self.context, self.settings = context, context.config
        self.streams = set()
        self.client = None
        self.key = ""
        self.opening = set()
        self.closing = False

    async def start(self, context):
        self.key = self.context.credential("DASHSCOPE_API_KEY") or ""
        self.usage = self.context.resource("usage")
        self.client = ClientSession(timeout=ClientTimeout(total=None, sock_connect=15), trust_env=False)
        context.own(self.close)
        if "recognize" in self.settings["capabilities"]:
            context.register(RecognitionRequest, self.request, timeout=30)
        if "synthesize" in self.settings["capabilities"]:
            context.register(SynthesisRequest, self.request, timeout=30)
        if not self.key:
            context.set_health("degraded", "speech_credentials_required")

    async def request(self, request):
        if not self.key:
            raise SpeechError("speech_credentials_required")
        if request.action not in self.settings["capabilities"]:
            raise SpeechError("speech_invalid_action")
        if request.action == "recognize" and (
            type(request.max_seconds) is not int or not 1 <= request.max_seconds <= 120
        ):
            raise SpeechError("speech_input_limit")
        if request.action == "synthesize" and (not request.text.strip() or len(request.text) > 200):
            raise SpeechError("speech_invalid_text")
        if self.closing or len(self.streams) + len(self.opening) >= 4:
            raise SpeechError("speech_busy")
        opening = asyncio.current_task()
        self.opening.add(opening)
        stream = None
        try:
            is_asr = request.action == "recognize"
            bounds = {"input_tokens": 0, "output_tokens": 0}
            bounds.update(
                {"audio_seconds": request.max_seconds} if is_asr else {"text_characters": len(request.text)}
            )
            ticket = await self.usage.admit(
                request.scope,
                service="speech_asr" if is_asr else "speech_tts",
                provider="aliyun_speech",
                model=self.settings["recognition_model" if is_asr else "synthesis_model"],
                bounds=bounds,
            )
            stream = Duplex(self, request, ticket)
            await stream.open()
            self.streams.add(stream)
            return stream if is_asr else Synthesis(stream)
        except WSServerHandshakeError as exc:
            if stream:
                await stream.close()
            raise SpeechError(
                "speech_auth_rejected" if exc.status in {401, 403} else "speech_connection_rejected"
            ) from None
        except BaseException:
            if stream:
                await stream.close()
            raise
        finally:
            self.opening.discard(opening)

    async def close(self):
        self.closing = True
        opening = tuple(self.opening)
        for task in opening:
            task.cancel()
        await asyncio.gather(*opening, return_exceptions=True)
        await asyncio.gather(*(s.close() for s in tuple(self.streams)), return_exceptions=True)
        if self.client:
            await self.client.close()
