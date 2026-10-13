"""Build the local, self-contained visual report from completed benchmark JSON."""

import copy
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import statistics
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "state/light-kws-20261013"


def read(path):
    return json.loads((ROOT / path).read_text())


def summarize(path, name):
    run = read(path)
    if not run.get("complete"):
        raise ValueError(f"Incomplete benchmark: {path}")
    trials = run["trials"]
    groups = {}
    for row in trials:
        key = row["cohort"] + "/" + row["variant"]
        group = groups.setdefault(
            key,
            {
                "clips": 0,
                "targets": 0,
                "hits": 0,
                "negative_clips": 0,
                "false_clips": 0,
                "false_events": 0,
                "extra_events": 0,
                "seconds": 0.0,
            },
        )
        group["clips"] += 1
        group["targets"] += row["targets"]
        group["hits"] += row["hits"]
        group["seconds"] += row["seconds"]
        if not row["positive"]:
            group["negative_clips"] += 1
            group["false_clips"] += int(row["detected"])
            group["false_events"] += len(row["events"])
        else:
            group["extra_events"] += len(row["extra_events"])
    original = [
        r
        for r in trials
        if r["cohort"] in ("human_positive", "human_negative") and r["variant"] == "original"
    ]
    performance = [r for r in original if r["cohort"] == "human_positive"]
    return {
        "id": Path(path).stem,
        "name": name,
        "groups": groups,
        "hits": sum(r["hits"] for r in original),
        "targets": 14,
        "false": sum(r["detected"] for r in original if not r["positive"]),
        "negative_clips": 4,
        "resident_mib": run["resident_idle"]["rss_end_mib"],
        "cpu": run["silent_stream"]["machine_capacity_percent"],
        "listening_seconds": run["silent_stream"]["wall_s"],
        "idle_cpu": run["resident_idle"]["machine_capacity_percent"],
        "peak_mib": run["worker_peak_mib"],
        "chunk_ms": run["chunk_ms"],
        "threshold": run["threshold"],
        "model": run["model_metadata"],
        "torch_imported": run["torch_imported"],
        "full_clip_median_ms": statistics.median(r["wall_s"] * 1000 for r in performance),
        "human_speech_cpu_budget": sum(r["metrics"]["cpu_s"] for r in performance)
        / sum(r["seconds"] for r in performance)
        * 100
        / 6,
        "speech_call_p95_median_ms": statistics.median(r["call_ms"]["p95"] for r in performance),
        "silent_call_p95_ms": run["silent_stream"]["call_ms"]["p95"],
        "paced": run["paced"],
        "cases": original,
        "false_cases": [
            r for r in trials if not r["positive"] and r["detected"] and r["variant"] == "original"
        ],
        "trial_count": len(trials),
        "path": str(path),
        "human_manifest_sha256": run["human_manifest_sha256"],
    }


