"""Offline synthetic-only custom KWS corpus. Never records or plays audio.

Kokoro speakers are split before synthesis; all real recordings are excluded.
The four speakers used in previous reports are excluded from every new split.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import sherpa_onnx
import soxr

from scripts.benchmark_wake import RATE, write_wav
from scripts.wake_corpus import NEGATIVES

SPEAKERS = {
    "train": [8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96],
    "validation": [12, 28, 68, 100],
    "test": [4, 36, 60, 92],
}
POSITIVES = [
    "嘟嘟。",
    "嘟嘟！",
    "你好嘟嘟。",
    "你好，嘟嘟。",
    "嘟嘟，今天星期几？",
    "你好嘟嘟，帮我查看进度。",
    "嘟嘟，把音箱切到书房。",
    "你好嘟嘟，二加三等于几？",
]
FILLERS = NEGATIVES + [
    "你好，读读刚才的内容。",
    "把书读读，再告诉我结论。",
    "我的肚肚不舒服。",
    "渡渡鸟是什么动物？",
    "都督有一封新的信。",
    "请监督这项工作。",
    "下班了，明天早上再来处理。",
    "大家一起讨论今天的新闻。",
    "小云小云，你好。",
    "你好小问，打开电视。",
]


def prepare(folder, tts_dir, shard=0, shards=1, recipe=None):
    folder.mkdir(parents=True, exist_ok=True)
    spec = json.loads(recipe.read_text()) if recipe else {}
    speakers_by_split = spec.get("speaker_splits", SPEAKERS)
    namespace = spec.get("seed_namespace", "light-kws-1")
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(tts_dir / "model.int8.onnx"),
                voices=str(tts_dir / "voices.bin"),
                tokens=str(tts_dir / "tokens.txt"),
                data_dir=str(tts_dir / "espeak-ng-data"),
                lexicon=f"{tts_dir / 'lexicon-us-en.txt'},{tts_dir / 'lexicon-zh.txt'}",
                dict_dir=str(tts_dir / "dict"),
            ),
            num_threads=1,
        ),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("Missing local Kokoro assets")
    tts = sherpa_onnx.OfflineTts(config)
    records = []
    speaker_index = 0
    for split, speakers in speakers_by_split.items():
        for sid in speakers:
            speaker_index += 1
            if (speaker_index - 1) % shards != shard:
                continue
            positives = spec.get("phrases", {}).get(split, {}).get("positive", POSITIVES)
            negatives = spec.get("phrases", {}).get(split, {}).get("negative", FILLERS)
            for index, text in enumerate(positives + negatives):
                seed = int(hashlib.sha256(f"{namespace}:{sid}:{index}".encode()).hexdigest()[:8], 16)
                rng = np.random.default_rng(seed)
                speed = float(rng.choice([0.8, 0.9, 1.0, 1.1, 1.2]))
                path = folder / f"{split}-sid{sid:03d}-{index:02d}.wav"
                if not path.exists():
                    audio = tts.generate(text, sid=sid, speed=speed)
                    speech = soxr.resample(np.asarray(audio.samples), audio.sample_rate, RATE).astype(
                        np.float32
                    )
                    # Both labels have randomized padding. No cue tone carries the label.
                    samples = np.concatenate(
                        [np.zeros(int(RATE * rng.uniform(0.2, 0.9))), speech, np.zeros(int(RATE * 0.5))]
                    )
                    write_wav(path, samples)
                record = {
                    "id": path.stem,
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "split": split,
                    "speaker_id": sid,
                    "text": text,
                    "positive": index < len(positives),
                    "speed": speed,
                    "natural_human_recording": False,
                }
                records.append(record)
            print(f"synthesized {split} sid={sid}; {len(records)} total", flush=True)
            (folder / f"manifest.partial-{shard}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2)
            )
    manifest = {
        "generator": "Kokoro int8 multilingual v1.1 local; no acoustic replay",
        "generator_sha256": hashlib.sha256((tts_dir / "model.int8.onnx").read_bytes()).hexdigest(),
        "seed_namespace": namespace,
        "speaker_splits": speakers_by_split,
        "excluded_previous_speakers": [3, 18, 58, 78],
        "human_training_recordings": 0,
        "records": records,
    }
    target = "manifest.json" if shards == 1 else f"manifest.shard-{shard}.json"
    (folder / target).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"complete {len(records)} synthetic recordings", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("state/light-kws-20261013/corpus"))
    parser.add_argument("--tts", type=Path, default=Path("models/test-tts/kokoro-int8-multi-lang-v1_1"))
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    if args.workers == 1:
        prepare(args.output, args.tts, args.shard, args.shards, args.recipe)
    else:
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.prepare_light_wake",
                    "--output",
                    str(args.output),
                    "--tts",
                    str(args.tts),
                    *(["--recipe", str(args.recipe)] if args.recipe else []),
                    "--shard",
                    str(i),
                    "--shards",
                    str(args.workers),
                ]
            )
            for i in range(args.workers)
        ]
        statuses = [p.wait() for p in processes]
        if any(statuses):
            raise RuntimeError(f"Synthesis worker failed: {statuses}")
        manifests = [
            json.loads((args.output / f"manifest.shard-{i}.json").read_text()) for i in range(args.workers)
        ]
        manifest = manifests[0]
        manifest["records"] = sorted([r for m in manifests for r in m["records"]], key=lambda r: r["id"])
        (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
