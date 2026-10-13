"""Independent quiet/speech CPU residency replication; no acoustic output."""

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.benchmark_light_wake import Fsmn, run_audio
from scripts.benchmark_resources import Measure
from scripts.light_wake_features import RATE, load_wav


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("models/light-kws-20261013/fsmn-custom"))
    parser.add_argument("--threshold", type=float, default=0.265)
    parser.add_argument("--chunk-ms", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    detector = Fsmn(args.model, args.chunk_ms, args.threshold)
    report = {
        "chunk_ms": args.chunk_ms,
        "seconds_per_phase": args.seconds,
        "threshold": args.threshold,
        "model": str(args.model),
    }
    with Measure() as metric:
        result = run_audio(detector, np.zeros(RATE * args.seconds, dtype=np.float32), True)
    report["silence"] = {**metric.result, "events": result["events"], "call_ms": result["call_ms"]}
    rows = json.loads(Path("state/wake-human-comparison-20261013/manifest.json").read_text())["records"][:18]
    source = np.concatenate([load_wav(r["path"]) for r in rows])
    audio = np.tile(source, int(np.ceil(args.seconds * RATE / len(source))))[: args.seconds * RATE]
    with Measure() as metric:
        result = run_audio(detector, audio, True)
    report["speech"] = {
        **metric.result,
        "events": result["events"],
        "call_ms": result["call_ms"],
        "note": "first minute of the fixed human corpus in order; pacing/resource test, not independent accuracy samples",
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps({key: report[key]["machine_capacity_percent"] for key in ["silence", "speech"]}),
        flush=True,
    )


if __name__ == "__main__":
    main()
