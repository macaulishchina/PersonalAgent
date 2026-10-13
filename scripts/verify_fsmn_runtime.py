"""Explicit model integration gate: human replay, research parity and bounded standby.

No microphone, playback or provider requests. Requires the private model/test assets.
"""

import argparse
import json
from pathlib import Path
import time

import numpy as np
import psutil

from assistant_runtime.plugins.keyword.fsmn import Fsmn, CHUNK
from scripts.benchmark_light_wake import Fsmn as Reference
from scripts.light_wake_features import load_wav

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standby-seconds", type=int, default=600)
    args = parser.parse_args()
    folder = ROOT / "state/acceptance/fsmn-v2"
    folder.mkdir(parents=True, exist_ok=True)
    model = Fsmn(ROOT / "models/fsmn-v2", ["嘟嘟", "你好嘟嘟"])
    reference = Reference(ROOT / "models/fsmn-v2-20261013/focused", 80, 0.485)
    rows = json.loads((ROOT / "state/wake-human-comparison-20261013/manifest.json").read_text())["records"]
    rows = [r for r in rows if r["group"] in {"human_positive", "human_negative"}]
    results = []
    for row in rows:
        samples = load_wav(row["path"])
        samples = np.pad(samples, (0, (-len(samples)) % CHUNK))
        model.reset()
        reference.reset()
        found, expected = [], []
        max_error = 0.0
        for i in range(0, len(samples), CHUNK):
            found.extend(model.feed(samples[i : i + CHUNK]))
            events, _ = reference.feed(samples[i : i + CHUNK])
            expected.extend(events)
            if model.probabilities.size:
                max_error = max(
                    max_error, float(np.max(np.abs(model.probabilities - reference.probabilities)))
                )
        assert [e[0] for e in found] == expected, row["id"]
        assert max_error < 1e-6, (row["id"], max_error)
        # Actual microphone frames are 20ms. Arbitrary input partitioning must
        # have precisely the same event clocks and scores as 80ms calibration.
        for stride in [320, 137, len(samples)]:
            model.reset()
            partitioned = []
            for i in range(0, len(samples), stride):
                partitioned.extend(model.feed(samples[i : i + stride]))
            assert partitioned == found, (row["id"], stride)
        results.append(
            {
                "id": row["id"],
                "positive": row["group"] == "human_positive",
                "events": found,
                "max_probability_error": max_error,
            }
        )
    print("human replay and partition/parity checks passed", flush=True)
    assert sum(bool(r["events"]) for r in results if r["positive"]) == 14
    assert not any(r["events"] for r in results if not r["positive"])
    model.reset()
    process = psutil.Process()
    begin = time.perf_counter()
    cpu = process.cpu_times()
    before = process.memory_info().rss
    maximum_frames = 0
    costs = []
    silence = np.zeros(CHUNK, dtype=np.float32)
    for i in range(args.standby_seconds * 1000 // 80):
        t = time.perf_counter()
        assert not model.feed(silence)
        costs.append(time.perf_counter() - t)
        maximum_frames = max(maximum_frames, model.diagnostics()["retained_feature_frames"])
        assert maximum_frames <= 140
        assert len(model.probabilities) <= 100 and len(model.pending) < CHUNK
    # A keyword after prolonged standby must survive feature eviction.
    sample = load_wav(rows[0]["path"])
    after_idle = model.feed(np.pad(sample, (0, (-len(sample)) % CHUNK)))
    assert after_idle
    after = process.memory_info().rss
    elapsed = time.perf_counter() - begin
    cpu_after = process.cpu_times()
    result = {
        "model": model.diagnostics(),
        "human": results,
        "standby_audio_seconds": args.standby_seconds,
        "standby_test_wall_seconds": elapsed,
        "standby_cpu_seconds": cpu_after.user + cpu_after.system - cpu.user - cpu.system,
        "maximum_retained_feature_frames": maximum_frames,
        "rss_before_mib": before / 1048576,
        "rss_after_mib": after / 1048576,
        "inference_ms_p50": float(np.percentile(costs, 50) * 1000),
        "inference_ms_p95": float(np.percentile(costs, 95) * 1000),
        "post_standby_keyword": after_idle,
        "note": "Accelerated replay, no real room playback. RSS includes two models for parity, not deployment RSS.",
    }
    (folder / "runtime-validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "human"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
