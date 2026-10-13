"""Build an offline visual report from the completed wake-word experiments."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote


LABELS = {
    "zh_en_c16_int8": ("当前模型", "中英 3M · chunk 16 · INT8"),
    "zh_en_c16_fp32": ("当前模型 · 浮点", "中英 3M · chunk 16 · FP32"),
    "zh_en_c8_int8": ("小 chunk · INT8", "中英 3M · chunk 8 · INT8"),
    "zh_en_c8_fp32": ("小 chunk · 浮点", "中英 3M · chunk 8 · FP32"),
    "wenet_c16_fp32": ("中文 Wenet · 浮点", "WenetSpeech 3.3M · chunk 16 · FP32"),
    "wenet_c16_mixed_int8": ("中文 Wenet · 混合", "WenetSpeech 3.3M · chunk 16 · 混合 INT8"),
    "wenet_c16_full_int8": ("中文 Wenet · 全量 INT8", "WenetSpeech 3.3M · chunk 16 · 全 INT8"),
}


def aggregate(trials):
    trials = list(trials)
    return {"hits": sum(t["hits"] for t in trials), "targets": sum(t["targets"] for t in trials)}


def build_data(folder, output):
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    summaries = json.loads((folder / "summary.json").read_text())
    by_id = {record["id"]: record for record in manifest["records"]}
    models = []
    main_trials = cross_trials = 0
    for summary in summaries:
        result = json.loads((folder / f"result-{summary['model']}.json").read_text())
        if result["manifest_sha256"] != expected_hash:
            raise ValueError("Result and corpus hashes differ; regenerate the measurements first")
        trials = result["trials"]
        main_trials += len(trials)
        original = [t for t in trials if t["variant"] == "original"]
        cross = json.loads((folder / "cross-generator" / f"result-{result['model']}.json").read_text())
        cross_hash = hashlib.sha256((folder / "cross-generator/manifest.json").read_bytes()).hexdigest()
        if cross["manifest_sha256"] != cross_hash:
            raise ValueError("Cross-generator result and corpus hashes differ")
        cross_trials += len(cross["trials"])
        groups = result["groups"]
        name, description = LABELS[result["model"]]
        models.append(
            {
                "id": result["model"],
                "name": name,
                "description": description,
                "family": "wenet" if result["model"].startswith("wenet") else "zh_en",
                "groups": groups,
                "room": {
                    str(scale): aggregate(
                        t for t in original if t["group"] == "fresh_room" and t["playback_scale"] == scale
                    )
                    for scale in (0.75, 0.35)
                },
                "monitor": {
                    str(scale): aggregate(
                        t for t in original if t["group"] == "sink_monitor" and t["playback_scale"] == scale
                    )
                    for scale in (0.75, 0.35)
                },
                "phase": {
                    label: [t["hits"] for t in trials if t["id"] == record_id]
                    for label, record_id in (
                        ("A", "1791734490-wake-direct"),
                        ("B", "1791734683-wake-aec"),
                    )
                },
                "voices": {
                    voice: aggregate(
                        t for t in original if t["group"] == "digital_positive" and t["voice"] == voice
                    )
                    for voice in manifest["voices"].values()
                },
                "speeds": {
                    str(speed): aggregate(
                        t
                        for t in original
                        if t["group"] == "digital_positive"
                        and t["speed"] == speed
                        and by_id[t["id"]]["style"] == "isolated"
                    )
                    for speed in (0.8, 1.0, 1.2)
                },
                "styles": {
                    style: aggregate(
                        t
                        for t in original
                        if t["group"] == "digital_positive" and by_id[t["id"]]["style"] == style
                    )
                    for style in ("inline", "paused_greeting")
                },
                "compute": result["call_compute_ms"],
                "rtf": result["compute_rtf"],
                "memory": result["worker_peak_rss_mb"],
                "initialization": result["initialization_ms"],
                "cross": aggregate(cross["trials"]),
                "false_examples": [
                    {
                        "text": next(
                            r.get("text", r["id"]) for r in manifest["records"] if r["id"] == t["id"]
                        ),
                        "group": t["group"],
                        "count": len(t["extra_hits"]),
                    }
                    for t in original
                    if not t["targets"] and t["extra_hits"] and t["group"] != "continuous_negative"
                ][:8],
            }
        )
    samples = []
    for phrase in manifest["wake_words"]:
        source = next(
            r
            for r in manifest["records"]
            if r["group"] == "digital_positive"
            and r["voice"] == "zf_001"
            and r["speed"] == 1.0
            and r["style"] == "isolated"
            and r["phrase"] == phrase
        )
        paths = [("合成源语音", source)]
        for group, label in (("sink_monitor", "输出监视器"), ("fresh_room", "客厅麦克风")):
            paths.append(
                (
                    label,
                    next(
                        r
                        for r in manifest["records"]
                        if r["group"] == group
                        and r["source_id"] == source["id"]
                        and r["playback_scale"] == 0.75
                    ),
                )
            )
        samples.append(
            {
                "phrase": phrase,
                "clips": [
                    {"label": label, "path": quote(os.path.relpath(record["path"], output.parent))}
                    for label, record in paths
                ],
            }
        )
    return {
        "models": models,
        "samples": samples,
        "audio_count": len(manifest["records"]),
        "main_trials": main_trials,
        "cross_trials": cross_trials,
        "human_samples": manifest["human_samples"],
        "manifest_sha256": expected_hash,
        "hardware": json.loads((folder / "hardware.json").read_text()),
        "parameters": result["decoder_parameters"],
    }


def make_report(folder, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    data = build_data(folder.resolve(), output.resolve())
    template = Path(__file__).with_name("templates") / "wake-report.html"
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    document = template.read_text().replace("__REPORT_DATA__", payload)
    markdown = output.with_suffix(".md")
    document = document.replace(
        "__MARKDOWN_HREF__", html.escape(quote(os.path.relpath(markdown, output.parent)))
    )
    document = document.replace(
        "__CSV_HREF__", html.escape(quote(os.path.relpath(folder / "trials.csv", output.parent)))
    )
    output.write_text(document)
    print(f"Created {output.resolve()} ({output.stat().st_size:,} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=Path("state/wake-comparison"))
    parser.add_argument("--output", type=Path, default=Path("reports/WAKE_MODEL_COMPARISON.html"))
    args = parser.parse_args()
    make_report(args.folder, args.output)


if __name__ == "__main__":
    main()
