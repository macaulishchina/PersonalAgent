"""Seven pinned local KWS variants, identical corpus and decoder settings.

Each variant runs in its own worker process. Results are split by source and
stress condition; derived phase trials are never described as human accuracy.
"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

import numpy as np
import sherpa_onnx

from assistant_runtime.config import Config
from scripts.benchmark_wake import BLOCK, RATE, load_wav, match_events, distribution


def variants(models):
    result = {}
    for chunk in (16, 8):
        for quant in (True, False):
            base = models / "kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
            tag = ".int8" if quant else ""
            result[f"zh_en_c{chunk}_{'int8' if quant else 'fp32'}"] = {
                "tokens": str(base / "tokens.txt"),
                "encoder": str(base / f"encoder-epoch-13-avg-2-chunk-{chunk}-left-64{tag}.onnx"),
                "decoder": str(base / f"decoder-epoch-13-avg-2-chunk-{chunk}-left-64.onnx"),
                "joiner": str(base / f"joiner-epoch-13-avg-2-chunk-{chunk}-left-64{tag}.onnx"),
            }
    base = models / "kws/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    for precision in ("fp32", "mixed_int8", "full_int8"):
        tag = ".int8" if precision != "fp32" else ""
        decoder_tag = ".int8" if precision == "full_int8" else ""
        result[f"wenet_c16_{precision}"] = {
            "tokens": str(base / "tokens.txt"),
            "encoder": str(base / f"encoder-epoch-12-avg-2-chunk-16-left-64{tag}.onnx"),
            "decoder": str(base / f"decoder-epoch-12-avg-2-chunk-16-left-64{decoder_tag}.onnx"),
            "joiner": str(base / f"joiner-epoch-12-avg-2-chunk-16-left-64{tag}.onnx"),
        }
    return result


def keyword_lines(words):
    from pypinyin import pinyin, Style

    lines = []
    for word in words:
        tokens = []
        for char in word:
            initial = pinyin(char, style=Style.INITIALS, strict=False)[0][0]
            final = pinyin(char, style=Style.FINALS_TONE, strict=False)[0][0]
            if initial:
                tokens.append(initial)
            tokens.append(final)
        lines.append(" ".join(tokens) + " @" + word)
    return "\n".join(lines) + "\n"


def trial_variants(record):
    yield "original", 1.0, 0.0, None
    if record["expected"] and record["group"] in {
        "fresh_room",
        "historical_room",
        "historical_digital",
        "human_positive",
    }:
        for offset in range(20, 320, 20):
            yield f"phase_{offset:03d}ms", 1.0, offset / 1000, None
    if record["group"] == "digital_positive" and record["style"] == "isolated":
        # Keep clean variants balanced across all voices/speeds, not only easiest clips.
        for offset in (80, 160, 240):
            yield f"phase_{offset:03d}ms", 1.0, offset / 1000, None
        for scale in (0.25, 0.1):
            yield f"amplitude_{scale}", scale, 0.0, None
        for snr in (15, 5):
            yield f"white_noise_{snr}db", 1.0, 0.0, snr

    if record["group"] == "human_positive":
        for scale in (0.25, 0.1):
            yield f"amplitude_{scale}", scale, 0.0, None
        for snr in (15, 5):
            yield f"white_noise_{snr}db", 1.0, 0.0, snr


def run_audio(detector, samples):
    detector.reset_keyword()
    hits, words, costs = [], [], []
    for offset in range(0, len(samples), BLOCK):
        block = samples[offset : offset + BLOCK]
        if len(block) < BLOCK:
            block = np.pad(block, (0, BLOCK - len(block)))
        started = time.perf_counter_ns()
        word = detector.keyword(block)
        costs.append((time.perf_counter_ns() - started) / 1e6)
        if word:
            hits.append(min(offset + BLOCK, len(samples)) / RATE)
            words.append(word)
    return hits, words, costs


def summarize(trials):
    groups = {}
    for trial in trials:
        condition = (
            "original"
            if trial["variant"] == "original"
            else ("phase" if trial["variant"].startswith("phase_") else trial["variant"])
        )
        key = trial["group"] + "/" + condition
        item = groups.setdefault(
            key,
            {
                "clips": 0,
                "targets": 0,
                "hits": 0,
                "extra": 0,
                "negative_seconds": 0,
                "negative_false_hits": 0,
                "delays": [],
                "phrases": {},
            },
        )
        item["clips"] += 1
        item["targets"] += trial["targets"]
        item["hits"] += trial["hits"]
        item["extra"] += len(trial["extra_hits"])
        if not trial["targets"]:
            item["negative_seconds"] += trial["seconds"]
            item["negative_false_hits"] += len(trial["extra_hits"])
        item["delays"].extend(trial["estimated_word_end_delay_ms"])
        if trial.get("phrase"):
            phrase = item["phrases"].setdefault(trial["phrase"], {"targets": 0, "hits": 0})
            phrase["targets"] += trial["targets"]
            phrase["hits"] += trial["hits"]
    for item in groups.values():
        item["recall"] = item["hits"] / item["targets"] if item["targets"] else None
        item["negative_seconds"] = round(item["negative_seconds"], 3)
        item["estimated_word_end_delay_ms"] = distribution(item.pop("delays"))
    return groups


class SherpaReference:
    """Offline historical comparison only; never loaded by the assistant."""

    def reset_keyword(self):
        self.keyword_stream = self.spotter.create_stream()

    def keyword(self, samples):
        self.keyword_stream.accept_waveform(RATE, samples)
        while self.spotter.is_ready(self.keyword_stream):
            self.spotter.decode_stream(self.keyword_stream)
            word = self.spotter.get_result(self.keyword_stream)
            if word:
                self.spotter.reset_stream(self.keyword_stream)
                return word
        return None


def worker(config, folder, name):
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    spec = variants(config.models)[name]
    model_hashes = {key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in spec.items()}
    supported = {line.rsplit(" ", 1)[0] for line in Path(spec["tokens"]).read_text().splitlines()}
    lines = keyword_lines(manifest["wake_words"])
    for line in lines.splitlines():
        for token in line.split(" @", 1)[0].split():
            if token not in supported:
                raise ValueError(f"Model {name} does not support token {token}")
    trials, costs = [], []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=folder) as temporary:
        keywords = Path(temporary) / "keywords.txt"
        keywords.write_text(lines)
        detector = SherpaReference()
        detector.spotter = sherpa_onnx.KeywordSpotter(
            **spec,
            keywords_file=str(keywords),
            num_threads=2,
            keywords_score=1.0,
            keywords_threshold=0.25,
            max_active_paths=4,
            num_trailing_blanks=1,
        )
        detector.keyword_stream = detector.spotter.create_stream()
        initialization_ms = (time.perf_counter() - started) * 1000
        run_audio(detector, np.zeros(RATE, dtype=np.float32))  # excluded warm-up
        for index, record in enumerate(manifest["records"]):
            path = Path(record["path"])
            data = load_wav(path)
            for variant, scale, shift, snr in trial_variants(record):
                audio = data * scale
                if snr is not None:
                    # Same deterministic noise for every model.
                    rng = np.random.default_rng(
                        int(hashlib.sha256(record["id"].encode()).hexdigest()[:8], 16)
                    )
                    active = data[np.abs(data) > 0.003]
                    rms = np.sqrt(np.mean(active**2)) if len(active) else 0
                    audio = audio + rng.normal(0, rms / 10 ** (snr / 20), len(audio)).astype(np.float32)
                audio = np.concatenate([np.zeros(round(shift * RATE), dtype=np.float32), audio])
                expected = [
                    {**event, "start_s": event["start_s"] + shift, "end_s": event["end_s"] + shift}
                    for event in record["expected"]
                ]
                hits, words, elapsed = run_audio(detector, audio)
                matches, extra = match_events(hits, expected)
                costs.extend(elapsed)
                delays = [
                    (hit - event["end_s"]) * 1000
                    for hit, event in zip(matches, expected)
                    if hit is not None and event["timing_source"] == "energy_estimate"
                ]
                trials.append(
                    {
                        "id": record["id"],
                        "source_id": record.get("source_id", record["id"]),
                        "group": record["group"],
                        "phrase": record.get("phrase"),
                        "voice": record.get("voice"),
                        "speed": record.get("speed"),
                        "room": record.get("room"),
                        "playback_scale": record.get("playback_scale"),
                        "variant": variant,
                        "seconds": len(audio) / RATE,
                        "targets": len(expected),
                        "hits": sum(hit is not None for hit in matches),
                        "hits_s": hits,
                        "detected_words": words,
                        "extra_hits": extra,
                        "estimated_word_end_delay_ms": delays,
                    }
                )
            if index % 20 == 0:
                print(f"{name}: {index + 1}/{len(manifest['records'])}", flush=True)
        report = {
            "model": name,
            "files": spec,
            "file_sha256": model_hashes,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "sherpa_onnx": importlib.metadata.version("sherpa-onnx"),
            "decoder_parameters": {
                "score": 1.0,
                "threshold": 0.25,
                "max_active_paths": 4,
                "trailing_blanks": 1,
                "threads": 2,
            },
            "initialization_ms": initialization_ms,
            "call_compute_ms": distribution(costs),
            "compute_rtf": sum(costs) / sum(trial["seconds"] * 1000 for trial in trials),
            "worker_peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "groups": summarize(trials),
            "trials": trials,
        }
    (folder / f"result-{name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"{name} finished: {len(trials)} conditions", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.model:
        worker(config, args.folder, args.model)
        return
    if not args.all:
        parser.error("Choose --all or --model NAME")
    results = []
    for name in variants(config.models):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.compare_wake_suite",
                "--config",
                args.config,
                "--folder",
                str(args.folder),
                "--model",
                name,
            ],
            check=True,
        )
        results.append(json.loads((args.folder / f"result-{name}.json").read_text()))
    summary = [{key: value for key, value in result.items() if key != "trials"} for result in results]
    (args.folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"all {len(results)} model variants finished", flush=True)


if __name__ == "__main__":
    main()
