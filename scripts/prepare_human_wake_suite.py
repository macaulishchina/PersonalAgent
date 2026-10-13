"""Freeze user-confirmed human recordings and explicitly derived test streams."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import random
import sys

import numpy as np

from scripts.benchmark_wake import RATE, load_wav, write_wav


def prepare(source, output):
    if output.exists():
        raise ValueError("Use a new output folder to preserve immutable previous runs")
    original_path = source / "manifest.json"
    original = json.loads(original_path.read_text())
    confirmation = json.loads((source / "user-confirmation.json").read_text())
    if confirmation["accepted_candidate_records"] != len(original["records"]):
        raise ValueError("User confirmation does not cover the complete corpus")
    records = []
    for entry in original["records"]:
        path = Path(entry["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Recording changed: {entry['id']}")
        data = load_wav(path)
        record = json.loads(json.dumps(entry))
        record["verification"] = "user confirmed completion and full recorded replay"
        record["source_id"] = record["id"]
        for event in record["expected"]:
            event["timing_source"] = "user_confirmed_window_unannotated"
        record["duration_s"] = len(data) / RATE
        records.append(record)
    output.mkdir(parents=True)
    rng = random.Random(4213)
    positives = [r for r in records if r["expected"]]
    negatives = [r for r in records if not r["expected"]]
    if not positives or not negatives:
        raise ValueError("Need confirmed positive and negative recordings")

    def derived(sample_id, group, chunks, expected):
        data = np.concatenate(chunks)
        path = output / f"{sample_id}.wav"
        write_wav(path, data)
        records.append(
            {
                "id": sample_id,
                "path": str(path.resolve()),
                "group": group,
                "source_id": sample_id,
                "expected": expected,
                "duration_s": len(data) / RATE,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "natural_human_recording": False,
                "note": "Derived from confirmed human clips; not an independent recording",
            }
        )

    # Ten minutes made from four negatives: report repeat exposure explicitly.
    negative_chunks, samples = [], 0
    while samples < 600 * RATE:
        for record in rng.sample(negatives, len(negatives)):
            chunk = load_wav(record["path"])
            gap = np.zeros(int(RATE * 0.8), dtype=np.float32)
            negative_chunks.extend([chunk, gap])
            samples += len(chunk) + len(gap)
    derived("human-negative-repeated-10min", "human_continuous_negative", negative_chunks, [])
    mixed_chunks, events, sample_count = [], [], 0
    for record in rng.sample(positives + negatives, len(positives + negatives)):
        data = load_wav(record["path"])
        events.extend(
            {
                **event,
                "start_s": event["start_s"] + sample_count / RATE,
                "end_s": event["end_s"] + sample_count / RATE,
            }
            for event in record["expected"]
        )
        gap = np.zeros(int(RATE * 0.8), dtype=np.float32)
        mixed_chunks.extend([data, gap])
        sample_count += len(data) + len(gap)
    derived("human-mixed-continuous", "human_continuous_mixed", mixed_chunks, events)
    derived("human-suite-silence-60s", "silence", [np.zeros(60 * RATE, dtype=np.float32)], [])
    manifest = {
        "wake_words": original["wake_words"],
        "human_samples": len(positives) + len(negatives),
        "independent_speakers": 1,
        "original_positive_recordings": len(positives),
        "original_negative_recordings": len(negatives),
        "derived_files": 3,
        "confirmation": confirmation,
        "source_manifest_sha256": hashlib.sha256(original_path.read_bytes()).hexdigest(),
        "conditions": "Original; 0–300 ms phase in 20 ms steps; 0.25/0.1 amplitude; white noise 15/5 dB",
        "limitations": [
            "One speaker, one microphone/location, no newly recorded distance/speed matrix",
            "Short cue tone remains in each raw recording; no source speech playback inside capture windows",
            "User confirmed requested utterances and complete playback; no phonetic word boundaries annotated",
            "Continuous negative stream repeats four sentences, not independent ten-minute speech",
            "Noise and amplitude variants are artificial; not additional human recordings",
        ],
        "records": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    (output / "hardware.json").write_text(
        json.dumps(
            {
                "cpu": cpu,
                "platform": platform.platform(),
                "python": sys.version,
            },
            indent=2,
        )
    )
    print(
        f"Frozen {len(positives)} positive + {len(negatives)} negative human recordings, 3 derived streams: {output}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)


if __name__ == "__main__":
    main()
