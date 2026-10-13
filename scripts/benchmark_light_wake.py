"""Offline and paced native ONNX dedicated KWS evaluation, no playback/recording.

No Torch, FunASR model wrapper, or Sherpa import in the inference worker. Human
samples are verified by SHA256 and excluded from threshold/model selection.
"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import sys
import time

import numpy as np
import onnxruntime as ort

from scripts.benchmark_resources import Measure
from scripts.light_wake_features import RATE, load_wav, new_fbank


def distribution(values):
    if not values:
        return None
    return {
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "max": float(max(values)),
    }


class Mdtc:
    def __init__(self, folder, chunk_ms, threshold=None):
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.meta = json.loads((folder / "model-metadata.json").read_text())
        self.threshold = threshold if threshold is not None else self.meta["threshold"]
        self.chunk_ms = chunk_ms
        self.reset()

    def reset(self):
        self.fbank = new_fbank()
        self.cursor = 0
        self.cache = np.zeros(self.meta["cache_shape"], dtype=np.float32)
        self.last_hit = -10.0
        self.high = False
        self.total_samples = 0

    def feed(self, samples):
        self.total_samples += len(samples)
        self.fbank.accept_waveform(RATE, np.asarray(samples, dtype=np.float32) * 32768)
        ready = self.fbank.num_frames_ready
        if ready <= self.cursor:
            return [], 0.0
        x = np.stack([self.fbank.get_frame(i) for i in range(self.cursor, ready)])[None].astype(np.float32)
        output, self.cache = self.session.run(None, {"input": x, "cache": self.cache})
        hits = []
        for j, p in enumerate(output[0, :, 0]):
            clock = (self.cursor + j) * 0.01 + 0.025
            if p >= self.threshold and not self.high and clock - self.last_hit >= 1:
                hits.append(self.total_samples / RATE)
                self.last_hit = clock
                self.high = True
            elif p < self.threshold * 0.5:
                self.high = False
        self.cursor = ready
        return hits, float(output.max())


class Fsmn:
    def __init__(self, folder, chunk_ms, threshold=None):
        from scripts.light_wake_ctc import make_decoder

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.meta = json.loads((folder / "model-metadata.json").read_text())
        self.threshold = threshold if threshold is not None else self.meta["threshold"]
        self.cmvn = np.load(folder / "cmvn.npy")
        self.decoder, self.decoder_source_sha256 = make_decoder(Path("models/fsmn-kws-xiaoyun"))
        self.chunk_ms = chunk_ms
        self.reset()

    def reset(self):
        self.fbank = new_fbank()
        self.cursor = 0
        self.total_samples = 0
        self.probabilities = np.zeros((0, 2599), dtype=np.float32)
        self.last_hit = -10.0
        self.high = False

    def feed(self, samples):
        from scripts.light_wake_ctc import score

        self.total_samples += len(samples)
        self.fbank.accept_waveform(RATE, np.asarray(samples, dtype=np.float32) * 32768)
        frames = self.fbank.num_frames_ready
        lfr_ready = max(0, (frames - 3) // 3 + 1)
        # Four blocks each look ahead two LFR frames. Only stable outputs count.
        stable = lfr_ready - 8
        if stable <= self.cursor:
            return [], 0.0
        start = max(0, self.cursor - 36)
        centers = np.arange(start, lfr_ready) * 3
        indexes = np.maximum(0, centers[:, None] + np.arange(-2, 3)[None])
        features = np.stack([self.fbank.get_frame(int(i)) for i in indexes.reshape(-1)]).reshape(
            len(centers), 400
        )
        features = ((features + self.cmvn[0]) * self.cmvn[1]).astype(np.float32)[None]
        logits = self.session.run(None, {"input": features})[0][0, self.cursor - start : stable - start]
        logits -= np.max(logits, axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        self.probabilities = np.concatenate([self.probabilities, probabilities])[-100:]
        self.cursor = stable
        peak, _ = score(self.decoder, self.probabilities)
        clock = self.total_samples / RATE
        hits = []
        if peak >= self.threshold and not self.high and clock - self.last_hit >= 1:
            hits.append(clock)
            self.last_hit = clock
            self.high = True
        elif peak < self.threshold * 0.5:
            self.high = False
        return hits, peak


def match(hits, expected):
    remaining = list(hits)
    matched = []
    for window in expected:
        found = next((h for h in remaining if window["start_s"] <= h <= window["end_s"] + 1), None)
        matched.append(found)
        if found is not None:
            remaining.remove(found)
    return sum(h is not None for h in matched), remaining


def run_audio(model, samples, paced=False):
    model.reset()
    stride = int(model.chunk_ms * RATE / 1000)
    events, costs, peak = [], [], 0.0
    beginning = time.perf_counter()
    for offset in range(0, len(samples), stride):
        block = samples[offset : offset + stride]
        if paced:
            time.sleep(max(0, beginning + (offset + len(block)) / RATE - time.perf_counter()))
        before = time.perf_counter()
        hits, probability = model.feed(block)
        costs.append((time.perf_counter() - before) * 1000)
        peak = max(peak, probability)
        events.extend({"audio_end_s": h, "wall_s": time.perf_counter() - beginning} for h in hits)
    return {
        "events": events,
        "detected": bool(events),
        "peak_score": peak,
        "call_ms": distribution(costs),
        "wall_s": time.perf_counter() - beginning,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("models/light-kws-20261013/mdtc-small"))
    parser.add_argument(
        "--human", type=Path, default=Path("state/wake-human-comparison-20261013/manifest.json")
    )
    parser.add_argument(
        "--synthetic", type=Path, default=Path("state/light-kws-20261013/corpus/manifest.json")
    )
    parser.add_argument("--previous", type=Path, default=Path("state/wake-comparison/manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-ms", type=int, choices=[10, 20, 40, 80], default=20)
    parser.add_argument("--standby-seconds", type=int, default=180)
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--skip-stress", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with Measure() as load:
        metadata = json.loads((args.model / "model-metadata.json").read_text())
        kind = Fsmn if "FSMN" in metadata["kind"] else Mdtc
        model = kind(args.model, args.chunk_ms, args.threshold)
    report = {
        "model": str(args.model),
        "model_metadata": model.meta,
        "chunk_ms": args.chunk_ms,
        "threshold": model.threshold,
        "load": load.result,
        "trials": [],
        "torch_imported": "torch" in sys.modules,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("onnxruntime", "kaldi-native-fbank", "numpy", "psutil")
        },
    }

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    human = json.loads(args.human.read_text())
    report["human_manifest_sha256"] = hashlib.sha256(args.human.read_bytes()).hexdigest()
    records = [{**r, "cohort": r["group"], "positive": bool(r["expected"])} for r in human["records"]]
    synthetic = json.loads(args.synthetic.read_text())
    records += [{**r, "cohort": "new_synthetic_test"} for r in synthetic["records"] if r["split"] == "test"]
    previous = json.loads(args.previous.read_text())
    records += [
        {**r, "cohort": r["group"], "positive": bool(r["expected"])}
        for r in previous["records"]
        if r["group"]
        in ("digital_positive", "digital_negative", "historical_room", "continuous_negative", "silence")
    ]
    for record in records:
        path = Path(record["path"])
        if record.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("Audio changed")
        audio = load_wav(path)
        variants = [("original", audio)]
        if record["cohort"] == "human_positive" and not args.skip_stress:
            variants += [(f"phase-{ms}", np.pad(audio, (ms * 16, 0))) for ms in range(20, 320, 20)]
            variants += [(f"amplitude-{scale}", audio * scale) for scale in (0.25, 0.1)]
            rng = np.random.default_rng(int(hashlib.sha256(record["id"].encode()).hexdigest()[:8], 16))
            active = audio[np.abs(audio) > max(0.001, float(np.max(np.abs(audio))) * 0.03)]
            rms = float(np.sqrt(np.mean(active**2)))
            for snr in (15, 5):
                noise = rng.normal(0, rms / 10 ** (snr / 20), len(audio)).astype(np.float32)
                variants.append((f"white-noise-{snr}db", np.clip(audio + noise, -1, 1)))
        for variant, samples in variants:
            with Measure() as metrics:
                result = run_audio(model, samples)
            expected = record.get(
                "expected", [{"start_s": 0, "end_s": len(samples) / RATE}] if record["positive"] else []
            )
            if variant.startswith("phase-"):
                shift = int(variant.split("-")[1]) / 1000
                expected = [{"start_s": w["start_s"] + shift, "end_s": w["end_s"] + shift} for w in expected]
            hits, extra = match([e["audio_end_s"] for e in result["events"]], expected)
            result.update(targets=len(expected), hits=hits, extra_events=extra)
            report["trials"].append(
                {
                    "id": record["id"],
                    "cohort": record["cohort"],
                    "text": record.get("text", ""),
                    "positive": record["positive"],
                    "variant": variant,
                    "seconds": len(samples) / RATE,
                    "metrics": metrics.result,
                    **result,
                }
            )
        save()
    report["paced"] = []
    for record_id in ("human-short-03", "human-inline-long-02"):
        record = next(r for r in human["records"] if r["id"] == record_id)
        with Measure() as metrics:
            result = run_audio(model, load_wav(record["path"]), True)
        report["paced"].append({"id": record_id, "metrics": metrics.result, **result})
    save()
    with Measure() as idle:
        time.sleep(min(args.standby_seconds, 60))
    report["resident_idle"] = idle.result
    with Measure() as silent:
        result = run_audio(model, np.zeros(RATE * args.standby_seconds, dtype=np.float32), True)
    report["silent_stream"] = {
        **silent.result,
        "false_events": len(result["events"]),
        "call_ms": result["call_ms"],
    }
    report["worker_peak_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report["complete"] = True
    save()
    trials = [
        r
        for r in report["trials"]
        if r["cohort"] in ("human_positive", "human_negative") and r["variant"] == "original"
    ]
    print(
        json.dumps(
            {
                "human_hits": sum(t["detected"] for t in trials if t["positive"]),
                "human_false": sum(t["detected"] for t in trials if not t["positive"]),
                "silent": report["silent_stream"],
                "resident": report["resident_idle"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
