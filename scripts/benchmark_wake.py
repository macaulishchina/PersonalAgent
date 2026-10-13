"""Reproducible KWS baseline. Offline only: no speaker, mic, cloud brain or task execution.

--prepare generates explicit synthetic fixtures with free Edge TTS. It is test
data, never a production recognition/TTS fallback. Private WAVs live in state/.
The benchmark calls the current production Recognizer, in 20 ms capture blocks.
"""

import argparse
import asyncio
from dataclasses import replace
import hashlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import tempfile
import time
import wave

import numpy as np

from assistant_runtime.config import Config
from assistant_runtime.plugins.keyword.recognition import Recognizer

RATE = 16000
BLOCK = 320


def load_wav(path):
    with wave.open(str(path)) as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"Expected 16 kHz mono PCM16: {path}")
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32768


def write_wav(path, samples):
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        writer.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def match_events(hits, expected, tolerance=1.0):
    """One hit per utterance, with a bounded tail; extra hits remain visible."""
    remaining = list(enumerate(hits))
    matches = []
    for target in expected:
        chosen = next(
            ((i, hit) for i, hit in remaining if target["start_s"] <= hit <= target["end_s"] + tolerance),
            None,
        )
        if chosen is None:
            matches.append(None)
        else:
            remaining.remove(chosen)
            matches.append(chosen[1])
    return matches, [hit for _, hit in remaining]


def distribution(values):
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "p50": round(float(np.percentile(values, 50)), 3),
        "p95": round(float(np.percentile(values, 95)), 3),
        "max": round(float(max(values)), 3),
        "min": round(float(min(values)), 3),
    }


