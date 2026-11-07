"""Bounded, cancellable audio transactions and literal spoken excerpts."""

import asyncio
import time

from assistant_runtime.api.speech import SpeechError, RecognitionRequest, SynthesisRequest


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

    async def accept(self, sequence, pcm):
        async with self.lock:
            if self.closed:
                raise SpeechError("speech_input_closed")
            if (
                type(sequence) is not int
                or sequence != self.sequence
                or not isinstance(pcm, bytes)
                or not pcm
                or len(pcm) % 2
                or len(pcm) > 8192
                or self.size + len(pcm) > self.max_bytes
                or time.monotonic() - self.started > self.max_seconds + 10
            ):
                await self.close()
                raise SpeechError("speech_input_gap_or_limit")
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
                upstream = await self.context.invoke(
                    RecognitionRequest(
                        request.scope, max_seconds=self.context.config["max_recording_seconds"]
                    ),
                    timeout=30,
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
            self.opening += 1
            try:
                upstream = await self.context.invoke(
                    SynthesisRequest(request.scope, request.text), timeout=30
                )
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
