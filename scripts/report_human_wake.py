"""Publish separate human wake evaluation in offline HTML, Markdown and CSV."""

import argparse
import csv
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

from scripts.report_wake_html import LABELS, aggregate


def load_data(folder, previous, output):
    manifest_file = folder / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    corpus_hash = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    index = {r["id"]: r for r in manifest["records"]}
    models, csv_rows = [], []
    summaries = json.loads((folder / "summary.json").read_text())
    for summary in summaries:
        r = json.loads((folder / f"result-{summary['model']}.json").read_text())
        old = json.loads((previous / f"result-{summary['model']}.json").read_text())
        if r["manifest_sha256"] != corpus_hash:
            raise ValueError("Human model results and immutable corpus hash differ")
        if r["file_sha256"] != old["file_sha256"] or r["decoder_parameters"] != old["decoder_parameters"]:
            raise ValueError("Previous and human runs must use identical model weights and decoder settings")
        original = [t for t in r["trials"] if t["variant"] == "original"]
        cases = [
            {
                "id": t["id"],
                "text": index[t["id"]]["text"],
                "style": index[t["id"]]["style"],
                "hits": t["hits"],
                "targets": t["targets"],
                "hits_s": t["hits_s"],
                "detected_words": t["detected_words"],
                "false_hits": len(t["extra_hits"]) if not t["targets"] else 0,
                "phase": [
                    v["hits"]
                    for v in r["trials"]
                    if v["id"] == t["id"]
                    and (v["variant"] == "original" or v["variant"].startswith("phase_"))
                ],
            }
            for t in original
            if t["group"] in ("human_positive", "human_negative")
        ]
        name, description = LABELS[r["model"]]
        models.append(
            {
                "id": r["model"],
                "name": name,
                "description": description,
                "groups": r["groups"],
                "cases": cases,
                "styles": {
                    style: aggregate(t for t in original if index[t["id"]].get("style") == style)
                    for style in ("short", "long", "inline", "inline-long")
                },
                "compute": r["call_compute_ms"],
                "rtf": r["compute_rtf"],
                "memory": r["worker_peak_rss_mb"],
                "initialization": r["initialization_ms"],
                "previous_digital": old["groups"]["digital_positive/original"],
            }
        )
        for t in r["trials"]:
            csv_rows.append(
                {
                    "model": r["model"],
                    "id": t["id"],
                    "group": t["group"],
                    "text": index[t["id"]].get("text", "derived continuous/silence stream"),
                    "variant": t["variant"],
                    "seconds": t["seconds"],
                    "targets": t["targets"],
                    "hits": t["hits"],
                    "negative_false_hits": len(t["extra_hits"]) if not t["targets"] else 0,
                    "positive_extra_hits": len(t["extra_hits"]) if t["targets"] else 0,
                    "detections_audio_s": json.dumps(t["hits_s"]),
                    "detected_words": json.dumps(t["detected_words"], ensure_ascii=False),
                }
            )
    with (folder / "trials.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    return {
        "models": models,
        "manifest_sha256": corpus_hash,
        "replays": len(csv_rows),
        "human_samples": manifest["human_samples"],
        "positive_count": manifest["original_positive_recordings"],
        "negative_count": manifest["original_negative_recordings"],
        "parameters": r["decoder_parameters"],
        "hardware": json.loads((folder / "hardware.json").read_text()),
        "limits": manifest["limitations"],
        "corpus_audio_files": len(manifest["records"]),
        "audio": [
            {
                "id": record["id"],
                "text": record["text"],
                "path": quote(os.path.relpath(record["path"], output.parent)),
            }
            for record in manifest["records"]
            if record["group"] in ("human_positive", "human_negative")
        ],
        "csv_href": quote(os.path.relpath(folder / "trials.csv", output.parent)),
    }


def count(x):
    return f"{x['hits']}/{x['targets']} ({x['hits'] / x['targets']:.1%})" if x["targets"] else "—"