def prepare_existing(config, folder):
    """Known old fixtures; labels and lack of independent voices stay explicit."""
    import soxr

    if "嘟嘟" not in config.wake_words:
        raise ValueError("Historical fixtures are labelled only for 嘟嘟; provide a new manifest")
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    source = config.state_dir / "acceptance/wake.wav"
    samples = load_wav(source)
    # The established fixture says 嘟嘟 then a question. Its first 0.8 s contains
    # the wake phrase; question speech starts about 0.9 s. This is a prefix window,
    # not an exact phonetic word-end annotation.
    for name, audio in [
        ("wake_then_question", samples),
        ("wake_only", np.concatenate([samples[:12800], np.zeros(RATE)])),
    ]:
        dest = folder / (name + ".wav")
        write_wav(dest, audio)
        records.append(
            {
                "id": name,
                "path": dest.name,
                "group": "synthetic",
                "expected": [{"start_s": 0, "end_s": 0.8, "timing_source": "bounded_prefix"}],
            }
        )
    for name in ["stop", "cancel", "living", "study", "file"]:
        path = config.state_dir / "acceptance" / (name + ".wav")
        if path.exists():
            records.append(
                {"id": name, "path": str(path.resolve()), "group": "command_negative", "expected": []}
            )
    # A single 嘟 cropped before the second syllable is a hard acoustic negative.
    single = folder / "single_du.wav"
    write_wav(single, np.concatenate([samples[:5280], np.zeros(RATE)]))
    records.append(
        {"id": "single_du", "path": single.name, "group": "single_syllable_negative", "expected": []}
    )
    for path in sorted((config.state_dir / "room-tests").glob("*-wake-*/raw.wav")):
        records.append(
            {
                "id": path.parent.name,
                "path": str(path.resolve()),
                "group": "room_replay",
                "expected": [
                    {"start_s": 0, "end_s": len(load_wav(path)) / RATE, "timing_source": "unannotated"}
                ],
            }
        )
    for path in sorted((config.state_dir / "room-tests").glob("*-speech-*/raw.wav")):
        records.append(
            {
                "id": path.parent.name,
                "path": str(path.resolve()),
                "group": "room_speech_negative",
                "expected": [],
            }
        )
    for path in sorted(
        (config.models / "kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/test_wavs").glob("*.wav")
    ):
        # Unverified transcripts: exploratory exposure, excluded from accuracy.
        with wave.open(str(path)) as reader:
            audio = (
                np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32768
            )
            audio = soxr.resample(audio, reader.getframerate(), RATE)
        dest = folder / ("upstream_" + path.name)
        write_wav(dest, np.concatenate([audio, np.zeros(RATE)]))
        records.append({"id": dest.stem, "path": dest.name, "group": "unlabelled_upstream", "expected": []})
    silence = folder / "silence.wav"
    write_wav(silence, np.zeros(RATE * 60, dtype=np.float32))
    records.append({"id": "silence", "path": silence.name, "group": "silence", "expected": []})
    # Production does not reset KWS for every sentence.
    order = [
        r for r in records if r["group"] in {"command_negative", "single_syllable_negative", "synthetic"}
    ]
    playlist, events, position = [], [], 0
    for record in order:
        path = Path(record["path"])
        audio = load_wav(path if path.is_absolute() else folder / path)
        events += [
            {**e, "start_s": e["start_s"] + position, "end_s": e["end_s"] + position}
            for e in record["expected"]
        ]
        playlist.extend([audio, np.zeros(RATE)])
        position += len(audio) / RATE + 1
    continuous = folder / "continuous.wav"
    write_wav(continuous, np.concatenate(playlist))
    records.append(
        {"id": "continuous", "path": continuous.name, "group": "continuous_mixed", "expected": events}
    )
    negative_playlist = []
    for record in order:
        if record["expected"]:
            continue
        path = Path(record["path"])
        negative_playlist.extend([load_wav(path if path.is_absolute() else folder / path), np.zeros(RATE)])
    negative_stream = folder / "continuous_negative.wav"
    write_wav(negative_stream, np.concatenate(negative_playlist))
    records.append(
        {
            "id": "continuous_negative",
            "path": negative_stream.name,
            "group": "continuous_negative",
            "expected": [],
        }
    )
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "wake_word": "嘟嘟",
                "label_note": "Existing synthetic fixture prefix annotated; room replays are not human trials; upstream samples unlabelled.",
                "samples": records,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def prepare(config, folder):
    import edge_tts
    import miniaudio
    import soxr

    folder.mkdir(parents=True, exist_ok=True)
    voices = ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-YunjianNeural"]
    negatives = [
        "你好，今天的天气怎么样？",
        "帮我查看当前任务的进度。",
        "我先去忙了，回头再聊。",
        "读读这本书。",
        "肚肚有点饿了。",
        "独独他没有参加。",
        "百度一下这个问题。",
        "嘟一声就好了。",
        "小度小度，你在吗？",
        "你好助手，现在几点了？",
        "请把书房的音箱声音调大。",
        "这个问题我们下次继续讨论。",
    ]
    jobs = []
    for voice in voices:
        for rate in ["-20%", "+0%", "+20%"]:
            for suffix in ["", "，二加三等于几？"]:
                jobs.append((voice, rate, config.wake_words[0] + suffix, True))
        for text in negatives:
            if config.wake_words[0] not in text:
                jobs.append((voice, "+0%", text, False))
    semaphore = asyncio.Semaphore(3)

    async def synthesize(index, job):
        voice, rate, text, positive = job
        dest = folder / f"tts-{index:03d}.wav"
        metadata = dest.with_suffix(".json")
        if dest.exists() and metadata.exists():
            cached = json.loads(metadata.read_text())
            if (cached["voice"], cached["rate"], cached["text"]) == (voice, rate, text):
                return cached
        async with semaphore:
            mp3 = bytearray()
            boundaries = []
            async for packet in edge_tts.Communicate(
                text, voice, rate=rate, boundary="WordBoundary"
            ).stream():
                if packet["type"] == "audio":
                    mp3.extend(packet["data"])
                elif packet["type"] == "WordBoundary":
                    boundaries.append(packet)
            decoded = miniaudio.decode(bytes(mp3), nchannels=1, sample_rate=24000)
            samples = np.asarray(decoded.samples, dtype=np.float32) / 32768
            samples = soxr.resample(samples, 24000, RATE).astype(np.float32)
            lead = 0.5
            samples = np.concatenate([np.zeros(int(lead * RATE), np.float32), samples, np.zeros(RATE)])
            # Provider word timings are estimates, not human-marked phonetic ends.
            collected = ""
            wake_start = wake_end = None
            if positive:
                for boundary in boundaries:
                    if wake_start is None:
                        wake_start = boundary["offset"] / 1e7 + lead
                    collected += boundary["text"].strip("，。？！ ")
                    if collected == config.wake_words[0]:
                        wake_end = (boundary["offset"] + boundary["duration"]) / 1e7 + lead
                        break
                    if not config.wake_words[0].startswith(collected):
                        break
            event = {
                "start_s": wake_start if wake_start is not None else lead,
                "end_s": wake_end if wake_end is not None else len(samples) / RATE - 1,
                "timing_source": "tts_word_boundary" if wake_end is not None else "unannotated",
            }
            record = {
                "id": dest.stem,
                "path": dest.name,
                "group": "synthetic",
                "voice": voice,
                "rate": rate,
                "text": text,
                "expected": [event] if positive else [],
            }
            write_wav(dest, samples)
            metadata.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            print(f"prepared {index + 1}/{len(jobs)}", flush=True)
            return record

    records = await asyncio.gather(*(synthesize(i, job) for i, job in enumerate(jobs)))
    # The old room fixtures are loudspeaker-replayed synthetic speech, not human speakers.
    for report_path in sorted((config.state_dir / "room-tests").glob("*-wake-*/report.json")):
        path = report_path.parent / "raw.wav"
        if path.exists():
            records.append(
                {
                    "id": report_path.parent.name,
                    "path": str(path.resolve()),
                    "group": "room_replay",
                    "expected": [
                        {"start_s": 0.0, "end_s": len(load_wav(path)) / RATE, "timing_source": "unannotated"}
                    ],
                }
            )
    silence = folder / "silence.wav"
    write_wav(silence, np.zeros(RATE * 60, dtype=np.float32))
    records.append({"id": "silence", "path": silence.name, "group": "silence", "expected": []})
    manifest = {"wake_word": config.wake_words[0], "samples": records}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def replay(recognizer, samples):
    recognizer.reset_keyword()
    hits, costs = [], []
    for offset in range(0, len(samples), BLOCK):
        frame = samples[offset : offset + BLOCK]
        if len(frame) < BLOCK:
            frame = np.pad(frame, (0, BLOCK - len(frame)))
        start = time.perf_counter_ns()
        word = recognizer.keyword(frame)
        costs.append((time.perf_counter_ns() - start) / 1e6)
        if word:
            hits.append(min(offset + BLOCK, len(samples)) / RATE)
    return hits, costs


