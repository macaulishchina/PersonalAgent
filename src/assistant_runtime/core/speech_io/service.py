"""Bounded, cancellable audio transactions and literal spoken excerpts."""

import asyncio
import time
from collections import deque

from assistant_runtime.api.speech import SpeechError, RecognitionRequest, SynthesisRequest


class BufferedRecognition:
    """Accept bounded PCM while the provider opens, preserving the first sound.

    This buffer lives on PA, not the no-PSRAM terminal. There is one provider
    opening and one ordered sender, with no retry or incomplete-input fallback.
    """

    def __init__(self, context, request):
        self.context, self.request = context, request
        self.upstream = None
        self.chunks = deque()
        self.pending_bytes = 0
        self.changed = asyncio.Event()
        self.opened = asyncio.Event()
        self.ended = self.closed = False
        self.task = context.spawn(self.run(), "speech-recognition")
        self.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    @property
    def partial(self):
        return getattr(self.upstream, "partial", "")

    async def send(self, pcm):
        if self.closed or self.ended:
            raise SpeechError("speech_input_closed")
        if self.task.done():
            await self.task
            raise SpeechError("speech_input_closed")
        # Both byte and item limits cover tiny frames as well as large ones.
        if self.pending_bytes + len(pcm) > 65536 or len(self.chunks) >= 256:
            raise SpeechError("speech_input_buffer_limit")
        self.chunks.append(pcm)
        self.pending_bytes += len(pcm)
        self.changed.set()

    async def run(self):
        try:
            self.upstream = await self.context.invoke(self.request, timeout=30)
            self.opened.set()
            while True:
                if self.chunks:
                    pcm = self.chunks.popleft()
                    await asyncio.wait_for(self.upstream.send(pcm), 10)
                    self.pending_bytes -= len(pcm)
                elif self.ended:
                    return await self.upstream.finish()
                else:
                    self.changed.clear()
                    await self.changed.wait()
        finally:
            self.opened.set()
            if self.upstream:
                await self.upstream.close()

    async def wait_update(self, previous):
        await self.opened.wait()
        if self.task.done():
            await self.task
            raise StopAsyncIteration
        update = getattr(self.upstream, "wait_update", None)
        if not callable(update):
            raise StopAsyncIteration
        return await update(previous)

    async def finish(self):
        if self.closed:
            raise SpeechError("speech_input_closed")
        self.ended = True
        self.changed.set()
        return await self.task

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.chunks.clear()
        self.pending_bytes = 0


class InputStream:
    def __init__(self, upstream, max_seconds, release):
        self.upstream, self.release = upstream, release
        self.max_bytes = max_seconds * 32000
        self.max_seconds = max_seconds
        self.sequence = self.size = 0
        self.closed = False
        self.started = time.monotonic()
        self.lock = asyncio.Lock()

    @property
    def partial(self):
        return getattr(self.upstream, "partial", "")

    async def wait_update(self, previous):
        if self.closed:
            raise StopAsyncIteration
        update = getattr(self.upstream, "wait_update", None)
        if not callable(update):
            raise StopAsyncIteration
        return await update(previous)

    async def accept(self, sequence, pcm):
        async with self.lock:
            if self.closed:
                raise SpeechError("speech_input_closed")
            code = (
                "speech_input_gap"
                if type(sequence) is not int or sequence != self.sequence
                else "speech_invalid_pcm"
                if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > 8192
                else "speech_input_limit"
                if self.size + len(pcm) > self.max_bytes
                else "speech_input_deadline"
                if time.monotonic() - self.started > self.max_seconds + 10
                else None
            )
            if code:
                await self.close()
                raise SpeechError(code)
            self.sequence += 1
            self.size += len(pcm)
            try:
                await asyncio.wait_for(self.upstream.send(pcm), 10)
            except BaseException:
                await self.close()
                raise

    async def finish(self):
        async with self.lock:
            if self.closed or not self.size:
                await self.close()
                raise SpeechError("speech_empty_or_closed")
            try:
                text = await asyncio.wait_for(self.upstream.finish(), 30)
                if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                    raise SpeechError("speech_no_transcript")
                return text.strip()
            finally:
                await self.close()

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await self.upstream.close()
            finally:
                self.release(self)


class OutputStream:
    def __init__(self, upstream, release):
        self.upstream, self.release = upstream, release
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            return await asyncio.wait_for(self.upstream.__anext__(), 30)
        except BaseException:
            await self.close()
            raise

    async def aclose(self):
        await self.close()

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await self.upstream.aclose()
            finally:
                self.release(self)


class SpeechService:
    def __init__(self, context):
        self.context = context
        self.streams = set()
        self.opening = 0
        self.closed = False

    async def request(self, request):
        if self.closed:
            raise SpeechError("speech_unavailable")
        if request.action == "recognize":
            if len(self.streams) + self.opening >= self.context.config["max_streams"]:
                raise SpeechError("speech_busy")
            self.opening += 1
            try:
                upstream = BufferedRecognition(
                    self.context,
                    RecognitionRequest(
                        request.scope, max_seconds=self.context.config["max_recording_seconds"]
                    ),
                )
                if self.closed:
                    await upstream.close()
                    raise SpeechError("speech_unavailable")
                stream = InputStream(
                    upstream, self.context.config["max_recording_seconds"], self.streams.discard
                )
                self.streams.add(stream)
                return stream
            finally:
                self.opening -= 1
        if request.action == "synthesize":
            if not isinstance(request.text, str) or not request.text.strip() or len(request.text) > 200:
                raise SpeechError("speech_invalid_text")
            if len(self.streams) + self.opening >= self.context.config["max_streams"]:
                raise SpeechError("speech_busy")
            provider_request = SynthesisRequest(
                request.scope, request.text, voice=request.voice, rate=request.rate
            )
            self.opening += 1
            try:
                upstream = await self.context.invoke(provider_request, timeout=30)
                if self.closed:
                    await upstream.aclose()
                    raise SpeechError("speech_unavailable")
                stream = OutputStream(upstream, self.streams.discard)
                self.streams.add(stream)
                return stream
            finally:
                self.opening -= 1
        raise SpeechError("speech_invalid_action")

    async def start(self, context):
        from assistant_runtime.api.speech import SpeechRequest

        context.register(SpeechRequest, self.request, timeout=35)
        context.own(self.close)

    async def close(self):
        self.closed = True
        await asyncio.gather(*(stream.close() for stream in tuple(self.streams)), return_exceptions=True)
