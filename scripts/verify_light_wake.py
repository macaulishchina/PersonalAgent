"""Verify FSMN bounded streaming windows against full-utterance ONNX outputs."""

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.benchmark_light_wake import Fsmn
from scripts.light_wake_features import features, load_wav


def verify(root, corpus, output):
    model = Fsmn(root, 20)
    records = [r for r in json.loads(corpus.read_text())["records"] if r["split"] == "validation"][:6]
    measured = []
    for record in records:
        samples = load_wav(record["path"])
        bank = features(samples)
        centers = np.arange(0, len(bank), 3)
        indexes = np.clip(centers[:, None] + np.arange(-2, 3)[None], 0, len(bank) - 1)
        x = ((bank[indexes].reshape(len(centers), 400) + model.cmvn[0]) * model.cmvn[1]).astype(np.float32)[
            None
        ]
        original = model.session
        reference = original.run(None, {"input": x})[0]

        class Observe:
            last = None

            def run(self, names, inputs):
                result = original.run(names, inputs)
                self.last = result[0].copy()
                return result

        observer = Observe()
        model.session = observer
        model.reset()
        max_error, comparisons = 0.0, 0
        for offset in range(0, len(samples), 320):
            previous = model.cursor
            model.feed(samples[offset : offset + 320])
            if model.cursor > previous:
                start = max(0, previous - 36)
                prediction = observer.last[0, previous - start : model.cursor - start]
                expected = reference[0, previous : model.cursor]
                max_error = max(max_error, float(np.max(np.abs(prediction - expected))))
                comparisons += len(prediction)
        model.session = original
        if max_error > 0.0002:
            raise ValueError(f"Streaming lookahead/context mismatch: {record['id']} {max_error}")
        measured.append({"id": record["id"], "frames": comparisons, "max_logit_error": max_error})
    result = {
        "records": measured,
        "max_error": max(r["max_logit_error"] for r in measured),
        "lookahead_lfr_frames": 8,
        "left_context_lfr_frames": 36,
        "frame_period_ms": 30,
        "frontend_lfr_right_frames": 2,
        "source": "same frozen validation audio; no final-frame flush",
    }
    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("models/light-kws-20261013/fsmn-custom"))
    parser.add_argument("--corpus", type=Path, default=Path("state/light-kws-20261013/corpus/manifest.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("state/light-kws-20261013/fsmn-stream-parity.json")
    )
    args = parser.parse_args()
    verify(args.model, args.corpus, args.output)
