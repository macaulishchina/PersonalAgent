"""Assemble synthetic-only continuation data with reproducible acoustic variants.

Voice identities keep their original train/validation/test allocation. Human
recordings are explicitly rejected before any files are read or transformed.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt, resample_poly

from scripts.benchmark_wake import write_wav
from scripts.light_wake_features import RATE, load_wav


def validate_records(records):
    voices, hashes, ids = {}, {}, set()
    for row in records:
        if row.get("natural_human_recording") is not False:
            raise ValueError("Every source must explicitly identify synthetic provenance")
        split = row["split"]
        if split not in ("train", "validation", "test"):
            raise ValueError("Unknown split")
        if row["id"] in ids:
            raise ValueError("Duplicate record ID")
        ids.add(row["id"])
        for index, value in ((voices, row["speaker_id"]), (hashes, row["sha256"])):
            if value in index and index[value] != split:
                raise ValueError("Voice or waveform crosses data splits")
            index[value] = split


def augment(samples, seed):
    rng = np.random.default_rng(seed)
    # Small pitch/speed shifts and a mild room response; no live microphone input.
    factor = int(rng.choice([90, 95, 105, 110]))
    audio = resample_poly(samples, 100, factor).astype(np.float32)
    dry = audio.copy()
    for delay, gain in ((0.037, 0.18), (0.079, 0.10), (0.133, 0.06)):
        shift = int(RATE * delay)
        audio[shift:] += dry[:-shift] * gain
    audio = sosfilt(butter(2, [100, 6500], btype="bandpass", fs=RATE, output="sos"), audio)
    active = dry[np.abs(dry) > max(float(np.max(np.abs(dry))) * 0.03, 0.0001)]
    rms = float(np.sqrt(np.mean(active**2))) if len(active) else 0
    snr = float(rng.choice([20, 26, 32]))
    audio += rng.normal(0, rms / 10 ** (snr / 20), len(audio))
    gain = float(rng.choice([0.12, 0.25, 0.5, 0.8]))
    audio = np.pad(audio * gain, (int(RATE * rng.uniform(0, 0.3)), 0))
    return np.clip(audio, -0.99, 0.99).astype(np.float32), {
        "resample_factor": factor / 100,
        "snr_db": snr,
        "gain": gain,
        "band_hz": [100, 6500],
        "echo_delays_s": [0.037, 0.079, 0.133],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--original", type=Path, default=Path("state/light-kws-20261013/corpus/manifest.json")
    )
    parser.add_argument("--extra", type=Path, default=Path("state/fsmn-v2-20261013/extra/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("state/fsmn-v2-20261013/corpus"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records, sources = [], []
    for prefix, manifest in (("v1", args.original), ("v2", args.extra)):
        data = json.loads(manifest.read_text())
        sources.append({"path": str(manifest), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()})
        records.extend({**r, "id": prefix + "-" + r["id"]} for r in data["records"])
    validate_records(records)
    original = list(records)
    for row in original:
        source = Path(row["path"])
        if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Source waveform changed")
        seed = int(hashlib.sha256(("v2-acoustic:" + row["id"]).encode()).hexdigest()[:8], 16)
        audio, settings = augment(load_wav(source), seed)
        path = args.output / (row["id"] + "-acoustic.wav")
        write_wav(path, audio)
        records.append(
            {
                **row,
                "id": row["id"] + "-acoustic",
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_id": row["id"],
                "augmentation": settings,
            }
        )
    validate_records(records)
    result = {
        "generator": "local Kokoro + deterministic acoustic variants",
        "human_training_recordings": 0,
        "sources": sources,
        "records": records,
        "policy": "same voice and source waveform never cross splits; variants are not independent voices",
    }
    (args.output / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({s: sum(r["split"] == s for r in records) for s in ("train", "validation", "test")}))


if __name__ == "__main__":
    main()
