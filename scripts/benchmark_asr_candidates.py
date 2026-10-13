"""CPU-only candidate ASR measurements on user-confirmed local recordings.

Exact configured text matching is the wake rule. Homophones are not silently
accepted. Qwen is utterance ASR; its offline timings are not native streaming.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import time
import unicodedata

import numpy as np

from scripts.benchmark_wake import RATE, load_wav
from scripts.benchmark_resources import Measure


def normalize(text):
    return "".join(c.lower() for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def distance(reference, prediction):
    previous = list(range(len(prediction) + 1))
    for i, left in enumerate(reference, 1):
        row = [i]
        for j, right in enumerate(prediction, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


class Paraformer:
    def __init__(self, folder, threads):
        from funasr import AutoModel

        self.model = AutoModel(
            model=str(folder.resolve()),
            device="cpu",
            ncpu=threads,
            disable_update=True,
            disable_pbar=True,
            disable_log=True,
        )
        self.parameter_count = sum(p.numel() for p in self.model.model.parameters())

    def transcribe(self, samples, words, chunk=10, realtime=False, finalize=True):
        cache, text, events = {}, "", []
        stride = chunk * 960
        beginning = time.perf_counter()
        for offset in range(0, len(samples), stride):
            end = min(offset + stride, len(samples))
            if realtime:
                time.sleep(max(0, beginning + end / RATE - time.perf_counter()))
            started = time.perf_counter()
            result = self.model.generate(
                input=samples[offset:end],
                cache=cache,
                is_final=finalize and end == len(samples),
                chunk_size=[0, chunk, 5 if chunk == 10 else 3],
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
                disable_pbar=True,
            )
            fragment = result[0].get("text", "") if result else ""
            text += fragment
            events.append(
                {
                    "audio_end_s": end / RATE,
                    "call_s": time.perf_counter() - started,
                    "wall_end_s": time.perf_counter() - beginning,
                    "fragment": fragment,
                    "wake": any(normalize(w) in normalize(text) for w in words),
                    "final": finalize and end == len(samples),
                }
            )
        return text, events


class Qwen:
    def __init__(self, folder, threads):
        import torch
        from transformers import AutoProcessor, AutoModelForMultimodalLM

        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(folder, local_files_only=True)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            folder,
            device_map="cpu",
            dtype=torch.float32,
            local_files_only=True,
            attn_implementation="sdpa",
        ).eval()
        self.parameter_count = sum(p.numel() for p in self.model.parameters())

    def transcribe(self, samples, words, prompt=False, **kwargs):
        context = f"助手的名字是{words[0]}。词汇：{'、'.join(words)}。" if prompt else None
        # Native processor supports ndarray inputs; sample rate fixed to 16kHz.
        inputs = self.processor.apply_transcription_request(
            audio=samples,
            language="Chinese",
            prompt=context,
            return_tensors="pt",
        ).to(self.model.device, self.model.dtype)
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=64, do_sample=False)
        output = output[:, inputs["input_ids"].shape[1] :]
        text = self.processor.decode(output[0], return_format="transcription_only")
        return text, []


class Kws:
    def __init__(self, folder, threads, words):
        import sherpa_onnx
        from scripts.compare_wake_suite import variants, keyword_lines

        self.files = variants(Path("models"))["zh_en_c16_int8"]
        folder.mkdir(parents=True, exist_ok=True)
        keywords = folder / "keywords.txt"
        keywords.write_text(keyword_lines(words))
        self.spotter = sherpa_onnx.KeywordSpotter(
            **self.files, keywords_file=str(keywords), num_threads=threads
        )
        self.parameter_count = None

    def transcribe(self, samples, words, realtime=False, finalize=True, **kwargs):
        stream = self.spotter.create_stream()
        text, events = "", []
        beginning = time.perf_counter()
        for offset in range(0, len(samples), 320):
            end = min(offset + 320, len(samples))
            if realtime:
                time.sleep(max(0, beginning + end / RATE - time.perf_counter()))
            started = time.perf_counter()
            block = samples[offset:end]
            stream.accept_waveform(RATE, np.pad(block, (0, 320 - len(block))))
            fragment = ""
            while self.spotter.is_ready(stream):
                self.spotter.decode_stream(stream)
                detected = self.spotter.get_result(stream)
                if detected:
                    fragment = detected
                    text += detected
                    self.spotter.reset_stream(stream)
            events.append(
                {
                    "audio_end_s": end / RATE,
                    "call_s": time.perf_counter() - started,
                    "wall_end_s": time.perf_counter() - beginning,
                    "fragment": fragment,
                    "wake": bool(text),
                    "final": False,
                }
            )
        return text, events


def score(record, text, words):
    reference, prediction = normalize(record["text"]), normalize(text)
    wake = any(normalize(w) in prediction for w in words)
    positive = bool(record["expected"])
    return {
        "wake": wake,
        "positive": positive,
        "hit": positive and wake,
        "false_wake": not positive and wake,
        "character_errors": distance(reference, prediction),
        "reference_chars": len(reference),
        "exact_transcript": reference == prediction,
    }


def main():
    import psutil

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["paraformer", "qwen", "kws"], required=True)
    parser.add_argument("--corpus", type=Path, default=Path("state/wake-human-comparison-20261013"))
    parser.add_argument("--output", type=Path, default=Path("state/asr-candidates-20261013"))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--idle-seconds", type=int, default=30)
    parser.add_argument(
        "--silent-seconds", type=int, help="Separate paced silence duration; defaults to idle-seconds"
    )
    parser.add_argument("--silent-chunk", type=int, choices=[5, 10], default=10)
    parser.add_argument(
        "--paced-only",
        action="store_true",
        help="Supplement real-time paced utterances without rescoring the corpus",
    )
    args = parser.parse_args()
    silent_seconds = args.silent_seconds if args.silent_seconds is not None else args.idle_seconds
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    torch = None
    if args.model != "kws":
        import torch

        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        torch.manual_seed(0)
    manifest_file = args.corpus / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    records = [r for r in manifest["records"] if r["group"] in ("human_positive", "human_negative")]
    audio = {}
    for r in records:
        path = Path(r["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != r["sha256"]:
            raise ValueError("Original recording changed")
        audio[r["id"]] = load_wav(path)
    args.output.mkdir(exist_ok=True, parents=True)
    output = args.output / f"{args.model}-t{args.threads}.json"
    report = {
        "model": args.model,
        "threads": args.threads,
        "device": "cpu",
        "dtype": "float32",
        "cpu_percent_definition": "100% = one logical core; machine percent divides by 6 cores",
        "corpus_sha256": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        "checkpoint": json.loads(
            (Path("models/asr-candidates") / args.model / "download-metadata.json").read_text()
        )
        if args.model != "kws"
        else {},
        "versions": {n: importlib.metadata.version(n) for n in ["torch", "transformers", "funasr", "psutil"]},
        "platform": platform.platform(),
        "logical_cores": psutil.cpu_count(),
        "process_start_wall_s": None,
        "trials": [],
        "error": None,
    }

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        print(f"Loading {args.model} CPU threads={args.threads}", flush=True)
        with Measure() as load:
            if args.model == "kws":
                model = Kws(args.output / "baseline-keywords", args.threads, manifest["wake_words"])
                report["checkpoint"] = {
                    key: {"path": value, "sha256": hashlib.sha256(Path(value).read_bytes()).hexdigest()}
                    for key, value in model.files.items()
                }
                report["dtype"] = "int8 encoder/joiner, fp32 decoder"
            else:
                model = (Paraformer if args.model == "paraformer" else Qwen)(
                    Path("models/asr-candidates") / args.model, args.threads
                )
        report["load"] = load.result
        report["parameter_count"] = model.parameter_count
        report["actual_torch_threads"] = torch.get_num_threads() if torch else None
        print(f"Loaded: {load.result}", flush=True)
        save()
        with Measure() as warmup:
            model.transcribe(audio[records[0]["id"]], manifest["wake_words"])
        report["warmup"] = warmup.result
        with Measure() as idle:
            time.sleep(args.idle_seconds)
        report["resident_idle"] = idle.result
        save()
        modes = (
            [("keyword", {})]
            if args.model == "kws"
            else (
                [("stream600", {"chunk": 10}), ("stream300", {"chunk": 5})]
                if args.model == "paraformer"
                else [("no_context", {}), ("name_context", {"prompt": True})]
            )
        )
        if args.paced_only:
            modes = []
        for mode, options in modes:
            for record in records:
                samples = audio[record["id"]]
                with Measure() as measurement:
                    text, events = model.transcribe(samples, manifest["wake_words"], **options)
                report["trials"].append(
                    {
                        "mode": mode,
                        "id": record["id"],
                        "style": record["style"],
                        "reference": record["text"],
                        "text": text,
                        "audio_seconds": len(samples) / RATE,
                        "metrics": measurement.result,
                        "rtf": measurement.result["wall_s"] / (len(samples) / RATE),
                        "events": events,
                        **score(record, text, manifest["wake_words"]),
                    }
                )
                print(f"{mode} {record['id']}: {text!r} {measurement.result['wall_s']:.3f}s", flush=True)
                save()
        if args.model in ("paraformer", "kws"):
            # Actual 30s paced silence with the same active ASR decoder.
            with Measure() as listening:
                silence_text, events = model.transcribe(
                    np.zeros(RATE * silent_seconds, dtype=np.float32),
                    manifest["wake_words"],
                    realtime=True,
                    finalize=False,
                    chunk=args.silent_chunk,
                )
            report["silent_stream"] = {
                **listening.result,
                "text": silence_text,
                "events": events,
                "chunk_size": args.silent_chunk if args.model == "paraformer" else None,
            }
            # A spoken clip paced at its real duration; earliest partial output is recorded.
            for chunk in [10, 5] if args.model == "paraformer" else [None]:
                for sample_id in ["human-short-03", "human-inline-long-02"]:
                    with Measure() as live:
                        text, events = model.transcribe(
                            audio[sample_id], manifest["wake_words"], realtime=True, chunk=chunk
                        )
                    report.setdefault("paced_speech", []).append(
                        {
                            "id": sample_id,
                            "chunk": chunk,
                            "text": text,
                            "events": events,
                            "metrics": live.result,
                        }
                    )
        else:
            # Same local Silero frontend used by production. This measures silence
            # gating, not a continuous Qwen ASR stream.
            import sherpa_onnx

            cfg = sherpa_onnx.VadModelConfig()
            cfg.silero_vad.model = "models/silero_vad.onnx"
            cfg.sample_rate = RATE
            cfg.num_threads = 1
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
            with Measure() as listening:
                started = time.perf_counter()
                for i in range(silent_seconds * RATE // 512):
                    vad.accept_waveform(np.zeros(512, dtype=np.float32))
                    time.sleep(max(0, started + (i + 1) * 512 / RATE - time.perf_counter()))
            report["silent_vad_gated"] = {
                **listening.result,
                "asr_calls": 0,
                "note": "Qwen resident, Silero continuously processing silence; not native ASR streaming",
            }
        save()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        save()
        raise
    report["worker_peak_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    save()
    print(f"Finished {output}", flush=True)


if __name__ == "__main__":
    main()
