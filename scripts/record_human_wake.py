"""Explicit, guided human wake-word capture; restore the idle service on exit.

Prompted windows are provisional labels, requiring user verification. No human
identity or exact phonetic timestamps are inferred from these recordings.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import signal
from datetime import datetime, timezone

import numpy as np
import sherpa_onnx
import soxr

from assistant_runtime.cli import request, systemctl
from assistant_runtime.config import Config
from assistant_runtime.plugins.pipewire.audio import audio_env, command
from scripts.benchmark_wake import RATE, write_wav

GROUPS = [
    ("short", "嘟嘟", "嘟嘟。", 5, "第一组，听到每一声提示音后，说一次嘟嘟。用平时自然的声音，共五次。"),
    ("long", "你好嘟嘟", "你好嘟嘟。", 5, "第二组，每次提示音后，说一次你好嘟嘟，共五次。"),
    ("inline", "嘟嘟", "嘟嘟，二加三等于几？", 2, "第三组，每次提示音后，说嘟嘟，二加三等于几。共两次。"),
    (
        "inline-long",
        "你好嘟嘟",
        "你好嘟嘟，现在几点？",
        2,
        "第四组，每次提示音后，说你好嘟嘟，现在几点。共两次。",
    ),
    ("negative-read", None, "你好，读读这本书。", 2, "第五组，每次提示音后，说你好，读读这本书。共两次。"),
    (
        "negative-ordinary",
        None,
        "今天晚上我们继续测试。",
        2,
        "最后一组，每次提示音后，说今天晚上我们继续测试。共两次。",
    ),
]


def prepare_prompts(tts_dir, groups):
    settings = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(tts_dir / "model.int8.onnx"),
                voices=str(tts_dir / "voices.bin"),
                tokens=str(tts_dir / "tokens.txt"),
                data_dir=str(tts_dir / "espeak-ng-data"),
                lexicon=f"{tts_dir / 'lexicon-us-en.txt'},{tts_dir / 'lexicon-zh.txt'}",
                dict_dir=str(tts_dir / "dict"),
            ),
            num_threads=2,
        )
    )
    if not settings.validate():
        raise RuntimeError("Test-only prompt model unavailable")
    tts = sherpa_onnx.OfflineTts(settings)
    result = {}
    for text in [group[4] for group in groups] + ["录音结束，谢谢。"]:
        audio = tts.generate(text, sid=3)
        result[text] = soxr.resample(np.asarray(audio.samples), audio.sample_rate, RATE)
    return result


async def record(config, folder, prompts, seconds, lead_in, groups):
    before = await request(config, {"command": "status"})
    if before["mode"] != "standby" or before["task_running"]:
        raise RuntimeError("Assistant is busy; wait until idle")
    sources = json.loads(await command("pactl", "-f", "json", "list", "sources"))
    raw = [
        s["name"] for s in sources if config.source_match in s["name"] and not s["name"].endswith(".monitor")
    ]
    if len(raw) != 1:
        raise RuntimeError("Ambiguous microphone")
    sinks = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
    identity = config.speakers[before["room"]]
    sink = next(
        (
            s["name"]
            for s in sinks
            if s["name"] == identity or f"bluez_output.{identity.replace(':', '_')}." in s["name"]
        ),
        None,
    )
    if sink is None:
        raise RuntimeError("Selected room speaker unavailable for prompts")
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {
        "wake_words": config.wake_words,
        "human_samples": 0,
        "speaker": "user-provided human voice, pending verification",
        "label_status": "provisional prompted windows; verify actual utterances before accuracy claims",
        "source": raw[0],
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "records": [],
    }

    def write_manifest():
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    write_manifest()

    async def play(samples):
        player = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            f"--device={sink}",
            env=audio_env(),
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, errors = await asyncio.wait_for(
                player.communicate((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()),
                len(samples) / RATE + 8,
            )
            if player.returncode:
                raise RuntimeError(errors.decode()[:200])
        finally:
            if player.returncode is None:
                player.kill()
                await player.wait()

    systemctl("stop", config.unit_name)
    try:
        print(f"READY: microphone {raw[0]}; instructions begin in {lead_in}s", flush=True)
        await asyncio.sleep(lead_in)
        t = np.arange(int(RATE * 0.18)) / RATE
        tone = 0.12 * np.sin(2 * np.pi * 880 * t) * np.sin(np.pi * np.arange(len(t)) / len(t)) ** 2
        for style, phrase, text, repeats, instruction in groups:
            print(f"GROUP {style}: {text} × {repeats}", flush=True)
            await play(prompts[instruction])
            await asyncio.sleep(1.5)
            for index in range(repeats):
                # Open capture first, then use its monotonic clock to trim the cue.
                capture = await asyncio.create_subprocess_exec(
                    "pacat",
                    "--record",
                    "--raw",
                    "--format=s16le",
                    "--rate=16000",
                    "--channels=1",
                    "--latency-msec=20",
                    f"--device={raw[0]}",
                    env=audio_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                content = bytearray()

                async def consume():
                    while chunk := await capture.stdout.read(640):
                        content.extend(chunk)

                reader = asyncio.create_task(consume())
                try:
                    await asyncio.sleep(0.15)
                    await play(tone)
                    # Keep pre-roll so early speech is not thrown away. The tone
                    # is retained and disclosed; prompt speech is never included.
                    await asyncio.sleep(seconds)
                finally:
                    if capture.returncode is None:
                        capture.terminate()
                    await capture.wait()
                    await reader
                samples = np.frombuffer(content, dtype="<i2").astype(np.float32) / 32768
                if len(samples) < RATE * seconds * 0.8:
                    raise RuntimeError("Incomplete capture; exclude from testing")
                sample_id = f"human-{style}-{index + 1:02d}"
                path = folder / f"{sample_id}.wav"
                write_wav(path, samples)
                manifest["records"].append(
                    {
                        "id": sample_id,
                        "path": str(path.resolve()),
                        "group": "human_positive" if phrase else "human_negative",
                        "phrase": phrase,
                        "text": text,
                        "style": style,
                        "voice": "owner",
                        "natural_human_recording": True,
                        "verification": "pending",
                        "room": before["room"],
                        "expected": [
                            {
                                "start_s": 0.0,
                                "end_s": len(samples) / RATE,
                                "word": phrase,
                                "timing_source": "prompt_window_unverified",
                            }
                        ]
                        if phrase
                        else [],
                        "duration_s": len(samples) / RATE,
                        "rms": float(np.sqrt(np.mean(samples**2))),
                        "peak": float(np.max(np.abs(samples))),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "note": "Raw camera mic; short cue tone retained; no precise word-end timing or identity validation",
                    }
                )
                manifest["human_samples"] += 1
                write_manifest()
                print(
                    f"SAVED {sample_id}: rms={manifest['records'][-1]['rms']:.5f} peak={manifest['records'][-1]['peak']:.3f}",
                    flush=True,
                )
        await play(prompts["录音结束，谢谢。"])
    finally:
        systemctl("start", config.unit_name)
        print("Service restored", flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--lead-in", type=float, default=10.0)
    parser.add_argument("--groups", nargs="+", choices=[group[0] for group in GROUPS])
    args = parser.parse_args()
    if not 3 <= args.seconds <= 10 or not 0 <= args.lead_in <= 60:
        parser.error("Capture seconds must be 3–10; lead-in must be 0–60")
    groups = [group for group in GROUPS if not args.groups or group[0] in args.groups]
    prompts = prepare_prompts(Path("models/test-tts/kokoro-int8-multi-lang-v1_1"), groups)

    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        await record(Config.load(args.config), args.folder, prompts, args.seconds, args.lead_in, groups)

    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Recording interrupted; partial files kept with provisional labels")


if __name__ == "__main__":
    main()