async def live_probe(config, output, seconds):
    """Observe raw microphone/KWS timing only; never persist PCM or wake the app."""
    from assistant_runtime.plugins.pipewire.audio import audio_env

    process = await asyncio.create_subprocess_exec(
        "pactl", "-f", "json", "list", "sources", env=audio_env(), stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    if process.returncode:
        raise RuntimeError("Cannot enumerate microphone sources")
    sources = [
        s["name"]
        for s in json.loads(stdout)
        if config.microphone_match in s["name"] and not s["name"].endswith(".monitor")
    ]
    if not config.microphone_match or len(sources) != 1:
        raise RuntimeError("Configure one unambiguous raw microphone identity")
    with tempfile.TemporaryDirectory(prefix="wake-live-", dir=config.state_dir) as temporary:
        isolated = replace(config, root=Path(temporary), model_dir=str(config.models))
        isolated.state_dir.mkdir()
        recognizer = Recognizer(isolated)
        capture = await asyncio.create_subprocess_exec(
            "pacat",
            "--record",
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=20",
            f"--device={sources[0]}",
            env=audio_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        start = last_frame = time.monotonic()
        costs, arrivals, hits, count = [], [], [], 0
        try:
            while time.monotonic() - start < seconds:
                pcm = await asyncio.wait_for(capture.stdout.readexactly(BLOCK * 2), 3)
                received = time.monotonic()
                arrivals.append((received - last_frame) * 1000)
                last_frame = received
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
                before = time.perf_counter_ns()
                word = recognizer.keyword(samples)
                costs.append((time.perf_counter_ns() - before) / 1e6)
                count += 1
                if word:
                    hits.append({"audio_s": count * BLOCK / RATE, "wall_s": time.monotonic() - start})
        finally:
            if capture.returncode is None:
                capture.terminate()
                await capture.wait()
        report = {
            "raw_recording_saved": False,
            "seconds": time.monotonic() - start,
            "audio_seconds": count * BLOCK / RATE,
            "frames": count,
            "hits": hits,
            "call_compute_ms": distribution(costs),
            "frame_arrival_ms": distribution(arrivals),
            "limit": "Unlabelled live observation, not a human accuracy trial or false-alarm estimate.",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))


def evaluate(config, manifest_path, output, stress):
    manifest = json.loads(manifest_path.read_text())
    if manifest["wake_word"] not in config.wake_words:
        raise ValueError("Corpus wake word differs from current instance configuration")
    # Avoid overwriting the live service's generated keyword file.
    with tempfile.TemporaryDirectory(prefix="wake-benchmark-", dir=config.state_dir) as temporary:
        isolated = replace(config, root=Path(temporary), model_dir=str(config.models))
        isolated.state_dir.mkdir()
        start = time.perf_counter()
        recognizer = Recognizer(isolated)
        initialization_ms = (time.perf_counter() - start) * 1000
        trials, all_costs = [], []
        rng = np.random.default_rng(20261012)
        for record in manifest["samples"]:
            path = Path(record["path"])
            path = path if path.is_absolute() else manifest_path.parent / path
            samples = load_wav(path)
            variants = [("original", 1.0, 0.0)]
            if stress and record["expected"] and record["group"] in {"synthetic", "room_replay"}:
                variants += [
                    ("half_amplitude", 0.5, 0.0),
                    ("quarter_amplitude", 0.25, 0.0),
                    ("tenth_amplitude", 0.1, 0.0),
                ]
                variants += [(f"phase_{offset:03d}ms", 1.0, offset / 1000) for offset in range(20, 320, 20)]
                # Signal-relative white noise is a controlled stressor, not TV/person speech.
                variants += [("white_noise_15db", 1.0, 0.0)]
            for variant, scale, phase in variants:
                audio = samples * scale
                if variant == "white_noise_15db":
                    nonquiet = samples[np.abs(samples) > 0.005]
                    rms = np.sqrt(np.mean(nonquiet**2)) if len(nonquiet) else 0.0
                    audio = audio + rng.normal(0, rms / 10**0.75, len(audio)).astype(np.float32)
                audio = np.concatenate([np.zeros(round(phase * RATE), np.float32), audio])
                expected = [
                    {**event, "start_s": event["start_s"] + phase, "end_s": event["end_s"] + phase}
                    for event in record["expected"]
                ]
                hits, costs = replay(recognizer, audio)
                matches, extra = match_events(hits, expected)
                all_costs.extend(costs)
                delays = [
                    (hit - target["end_s"]) * 1000
                    for hit, target in zip(matches, expected)
                    if hit is not None and target["timing_source"] in {"tts_word_boundary", "manual"}
                ]
                trials.append(
                    {
                        "id": record["id"],
                        "group": record["group"],
                        "variant": variant,
                        "duration_s": len(audio) / RATE,
                        "expected_count": len(expected),
                        "detected_count": sum(hit is not None for hit in matches),
                        "misses": sum(hit is None for hit in matches),
                        "extra_hits": extra,
                        "hits_s": hits,
                        "word_end_delay_ms": delays,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        groups = {}
        for trial in trials:
            key = trial["group"] + "/" + trial["variant"]
            group = groups.setdefault(
                key,
                {
                    "clips": 0,
                    "expected": 0,
                    "misses": 0,
                    "extra_hits": 0,
                    "negative_seconds": 0,
                    "delays": [],
                },
            )
            group["clips"] += 1
            group["expected"] += trial["expected_count"]
            group["misses"] += trial["misses"]
            group["extra_hits"] += len(trial["extra_hits"])
            if not trial["expected_count"]:
                group["negative_seconds"] += trial["duration_s"]
            group["delays"].extend(trial["word_end_delay_ms"])
        for group in groups.values():
            group["miss_rate"] = group["misses"] / group["expected"] if group["expected"] else None
            group["word_end_delay_ms"] = distribution(group.pop("delays"))
            group["negative_seconds"] = round(group["negative_seconds"], 3)
        report = {
            "wake_words": config.wake_words,
            "corpus_target": manifest["wake_word"],
            "sherpa_onnx_version": importlib.metadata.version("sherpa-onnx"),
            "recognizer_source_sha256": hashlib.sha256(
                Path(inspect.getfile(Recognizer)).read_bytes()
            ).hexdigest(),
            "keyword_tokens": list(recognizer.detector.decoder.keywords_token),
            "model": "fsmn-v2",
            "production_parameters": recognizer.diagnostics(),
            "capture_block_ms": 20,
            "initialization_ms": round(initialization_ms, 2),
            "call_compute_ms": distribution(all_costs),
            "compute_rtf": sum(all_costs) / sum(t["duration_s"] * 1000 for t in trials),
            "groups": groups,
            "trials": trials,
            "limits": [
                "Mixed continuous replay tests KWS only, not the assistant active/standby gate; negative-only replay matches uninterrupted standby.",
                "Upstream utterances without checked transcripts are exploratory and excluded from certified negative statistics.",
                "Synthetic and speaker-replayed samples are not independent human trials.",
                "Stress variants share base utterances; never combine them as population accuracy.",
                "TTS boundary delay is estimated; negative delay can mean early detection.",
                "Offline audio-clock detection is not wall-clock live microphone or audible acknowledgement latency.",
                "Short negative exposure cannot establish reliable false alarms per hour.",
            ],
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "trials"}, ensure_ascii=False, indent=2
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument(
        "--existing-fixtures", action="store_true", help="Use historical fixtures without network requests"
    )
    parser.add_argument("--stress", action="store_true")
    parser.add_argument(
        "--live-seconds", type=float, help="Observe raw mic timing; no PCM saved, no app activation"
    )
    args = parser.parse_args()
    config = Config.load(args.config)
    folder = args.folder or config.state_dir / "wake-benchmark"
    if args.live_seconds is not None:
        if not 0 < args.live_seconds <= 300:
            parser.error("--live-seconds must be in (0, 300]")
        asyncio.run(live_probe(config, args.output or folder / "live.json", args.live_seconds))
        return
    if args.prepare:
        asyncio.run(prepare(config, folder))
    elif args.existing_fixtures:
        prepare_existing(config, folder)
    manifest = args.manifest or folder / "manifest.json"
    evaluate(config, manifest, args.output or folder / "baseline.json", args.stress)


if __name__ == "__main__":
    main()