def main():
    models = [
        summarize("state/light-kws-20261013/mdtc-small-c20.json", "定制 MDTC · 3.5 万参数"),
        summarize("state/light-kws-20261013/fsmn-custom-c20.json", "定制 FSMN · 初始阈值"),
        summarize("state/light-kws-20261013/fsmn-calibrated-c20.json", "定制 FSMN · 校准后 / 20ms"),
        summarize("state/light-kws-20261013/fsmn-calibrated-c80.json", "定制 FSMN · 校准后 / 80ms"),
    ]
    human = read("state/wake-human-comparison-20261013/manifest.json")
    expected_hash = hashlib.sha256(
        (ROOT / "state/wake-human-comparison-20261013/manifest.json").read_bytes()
    ).hexdigest()
    if any(m["human_manifest_sha256"] != expected_hash for m in models):
        raise ValueError("Human cohort changed")
    previous = read("state/wake-human-comparison-20261013/result-zh_en_c16_int8.json")
    baseline = read("state/asr-candidates-20261013/kws-t1.json")
    references = []
    for model, mode, name, threads in [
        ("kws", "keyword", "实验对照 · Zipformer 单线程", 1),
        ("paraformer", "stream600", "未部署 · Paraformer 600ms", 2),
        ("qwen", "name_context", "未部署 · Qwen3-ASR 名称上下文", 2),
        ("kws", "keyword", "历史基线 · Zipformer 双线程", 2),
    ]:
        run = read(f"state/asr-candidates-20261013/{model}-t{threads}.json")
        trials = [t for t in run["trials"] if t["mode"] == mode]
        silent = run.get("silent_stream", run.get("silent_vad_gated"))
        references.append(
            {
                "id": f"{model}-t{threads}",
                "name": name,
                "hits": sum(t["hit"] for t in trials),
                "targets": 14,
                "false": sum(t["false_wake"] for t in trials),
                "resident_mib": run["resident_idle"]["rss_end_mib"],
                "cpu": silent["machine_capacity_percent"],
                "listening_seconds": silent["wall_s"],
                "cpu_note": "仅VAD工作，ASR调用0次" if model == "qwen" else "持续输入静音",
                "full_clip_median_ms": statistics.median(t["metrics"]["wall_s"] * 1000 for t in trials),
                "cases": [{k: t[k] for k in ("id", "positive", "hit", "false_wake")} for t in trials],
            }
        )
    official = []
    for variant, name in [("finetune", "官方“小云”权重"), ("base", "官方通用基础权重")]:
        r = read(f"state/light-kws-20261013/fsmn-{variant}-reference.json")
        cases = r["records"][:18]
        official.append(
            {
                "name": name,
                "hits": sum(t["detected"] for t in cases if t["positive"]),
                "false": sum(t["detected"] for t in cases if not t["positive"]),
                "synthetic_hits": sum(t["detected"] for t in r["records"][18:] if t["positive"]),
                "note": "官方 FunASR 整段接口；与流式时序结果分开解读",
            }
        )
    selected = models[2]
    corpus = read("state/light-kws-20261013/corpus/manifest.json")
    calibration = read("state/light-kws-20261013/fsmn-stream-calibration.json")
    split_counts = {
        s: {
            "voices": len(corpus["speaker_splits"][s]),
            "positive": sum(r["positive"] for r in corpus["records"] if r["split"] == s),
            "negative": sum(not r["positive"] for r in corpus["records"] if r["split"] == s),
        }
        for s in ("train", "validation", "test")
    }
    md_training = read("models/light-kws-20261013/mdtc-small/training.json")
    fs_training = read("models/light-kws-20261013/fsmn-custom/training.json")
    data = {
        "models": models,
        "references": references,
        "deployment": read("state/light-kws-20261013/deployment-snapshot.json")
        if (FOLDER / "deployment-snapshot.json").exists()
        else None,
        "official": official,
        "selected": selected["id"],
        "corpus": split_counts,
        "calibration": calibration,
        "training": {"mdtc": md_training, "fsmn": fs_training},
        "parity": read("state/light-kws-20261013/fsmn-stream-parity.json"),
        "human_hash": expected_hash,
        "human_count": 18,
        "independent_human_speakers": 1,
        "hardware": read("state/wake-human-comparison-20261013/hardware.json"),
        "old_groups": previous["groups"],
        "baseline_paced": baseline.get("paced_speech", []),
        "audio": [
            {"id": r["id"], "text": r.get("text", ""), "path": str(Path(r["path"]).relative_to(ROOT))}
            for r in human["records"][:18]
        ],
        "sources": [
            {"title": "WeKWS：训练、MDTC 与流式 ONNX", "url": "https://github.com/wenet-e2e/wekws"},
            {
                "title": "官方 FSMN 中文唤醒模型",
                "url": "https://modelscope.cn/models/iic/speech_charctc_kws_phone-xiaoyun",
            },
            {
                "title": "FunASR 关键词检测",
                "url": "https://github.com/modelscope/FunASR/blob/main/docs/keyword_spotting.md",
            },
            {"title": "Kokoro 合成模型与声线说明", "url": "https://github.com/k2-fsa/sherpa-onnx/pull/1942"},
        ],
    }
    data["replications"] = [read(f"state/light-kws-20261013/replicate-c{chunk}.json") for chunk in (20, 80)]
    old_hits = {r["id"] for r in references[0]["cases"] if r["hit"]}
    new_hits = {r["id"] for r in selected["cases"] if r["hits"]}
    data["paired_human"] = {
        "recovered": sorted(new_hits - old_hits),
        "regressed": sorted(old_hits - new_hits),
    }
    (FOLDER / "summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    with (FOLDER / "light-wake-trials.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "model",
                "id",
                "cohort",
                "variant",
                "targets",
                "hits",
                "false_events",
                "seconds",
                "peak_score",
                "wall_s",
            ],
        )
        writer.writeheader()
        for model in models:
            for t in read(model["path"])["trials"]:
                writer.writerow(
                    {
                        "model": model["id"],
                        "id": t["id"],
                        "cohort": t["cohort"],
                        "variant": t["variant"],
                        "targets": t["targets"],
                        "hits": t["hits"],
                        "false_events": len(t["events"]) if not t["positive"] else len(t["extra_events"]),
                        "seconds": t["seconds"],
                        "peak_score": t["peak_score"],
                        "wall_s": t["wall_s"],
                    }
                )
    template = (ROOT / "scripts/templates/light-wake-report.html").read_text()
    output = ROOT / "reports/LIGHT_WAKE_COMPARISON.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    display = copy.deepcopy(data)
    for entry in display["models"] + display["audio"]:
        entry["path"] = quote(os.path.relpath(ROOT / entry["path"], output.parent))
    encoded = json.dumps(display, ensure_ascii=False).replace("</", "<\\/")
    candidate = "优先验收阈值 0.265、80ms 输入配置。真人命中 13/14；新合成负样本误触发 3/112。需补充新的真人录音与现场验证。"
    refinement = ""
    refinement_path = ROOT / "state/fsmn-v2-20261013/summary.json"
    if refinement_path.exists():
        latest = json.loads(refinement_path.read_text())
        candidate = (
            f"第二轮合成语音定制，阈值 {latest['selected']['threshold']}，80ms 输入。"
            f"真人命中 {latest['human_v2']['hits']}/14；扩充合成负样本误触发 "
            f"{latest['synthetic_v2']['false_clips']}/{latest['synthetic_v2']['negative_clips']}。"
            "已正式部署；详细数据与部署验收见第二轮报告。"
        )
        refinement = (
            '<section id="refinement"><span class="pill">新：第二轮合成语音定制</span>'
            "<h2>后续训练结果已更新</h2><p>"
            + html.escape(latest["verdict"])
            + f"。同条件真人命中：V1 {latest['human_v1']['hits']}/14，"
            + f"V2 {latest['human_v2']['hits']}/14。当前正式使用 FSMN V2。"
            + '</p><a href="FSMN_REFINEMENT.html">查看第二轮训练、误唤醒和资源对照</a>'
            + '<p class="note">下文保留第一轮的完整模型对比，便于追溯。</p></section>'
        )
    result = (
        template.replace("__DATA__", encoded)
        .replace("__REFINEMENT__", refinement)
        .replace("__CANDIDATE_SUMMARY__", html.escape(candidate))
        .replace("__HITS__", str(selected["hits"]))
        .replace("__MEMORY__", f"{models[3]['resident_mib']:.0f}")
        .replace("__CPU__", f"{models[3]['cpu']:.2f}")
    )
    output.write_text(result)
    print(
        json.dumps(
            {
                "path": str(output.relative_to(ROOT)),
                "human_hits": selected["hits"],
                "memory_mib": selected["resident_mib"],
                "cpu": selected["cpu"],
                "trial_count": sum(m["trial_count"] for m in models),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
