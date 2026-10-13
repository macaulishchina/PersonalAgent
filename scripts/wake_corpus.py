"""Explicit test-only synthesis and room recording; never changes production KWS.

prepare: generate local multi-voice positives, hard negatives and long streams.
record: stop the idle assistant, replay selected fixtures, record mic/monitor,
        and restore the service in finally. Recording requires this explicit mode.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np
import sherpa_onnx
import soxr

from assistant_runtime.cli import request, systemctl
from assistant_runtime.config import Config
from assistant_runtime.plugins.pipewire.audio import audio_env, command
from scripts.benchmark_wake import RATE, load_wav, write_wav

VOICES = {3: "zf_001", 18: "zf_026", 58: "zm_009", 78: "zm_052"}
NEGATIVES = [
    "读读这本书。",
    "肚肚有点饿了。",
    "独独他没有参加。",
    "渡渡鸟已经灭绝了。",
    "都读完了吗？",
    "督促一下进度。",
    "嘟一声就好了。",
    "你好，读读这本书。",
    "你好，肚肚饿了。",
    "小度小度，你在吗？",
    "你好小爱。",
    "你好助手，现在几点？",
    "今天下午我们讨论一下文件修改。",
    "请把客厅的音箱切换到书房。",
    "二加三等于几？",
    "取消当前任务。",
    "结束对话。",
    "再看一遍这个问题，稍后继续处理。",
]
STORY = "今天我们整理了一下房间，打开电脑查看工作安排。上午先处理文件和邮件，下午阅读一本书，晚上准备晚饭。天气逐渐变凉，出门时记得带一件外套。这个项目还有几项工作需要继续讨论，比如录音设备的设置，测试报告的内容，以及怎样保存已经完成的任务。请先检查每一个步骤，再确认最终结果。"


def bounds(samples):
    """Energy estimate for isolated clean speech, not phonetic ground truth."""
    n = 160
    rms = np.sqrt(np.mean(np.pad(samples, (0, (-len(samples)) % n)).reshape(-1, n) ** 2, axis=1))
    active = np.flatnonzero(rms > max(0.001, float(rms.max()) * 0.03))
    if not len(active):
        raise ValueError("Generated speech is empty")
    return active[0] * 0.01, min(len(samples) / RATE, (active[-1] + 1) * 0.01)


def prepare(config, folder, tts_dir):
    folder.mkdir(parents=True, exist_ok=True)
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
        ),
        max_num_sentences=1,
    )
    if not settings.validate():
        raise ValueError("Incomplete local test TTS assets")
    tts = sherpa_onnx.OfflineTts(settings)
    records = []
    durations = {}

    def generate(text, sid, speed, phrase=None, style="negative"):
        key = hashlib.sha256(f"{text}|{sid}|{speed}".encode()).hexdigest()[:16]
        path = folder / f"kokoro-{key}.wav"
        if not path.exists():
            audio = tts.generate(text, sid=sid, speed=speed)
            data = soxr.resample(np.asarray(audio.samples), audio.sample_rate, RATE).astype(np.float32)
            write_wav(path, np.concatenate([np.zeros(6400), data, np.zeros(RATE)]))
        data = load_wav(path)
        start, end = bounds(data)
        expected = []
        if phrase:
            if style == "inline":
                # Different sentence prosody: approximate prefix window, no end-latency claim.
                end = min(end, start + durations[(sid, 1.0, phrase)] * 1.4 + 0.25)
            expected = [
                {
                    "start_s": float(start),
                    "end_s": float(end),
                    "word": phrase,
                    "timing_source": "energy_estimate" if style == "isolated" else "prefix_estimate",
                }
            ]
            if style == "isolated":
                durations[(sid, speed, phrase)] = end - start
        record = {
            "id": path.stem,
            "path": str(path.resolve()),
            "group": "digital_positive" if phrase else "digital_negative",
            "phrase": phrase,
            "text": text,
            "voice": VOICES[sid],
            "sid": sid,
            "speed": speed,
            "style": style,
            "expected": expected,
            "duration_s": len(data) / RATE,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "natural_human_recording": False,
        }
        records.append(record)
        print(f"generated {len(records)}: {VOICES[sid]} {speed} {text}", flush=True)
        return record

    for sid in VOICES:
        for speed in (0.8, 1.0, 1.2):
            for phrase in config.wake_words:
                generate(phrase + "。", sid, speed, phrase, "isolated")
        for phrase in config.wake_words:
            generate(phrase + "，二加三等于几？", sid, 1.0, phrase, "inline")
        generate("你好，嘟嘟。", sid, 1.0, "你好嘟嘟", "paused_greeting")
        for text in NEGATIVES:
            generate(text, sid, 1.0)
        generate(STORY, sid, 1.0, style="long_negative")

    # Existing room A/B preserve the difficult regression, not a new speaker count.
    for name in ("1791734490-wake-direct", "1791734683-wake-aec"):
        path = config.state_dir / "room-tests" / name / "raw.wav"
        records.append(
            {
                "id": name,
                "path": str(path.resolve()),
                "group": "historical_room",
                "phrase": "嘟嘟",
                "voice": "historical_edge",
                "style": "historical",
                "expected": [
                    {
                        "start_s": 0,
                        "end_s": len(load_wav(path)) / RATE,
                        "word": "嘟嘟",
                        "timing_source": "unannotated",
                    }
                ],
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "natural_human_recording": False,
            }
        )

    rng = np.random.default_rng(20261012)
    negatives = [r for r in records if not r["expected"]]
    audio, seconds = [], 0
    while seconds < 600:
        for record in rng.permutation(negatives):
            part = load_wav(record["path"])
            gap = np.zeros(int(RATE * rng.uniform(0.1, 0.7)), dtype=np.float32)
            audio.extend([part, gap])
            seconds += (len(part) + len(gap)) / RATE
            if seconds >= 600:
                break
    path = folder / "negative-continuous-10min.wav"
    write_wav(path, np.concatenate(audio))
    records.append(
        {
            "id": "negative-continuous-10min",
            "path": str(path.resolve()),
            "group": "continuous_negative",
            "expected": [],
            "derived_repeated_corpus": True,
        }
    )
    path = folder / "silence-60s.wav"
    write_wav(path, np.zeros(RATE * 60))
    records.append({"id": "silence-60s", "path": str(path.resolve()), "group": "silence", "expected": []})
    manifest = {
        "wake_words": config.wake_words,
        "generator": "Kokoro int8 v1.1-zh local",
        "tts_model_sha256": hashlib.sha256((tts_dir / "model.int8.onnx").read_bytes()).hexdigest(),
        "voices": VOICES,
        "human_samples": 0,
        "records": records,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"saved {len(records)} records", flush=True)


async def record_rooms(config, folder, rooms=None):
    manifest = json.loads((folder / "manifest.json").read_text())
    # Four voices and both phrases, normal speed, plus commands/confusers.
    chosen = [r for r in manifest["records"] if r.get("style") == "isolated" and r["speed"] == 1.0]
    chosen += [
        r
        for r in manifest["records"]
        if r.get("sid") == 3
        and r.get("text") in {"读读这本书。", "你好，读读这本书。", "二加三等于几？", "结束对话。"}
    ]
    before = await request(config, {"command": "status"})
    if before["mode"] != "standby" or before["task_running"]:
        raise RuntimeError("Assistant is busy; do not interrupt it for recordings")
    systemctl("stop", config.unit_name)
    added = []
    try:
        sources = json.loads(await command("pactl", "-f", "json", "list", "sources"))
        raw = [
            s["name"]
            for s in sources
            if config.microphone_match in s["name"] and not s["name"].endswith(".monitor")
        ]
        if len(raw) != 1:
            raise RuntimeError("Ambiguous microphone identity")
        sinks = json.loads(await command("pactl", "-f", "json", "list", "sinks"))
        for room in rooms or config.output_connections:
            target = config.output_connections[room]
            sink = next(
                (
                    s
                    for s in sinks
                    if s["name"] == target or f"bluez_output.{target.replace(':', '_')}." in s["name"]
                ),
                None,
            )
            if sink is None:
                print(f"room unavailable: {room}", flush=True)
                continue
            volume = {k: v.get("value") for k, v in sink.get("volume", {}).items()}
            levels = (0.75, 0.35) if room == config.default_output else (0.75,)
            for level in levels:
                for item in chosen:
                    samples = load_wav(item["path"]) * level
                    chunks = {"raw": bytearray(), "monitor": bytearray()}
                    captures, tasks = [], []

                    async def consume(process, key):
                        while True:
                            data = await process.stdout.read(640)
                            if not data:
                                break
                            chunks[key].extend(data)

                    player = None
                    try:
                        for key, source in [("raw", raw[0]), ("monitor", sink["name"] + ".monitor")]:
                            process = await asyncio.create_subprocess_exec(
                                "pacat",
                                "--record",
                                "--raw",
                                "--format=s16le",
                                "--rate=16000",
                                "--channels=1",
                                "--latency-msec=20",
                                f"--device={source}",
                                env=audio_env(),
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            captures.append(process)
                            tasks.append(asyncio.create_task(consume(process, key)))
                        await asyncio.sleep(0.7)
                        player = await asyncio.create_subprocess_exec(
                            "pacat",
                            "--playback",
                            "--raw",
                            "--format=s16le",
                            "--rate=16000",
                            "--channels=1",
                            "--latency-msec=80",
                            f"--device={sink['name']}",
                            env=audio_env(),
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        _, errors = await asyncio.wait_for(
                            player.communicate((samples * 32767).astype("<i2").tobytes()),
                            len(samples) / RATE + 8,
                        )
                        if player.returncode:
                            raise RuntimeError(errors.decode()[:200])
                        await asyncio.sleep(0.8)
                    finally:
                        if player and player.returncode is None:
                            player.kill()
                            await player.wait()
                        for process in captures:
                            if process.returncode is None:
                                process.terminate()
                                await process.wait()
                        await asyncio.gather(*tasks)
                    for key, pcm in chunks.items():
                        name = f"{room}-{level}-{key}-{item['id']}"
                        path = folder / (name + ".wav")
                        captured = (
                            np.frombuffer(bytes(pcm[: len(pcm) // 2 * 2]), dtype="<i2").astype(np.float32)
                            / 32768
                        )
                        if len(captured) < RATE // 2:
                            raise RuntimeError(f"Capture failed or too short: {name}")
                        write_wav(path, captured)
                        expected = (
                            [
                                {
                                    "start_s": 0,
                                    "end_s": len(captured) / RATE,
                                    "word": item["phrase"],
                                    "timing_source": "unannotated",
                                }
                            ]
                            if item["expected"]
                            else []
                        )
                        added.append(
                            {
                                **item,
                                "id": name,
                                "path": str(path.resolve()),
                                "group": "fresh_room" if key == "raw" else "sink_monitor",
                                "capture": key,
                                "room": room,
                                "playback_scale": level,
                                "sink_volume": volume,
                                "expected": expected,
                                "source_id": item["id"],
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "duration_s": len(captured) / RATE,
                                "rms": float(np.sqrt(np.mean(captured**2))),
                                "peak": float(np.max(np.abs(captured), initial=0)),
                                "timing_note": "Whole isolated utterance capture; no exact word-end annotation",
                            }
                        )
                    print(f"recorded {room} scale={level}: {item['text']}", flush=True)
                    manifest["records"] = [
                        r for r in manifest["records"] if r["id"] not in {a["id"] for a in added}
                    ] + added
                    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        systemctl("start", config.unit_name)
    print(f"recorded {len(added)} mic/monitor samples; service restored", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "record"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--tts-dir", type=Path)
    parser.add_argument("--rooms", nargs="+", help="Explicit configured rooms for recording")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.mode == "prepare":
        if not args.tts_dir:
            parser.error("prepare requires --tts-dir")
        prepare(config, args.folder, args.tts_dir)
    else:
        if args.rooms and set(args.rooms) - config.output_connections.keys():
            parser.error("--rooms must name configured outputs")
        asyncio.run(record_rooms(config, args.folder, args.rooms))


if __name__ == "__main__":
    main()
