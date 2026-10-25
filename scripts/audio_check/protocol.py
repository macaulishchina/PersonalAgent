"""Human benchmark orchestration only; never wakes or controls the assistant."""

import asyncio
import os
import sys
import wave

from assistant_runtime.api.system import system

import numpy as np

from .guide import CUES, POSITIONS, READING
from .metrics import RATE


def write_wave(path, pcm):
    directory = system.directory(path.parent)
    try:
        descriptor = directory.open_file(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "wb") as file, wave.open(file, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(RATE)
            audio.writeframes(pcm)
    finally:
        directory.close()


def keyword_events(detector, pcm):
    detector.reset()
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    events = []
    for start in range(0, len(audio), 1280):
        events.extend(detector.feed(audio[start : start + 1280]))
    events.extend(detector.feed(np.zeros(RATE, dtype=np.float32)))
    return [
        {"seconds": round(t, 3), "phrase": word, "score": round(float(score), 4)} for t, word, score in events
    ]


class Protocol:
    def __init__(self, capture, player, report, folder, detector=None, *, save_audio=False):
        self.capture, self.player, self.report = capture, player, report
        self.folder, self.detector, self.save_audio = folder, detector, save_audio

    async def wait(self, seconds):
        for _ in range(seconds):
            await self.capture.ready()
            await asyncio.sleep(1)

    async def cue(self, name):
        print(CUES[name], file=sys.stderr, flush=True)
        await self.capture.ready()
        if self.player is not None:
            await self.player.play(name, self.capture.ready)

    async def measure(self, environment, position, kind, seconds):
        print(f"录音开始：{environment} / {position} / {kind}，{seconds}秒。", file=sys.stderr, flush=True)
        pcm, measured, findings = await self.capture.record(seconds)
        row = {
            "environment": environment,
            "position": position,
            "kind": kind,
            "measurement": measured,
            "findings": findings,
            "reference": ((self.report["wake_words"][0] + "，") if kind == "wake_reading" else "") + READING
            if kind in {"reading", "wake_reading"}
            else None,
        }
        self.report["phases"].append(row)
        if self.save_audio:
            path = self.folder / f"{environment}-{position}-{kind}.wav"
            write_wave(path, pcm)
            row["file"] = str(path)
        if self.detector is not None:
            row["offline_keyword_events"] = await asyncio.to_thread(keyword_events, self.detector, pcm)
        return row

    async def run(self):
        await self.cue("intro")
        await self.cue("ready")
        await self.wait(10)
        for environment in ("quiet", "noise"):
            if environment == "noise":
                await self.cue("noise")
                await self.wait(20)
            for position in POSITIONS:
                await self.cue(position)
                await self.wait(15)
                await self.cue("quiet")
                await self.wait(1)
                await self.measure(environment, position, "background", 3)
                for kind, cue, seconds in (("reading", "read", 10), ("wake_reading", "wake_read", 20)):
                    await self.cue(cue)
                    await self.cue("start")
                    await self.wait(2)
                    await self.measure(environment, position, kind, seconds)
                    await self.cue("end")
        await self.cue("restore_quiet")
        await self.wait(10)
        await self.cue("complete")