def write_markdown(data, output):
    lines = [
        "# 真人唤醒词完整测试报告",
        "",
        "测试日期：2026-10-13（Asia/Shanghai）。",
        "",
        "## 判断",
        "",
        "本轮七种默认配置都未做到逐次稳定唤醒。合成语音的模型排序不能直接外推为用户真人使用表现。",
        "当前模型保留，先根据用户真人样本校准候选配置；没有本轮换模或部署参数变更。",
        "",
        "## 范围与计数",
        "",
        f"一个说话人、客厅原始摄像头麦克风；{data['positive_count']} 段唤醒、{data['negative_count']} 段负样本，共 {data['human_samples']} 段真人录音。",
        "用户已试听完整回放并确认录音完整。第一轮未听清提示的四段负样本排除，使用用户确认的四段补录。",
        "七种配置与前次权重哈希、词表和 decoder 参数逐一一致。每配置 287 条件，总计 2009 次离线回放。",
        "14 段正样本各做 16 种相位、两档振幅、两档白噪声；四段负样本原样；另测重复负流、混合连续流及60秒静音。",
        "未重新进行音箱播放或麦克风录制，也没有生成新音色或真实快慢速/距离矩阵。所有变体共用原始录音，不算独立真人准确率。",
        "提示音保留。期望事件使用用户确认的整段录音窗口，未标注精确词尾，不报告词尾延迟；时间戳只表示音频流位置。",
        "",
        "## 原始录音和连续流",
        "",
        "| 配置 | 原始唤醒 | 独立短词 | 独立长词 | 短词接问题 | 长词接问题 | 原始负样本误触发 | 重复负流误触发 | 混合流唤醒 / 额外命中 |",
        "|---|---|---|---|---|---|---:|---:|---|",
    ]
    for m in data["models"]:
        groups = m["groups"]
        mixed = groups["human_continuous_mixed/original"]
        lines.append(
            "| "
            + " | ".join(
                [
                    m["id"],
                    count(groups["human_positive/original"]),
                    *[count(m["styles"][k]) for k in ("short", "long", "inline", "inline-long")],
                    str(groups["human_negative/original"]["negative_false_hits"]),
                    str(groups["human_continuous_negative/original"]["negative_false_hits"]),
                    f"{count(mixed)} / {mixed['extra']}",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 压力条件",
        "",
        "| 配置 | 16种相位（含原始） | 振幅0.25 | 振幅0.1 | 白噪声15dB | 白噪声5dB |",
        "|---|---|---|---|---|---|",
    ]
    for m in data["models"]:
        g = m["groups"]
        base, phase = g["human_positive/original"], g["human_positive/phase"]
        all_phase = {"hits": base["hits"] + phase["hits"], "targets": base["targets"] + phase["targets"]}
        lines.append(
            "| "
            + " | ".join(
                [m["id"], count(all_phase)]
                + [
                    count(g["human_positive/" + k])
                    for k in ("amplitude_0.25", "amplitude_0.1", "white_noise_15db", "white_noise_5db")
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 计算与资源",
        "",
        "每帧 keyword() 含缓存调用；RTF 为计算墙钟时间 / 音频时长。内存为整个 worker，不是模型独占。成功时间戳不等同于唤醒后实际出声。",
        "",
        "| 配置 | 初始化ms | 计算P50/P95/最大ms | RTF | 进程峰值MiB |",
        "|---|---:|---|---:|---:|",
    ]
    for m in data["models"]:
        c = m["compute"]
        lines.append(
            f"| {m['id']} | {m['initialization']:.1f} | {c['p50']}/{c['p95']}/{c['max']} | {m['rtf']:.4f} | {m['memory']:.1f} |"
        )
    lines += [
        "",
        "## 原始录音逐条命中",
        "",
        "|录音|内容|" + "|".join(m["id"] for m in data["models"]) + "|",
        "|---|---|" + "---|" * len(data["models"]),
    ]
    for i, case in enumerate(data["models"][0]["cases"]):
        values = (
            ["命中" if m["cases"][i]["hits"] else "漏检" for m in data["models"]]
            if case["targets"]
            else [f"误触发{m['cases'][i]['false_hits']}次" for m in data["models"]]
        )
        lines.append("| " + " | ".join([case["id"], case["text"], *values]) + " |")
    negative = data["models"][0]["groups"]["human_continuous_negative/original"]
    lines += [
        "",
        "## 限制与复现",
        "",
        f"重复负流长 {negative['negative_seconds']:.2f} 秒，由四句话反复排列组成，不能当作十分钟独立讲话或产品每小时打扰率。",
        "混合流只测持续 KWS，不模拟助手被唤醒后进入会话或门控；其额外命中不混入独立负样本误触发统计。",
        "未使用这些样本调参；后续若用于校准，必须另留或重新录制独立验收集。一个人一个位置仍不足以覆盖日常变化。",
        f"CPU：{data['hardware']['cpu']}。主清单SHA256：`{data['manifest_sha256']}`。",
        "",
        "```sh",
        ".venv/bin/python -m scripts.compare_wake_suite --config config.toml --folder state/wake-human-comparison-20261013 --all",
        ".venv/bin/python -m scripts.report_human_wake --folder state/wake-human-comparison-20261013",
        "```",
        "",
        "逐条件CSV和JSON仅存放在本地私有 state/；真人波形没有上传或写入Git。",
    ]
    output.write_text("\n".join(lines) + "\n")


def make_report(folder, previous, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    data = load_data(folder.resolve(), previous.resolve(), output.resolve())
    markdown = output.with_suffix(".md")
    write_markdown(data, markdown)
    templates = Path(__file__).with_name("templates")
    css = (templates / "wake-report.html").read_text().split("<style>", 1)[1].split("</style>", 1)[0]
    document = (templates / "human-wake-report.html").read_text()
    document = document.replace("__SHARED_CSS__", css).replace(
        "__REPORT_DATA__", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    )
    document = document.replace("__MARKDOWN_HREF__", html.escape(quote(markdown.name)))
    output.write_text(document)
    print(f"Created {output} and {markdown}; CSV {data['replays']} rows")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=Path("state/wake-comparison"))
    parser.add_argument("--output", type=Path, default=Path("reports/WAKE_HUMAN_COMPARISON.html"))
    args = parser.parse_args()
    make_report(args.folder, args.previous, args.output)


if __name__ == "__main__":
    main()
