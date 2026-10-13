"""Select CTC streaming threshold on synthetic validation voices only."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from scripts.benchmark_light_wake import Fsmn, run_audio
from scripts.light_wake_features import load_wav


def choose(scores, objective="zero-false"):
    positives = np.array([r["peak_score"] for r in scores if r["positive"]])
    negatives = np.array([r["peak_score"] for r in scores if not r["positive"]])
    if not len(positives) or not len(negatives):
        raise ValueError("Calibration requires independent positive and negative validation clips")
    rows = []
    for value in np.arange(0.05, 1.0001, 0.005):
        threshold = round(float(value), 3)
        hits = int(np.sum(positives >= threshold))
        false = int(np.sum(negatives >= threshold))
        rows.append(
            {
                "threshold": threshold,
                "hits": hits,
                "targets": len(positives),
                "false_clips": false,
                "negative_clips": len(negatives),
            }
        )
    if objective == "balanced":
        selected = min(
            rows,
            key=lambda r: (
                (1 - r["hits"] / r["targets"]) + r["false_clips"] / r["negative_clips"],
                r["false_clips"],
                -r["hits"],
                -r["threshold"],
            ),
        )
    elif objective == "zero-false":
        selected = max(rows, key=lambda r: (-r["false_clips"], r["hits"], -r["threshold"]))
    else:
        raise ValueError("Unknown calibration objective")
    return selected, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objective", choices=["zero-false", "balanced"], default="zero-false")
    parser.add_argument("--chunk-ms", type=int, choices=[20, 80], default=20)
    parser.add_argument("--model", type=Path, default=Path("models/light-kws-20261013/fsmn-custom"))
    parser.add_argument("--corpus", type=Path, default=Path("state/light-kws-20261013/corpus/manifest.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("state/light-kws-20261013/fsmn-stream-calibration.json")
    )
    args = parser.parse_args()
    records = [r for r in json.loads(args.corpus.read_text())["records"] if r["split"] == "validation"]
    detector = Fsmn(args.model, args.chunk_ms)
    scores = []
    for r in records:
        assert not r["natural_human_recording"]
        if hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() != r["sha256"]:
            raise ValueError("Changed calibration source")
        result = run_audio(detector, load_wav(r["path"]))
        scores.append(
            {
                "id": r["id"],
                "speaker_id": r["speaker_id"],
                "positive": r["positive"],
                "text": r["text"],
                "peak_score": result["peak_score"],
            }
        )
    selection, curve = choose(scores, args.objective)
    result = {
        "selection": selection,
        "curve": curve,
        "scores": scores,
        "source_manifest_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "model_sha256": detector.meta["model_sha256"],
        "selection_policy": args.objective,
        "human_used_for_selection": False,
        "same_human_cohort_previously_inspected": True,
        "kind": f"{args.chunk_ms} ms causal audio feed, no endpoint flush",
        "chunk_ms": args.chunk_ms,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(selection), flush=True)


if __name__ == "__main__":
    main()
