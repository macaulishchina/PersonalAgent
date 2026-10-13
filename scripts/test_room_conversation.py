"""Real microphone, Bluetooth acoustics and subscription voice in an isolated task.

Stop production first. Synthetic speech is played outside the assistant AEC path.
This explicitly records a short room test; normal service recording stays disabled.
"""

import asyncio
import argparse
import json
import logging
from pathlib import Path
import time
import wave

import numpy as np

from assistant_runtime.plugins.pipewire.audio import audio_env
from assistant_runtime.config import Config
from assistant_runtime.assistant.service import AssistantService
from test_room_audio import record, wav

ROOT = Path(__file__).resolve().parents[1]


async def main():
    root = ROOT / "state" / f"r{int(time.time())}"
    root.mkdir()
    (root / "models").symlink_to(ROOT / "models", target_is_directory=True)
    (root / "work").mkdir()
    config = Config.load(ROOT / "config.toml")
    config.root = root
    config.workspace = str(root / "work")
    service = AssistantService(config)
    start = time.monotonic()
    events = []
    audio = bytearray()
    raw = bytearray()
    raw_times = []
    clean = bytearray()
    processes = []
    tasks = []

    def note(kind, **data):
        events.append({"s": round(time.monotonic() - start, 3), "kind": kind, **data})
        (root / "room-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str))

    old_audio = service.voice.on_audio

    def on_audio(pcm):
        audio.extend(pcm)
        if np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype(np.int32)), initial=0) > 150:
            if not any(e["kind"] == "first_reply" for e in events):
                note("first_reply")
        old_audio(pcm)

    service.voice.on_audio = on_audio
    old_stop = service.player.stop

    async def stop(*args, **kwargs):
        note("player_stop", was_playing=bool(service.player.playing), mode=service.mode)
        await old_stop(*args, **kwargs)

    service.player.stop = stop
    frames = service.route.frames

    async def capture():
        async for pcm in frames():
            clean.extend(pcm)
            yield pcm

    service.route.frames = capture
    old_events = service.voice.on_event

    async def event(e):
        if e.get("kind") == "transcript":
            note("transcript", role=e["role"], text=e["text"])
        if e.get("kind") == "media":
            note("media", event=e["event"])
        await old_events(e)

    service.voice.on_event = event
    run = asyncio.create_task(service.run())

    async def wait_ready():
        for _ in range(100):
            if run.done():
                run.result()
            if service.last_frame:
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Microphone unavailable")

    try:
        await wait_ready()
        sources, _ = await service.route.devices()
        physical = next(
            x["name"]
            for x in sources
            if config.microphone_match in x["name"] and not x["name"].endswith(".monitor")
        )
        tasks.append(asyncio.create_task(record(physical, raw, raw_times)))
        p = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=120",
            "--volume=65536",
            "--client-name=personal-agent-test-user",
            f"--device={service.route.physical_sink}",
            env=audio_env(),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(p)
        stimuli = asyncio.Queue()

        async def stimulus_clock():
            clock = time.monotonic()
            index = 0
            while True:
                await asyncio.sleep(max(0, clock + index * 0.02 - time.monotonic()))
                index += 1
                queued = not stimuli.empty()
                block = stimuli.get_nowait() if queued else bytes(640)
                p.stdin.write(block)
                await p.stdin.drain()
                if queued:
                    stimuli.task_done()

        tasks.append(asyncio.create_task(stimulus_clock()))

        async def play_stimulus(pcm):
            for offset in range(0, len(pcm), 640):
                await stimuli.put(pcm[offset : offset + 640].ljust(640, b"\0"))
            await stimuli.join()

        with wave.open(str(ROOT / "state/acceptance/wake.wav")) as f:
            fixture = f.readframes(f.getnframes())
        # Warm the test speaker, so a cold Bluetooth transport cannot clip the
        # first syllable of a simulated person (real users don't have this issue).
        t = np.arange(3200) / 16000
        await play_stimulus((np.sin(2 * np.pi * 1100 * t) * 1000).astype("<i2").tobytes())
        await asyncio.sleep(1)
        note("stimulus_start")
        await play_stimulus(fixture)
        note("stimulus_end")
        await asyncio.sleep(9)
        if not service.active:
            note("wake_retry")
            await play_stimulus(fixture)
        await asyncio.sleep(8)
        note("question_status", status=service.status())
        if not service.active:
            note("diagnostic_manual_wake")
            await service.wake()
            await service.connect_task
        if service.mode == "active":
            note("long_reply_request")
            await service.backend.client.request(
                "thread/realtime/appendText",
                {
                    "threadId": service.backend.thread_id,
                    "role": "user",
                    "text": "请正常速度朗读下面这段话：这是一段连续播放测试。我会用正常的速度说几句话。请留意声音是否连贯，句子中间有没有不应该出现的停顿。现在我们在客厅测试麦克风和音箱。测试结束。",
                },
            )
            # Allow for variable native service startup, then record the whole
            # response. A silent WebRTC track does not count as successful audio.
            deadline = time.monotonic() + 35
            requested = time.monotonic()
            interrupted = False
            while time.monotonic() < deadline:
                if (
                    args.interrupt
                    and not interrupted
                    and service.player.playing
                    and time.monotonic() - requested > 3
                ):
                    interrupted = True
                    with wave.open(str(ROOT / "state/acceptance/stop.wav")) as f:
                        stop_pcm = f.readframes(f.getnframes())
                    note("spoken_stop_start")
                    await play_stimulus(stop_pcm)
                    note("spoken_stop_end")
                if interrupted and service.player.manual_muted:
                    note("stop_applied", status=service.status())
                    await asyncio.sleep(2)
                    break
                if any(
                    e["kind"] == "transcript" and e["role"] == "assistant" and "测试结束" in e["text"]
                    for e in events
                ):
                    await asyncio.sleep(2)
                    break
                await asyncio.sleep(0.1)
        if service.voice.peer:
            stats = await service.voice.peer.getStats()
            note(
                "rtp_stats",
                values=[vars(v) | {"timestamp": str(v.timestamp)} for v in stats.values() if "rtp" in v.type],
            )
        elif getattr(service.voice, "engine", None):
            note("rtp_stats", values=await service.voice.engine.get_stats())
        note("final_status", status=service.status())
    finally:
        (root / "room-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2, default=str))
        wav(root / "room.wav", raw, 16000)
        wav(root / "model.wav", audio, 24000)
        wav(root / "aec.wav", clean, 16000)
        for p in processes:
            if p.returncode is None:
                p.kill()
                await p.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.stop_event.set()
        await asyncio.wait_for(run, 20)
        print(
            json.dumps({"path": str(root), "events": events}, ensure_ascii=False, indent=2, default=str),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interrupt", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
