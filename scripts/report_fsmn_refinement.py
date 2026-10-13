"""Render completed synthetic-only FSMN refinement results; no model selection."""

import csv
import html
import json
import os
import re
from pathlib import Path
from collections import Counter
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "state/fsmn-v2-20261013"


def read(name):
    return json.loads((FOLDER / name).read_text())


def metric(run, cohort, variant="original"):
    rows = [r for r in run["trials"] if r["cohort"] == cohort and r["variant"] == variant]
    return {
        "clips": len(rows),
        "targets": sum(r["targets"] for r in rows),
        "hits": sum(r["hits"] for r in rows),
        "negative_clips": sum(not r["positive"] for r in rows),
        "false_clips": sum(not r["positive"] and r["detected"] for r in rows),
        "false_events": sum(len(r["events"]) for r in rows if not r["positive"]),
    }


def table(headers, rows):
    return (
        '<div class="tablewrap"><table><thead><tr>'
        + "".join("<th>" + html.escape(str(c)) + "</th>" for c in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + html.escape(str(c)) + "</td>" for c in row) + "</tr>" for row in rows
        )
        + "</tbody></table></div>"
    )


def bar(label, hits, total):
    return f'<div class="row"><div>{html.escape(label)}</div><div class="bar"><span style="width:{100 * hits / max(total, 1):.1f}%"></span></div><div class="number">{hits}/{total}</div></div>'


def main():
    old, new = read("v1-c80.json"), read("selected-c80.json")
    assert old["complete"] and new["complete"]
    assert old["human_manifest_sha256"] == new["human_manifest_sha256"]
    assert [(r["id"], r["variant"]) for r in old["trials"]] == [
        (r["id"], r["variant"]) for r in new["trials"]
    ]
    paired_rows = []
    for before, after in zip(old["trials"], new["trials"]):
        paired_rows.append(
            {
                "id": after["id"],
                "cohort": after["cohort"],
                "variant": after["variant"],
                "targets": after["targets"],
                "v1_hits": before["hits"],
                "v2_hits": after["hits"],
                "v1_detected": before["detected"],
                "v2_detected": after["detected"],
                "v1_peak_score": before["peak_score"],
                "v2_peak_score": after["peak_score"],
            }
        )
    with (FOLDER / "paired-trials.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(paired_rows[0]))
        writer.writeheader()
        writer.writerows(paired_rows)
    selection = read("frozen-selection.json")
    corpus = read("corpus/manifest.json")
    resources = read("selected-resources.json")
    human_old, human_new = metric(old, "human_positive"), metric(new, "human_positive")
    synth_old, synth_new = metric(old, "new_synthetic_test"), metric(new, "new_synthetic_test")
    improved = (
        human_new["hits"] >= human_old["hits"]
        and synth_new["hits"] >= synth_old["hits"]
        and synth_new["false_clips"] <= synth_old["false_clips"]
        and (
            human_new["hits"] > human_old["hits"]
            or synth_new["hits"] > synth_old["hits"]
            or synth_new["false_clips"] < synth_old["false_clips"]
        )
    )
    verdict = (
        "新一轮在这些指标上有改善，仍需新的现场验收"
        if improved
        else "训练已完成，但没有在所有关键指标上同时胜出"
    )
    summary = {
        "selected": selection["selected"],
        "human_v1": human_old,
        "human_v2": human_new,
        "synthetic_v1": synth_old,
        "synthetic_v2": synth_new,
        "verdict": verdict,
        "human_training_recordings": 0,
        "production_changed": (ROOT / "state/acceptance/fsmn-v2/deployment.json").exists(),
    }
    (FOLDER / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    style = (
        (ROOT / "scripts/templates/light-wake-report.html")
        .read_text()
        .split("<style>", 1)[1]
        .split("</style>", 1)[0]
    )
    sections = []
    deployment_file = ROOT / "state/acceptance/fsmn-v2/deployment.json"
    if deployment_file.exists():
        deployed = json.loads(deployment_file.read_text())
        sections.append(
            '<section><span class="pill">正式部署</span><h2>现在使用 FSMN V2</h2>'
            "<p>生产插件与实验权重一致；真人正样本 14/14、负样本 0/4 误触发；"
            "十分钟加速静音输入后缓存有界，之后仍能唤醒。推理不加载 Torch 或 FunASR。"
            "待机特征缓存最多 136 帧，CTC 概率保留最近 100 帧。旧模型没有回退分支。</p>"
            "<p>核实时间：" + html.escape(deployed["checked_at"]) + "（部署快照，非实时监控）。</p>"
            '<p><a href="../state/acceptance/fsmn-v2/deployment.json">部署状态</a> · '
            '<a href="../state/acceptance/fsmn-v2/runtime-validation.json">正式插件验收</a></p></section>'
        )
    sections.append(
        "<section><h2>新旧候选，同一批声音重测</h2><p>正式版本已采用 FSMN V2；这里保留两代 FSMN 的同条件离线对照，均使用 80ms 输入。旧 Zipformer 的历史结果为 9/14。</p>"
        + bar("上一代 FSMN · 真人", human_old["hits"], human_old["targets"])
        + bar("本轮 FSMN · 真人", human_new["hits"], human_new["targets"])
        + "<h3>合成测试集命中（含声学变体）</h3>"
        + bar("上一代 FSMN", synth_old["hits"], synth_old["targets"])
        + bar("本轮 FSMN", synth_new["hits"], synth_new["targets"])
        + '<p class="note">提高命中率和减少误触发必须同时看；声学变体与源录音相关，不能当作独立人群。</p></section>'
    )
    groups = [
        ("human_positive", "真人唤醒词"),
        ("human_negative", "真人非唤醒"),
        ("new_synthetic_test", "扩充合成测试集"),
        ("digital_positive", "旧合成唤醒词"),
        ("digital_negative", "旧合成非唤醒"),
        ("historical_room", "历史录音 A/B"),
        ("human_continuous_mixed", "真人拼接流"),
        ("human_continuous_negative", "真人重复负流"),
        ("continuous_negative", "旧合成重复负流"),
        ("silence", "静音"),
    ]
    rows = []
    for key, label in groups:
        a, b = metric(old, key), metric(new, key)
        rows.append(
            [
                label,
                f"{a['hits']}/{a['targets']}" if a["targets"] else "—",
                f"{b['hits']}/{b['targets']}" if b["targets"] else "—",
                f"{a['false_clips']}/{a['negative_clips']}" if a["negative_clips"] else "—",
                f"{b['false_clips']}/{b['negative_clips']}" if b["negative_clips"] else "—",
            ]
        )
    sections.append(
        "<section><h2>误唤醒有没有一起变好？</h2>"
        + table(["数据组", "V1命中", "V2命中", "V1误触发片段", "V2误触发片段"], rows)
        + '<p class="note">连续负流每条作为一个片段；是否误触发与事件总数并非同一指标。不是实际全天误唤醒率。</p></section>'
    )
    negative = [
        r
        for r in new["trials"]
        if not r["positive"]
        and r["detected"]
        and r["variant"] == "original"
        and "continuous" not in r["cohort"]
    ]
    counts = Counter(r["text"] for r in negative)
    sections.append(
        "<section><h2>仍然会误触发的说法</h2>"
        + table(["内容", "触发片段数"], sorted(counts.items()))
        + "<p>“都督”与“嘟嘟”同音。模型使用官方发音词典及完整文本 CTC 标签，没有把同音语音标成“没有这两个音节”来制造虚假区分。“督促”等近音词的误识别则单独检查。</p></section>"
    )
    phrase_rows = []
    for prefix, label in [
        ("short", "嘟嘟：单独说"),
        ("long", "你好嘟嘟：单独说"),
        ("inline", "嘟嘟：紧接问题"),
        ("inline-long", "你好嘟嘟：紧接问题"),
    ]:
        values = []
        for run in [old, new]:
            records = [
                r
                for r in run["trials"]
                if r["variant"] == "original" and re.fullmatch("human-" + prefix + r"-\d+", r["id"])
            ]
            values.append(f"{sum(r['hits'] for r in records)}/{len(records)}")
        phrase_rows.append([label, *values])
    sections.append(
        "<section><h2>两个唤醒词分别怎么样？</h2>"
        + table(["真人说法", "V1", "V2"], phrase_rows)
        + '<p class="note">仍采用“嘟嘟／你好嘟嘟”都有效的等效触发模式；说长词时也可能由其中的短词触发。</p></section>'
    )
    phase_results = []
    for run in [old, new]:
        records = [
            r
            for r in run["trials"]
            if r["cohort"] == "human_positive"
            and (r["variant"] == "original" or r["variant"].startswith("phase-"))
        ]
        phase_results.append(f"{sum(r['hits'] for r in records)}/{sum(r['targets'] for r in records)}")
    stress_rows = [["16个前置分块偏移", *phase_results]]
    for variant, label in [
        ("amplitude-0.25", "音量 ×0.25"),
        ("amplitude-0.1", "音量 ×0.1"),
        ("white-noise-15db", "白噪 15dB"),
        ("white-noise-5db", "白噪 5dB"),
    ]:
        a, b = metric(old, "human_positive", variant), metric(new, "human_positive", variant)
        stress_rows.append([label, f"{a['hits']}/{a['targets']}", f"{b['hits']}/{b['targets']}"])
    sections.append(
        "<section><h2>派生压力条件</h2>"
        + table(["条件", "V1", "V2"], stress_rows)
        + '<p class="note">对同一批真人录音人工变换，只是稳健性探测，不是额外真人样本；完整前置分块偏移结果保留在原始 JSON。</p></section>'
    )
    audio_index = {r["id"]: r for r in corpus["records"]}
    audio_examples = []
    for row in negative:
        source = audio_index.get(row["id"])
        if source is None:
            continue
        href = quote(os.path.relpath(source["path"], ROOT / "reports"))
        label = row["text"] + ("（声学变体）" if source.get("augmentation") else "（合成原声）")
        audio_examples.append(
            "<p>"
            + html.escape(label)
            + '</p><audio controls preload="none" src="'
            + html.escape(href)
            + '"></audio>'
        )
    if audio_examples:
        sections.append(
            "<section><h2>试听误触发样本</h2><details><summary>展开本轮合成测试的误触发声音（不会自动播放）</summary>"
            + "".join(audio_examples)
            + "</details></section>"
        )
    pairs = []
    for row in new["trials"]:
        if row["cohort"] not in ("human_positive", "human_negative") or row["variant"] != "original":
            continue
        before = next(r for r in old["trials"] if r["id"] == row["id"] and r["variant"] == "original")
        pairs.append(
            [
                row["id"],
                row["text"],
                "触发" if before["detected"] else "静默",
                "触发" if row["detected"] else "静默",
            ]
        )
    sections.append(
        "<section><h2>真人逐段对照</h2>"
        + table(["录音", "内容", "上一代", "本轮"], pairs)
        + '<p class="note">真人录音只在三种合成验证方案完成选择并冻结后打开评估。仍是之前已查看过的同一批18段录音，不是新的盲测。</p></section>'
    )
    baseline_calibration = read("calibration-v1.json")
    calibration_rows = [
        [
            "旧模型：原阈值",
            0.265,
            sum(r["positive"] and r["peak_score"] >= 0.265 for r in baseline_calibration["scores"]),
            sum(not r["positive"] and r["peak_score"] >= 0.265 for r in baseline_calibration["scores"]),
        ]
    ]
    old_selection = baseline_calibration["selection"]
    calibration_rows.append(
        [
            "旧模型：同验证集重新校准",
            old_selection["threshold"],
            old_selection["hits"],
            old_selection["false_clips"],
        ]
    )
    for candidate in selection["candidates"]:
        calibration_rows.append(
            [
                "新模型：" + candidate["name"],
                candidate["threshold"],
                candidate["hits"],
                candidate["false_clips"],
            ]
        )
    sections.append(
        "<section><h2>训练与降低阈值，分开看</h2>"
        + table(["同一合成验证集", "阈值", "命中片段", "误触发片段"], calibration_rows)
        + "<p>此处用相同验证数据比较分离能力。上面的最终测试比较“旧权重＋原阈值”与“新权重＋新校准阈值”，属于训练与校准的综合变化，不能把全部提升归因于训练。旧模型重新校准的这一行只报告验证集表现。</p></section>"
    )
    split = Counter(r["split"] for r in corpus["records"])
    train_rows = []
    for c in selection["candidates"]:
        training = json.loads((ROOT / "models/fsmn-v2-20261013" / c["name"] / "training.json").read_text())
        train_rows.append(
            [
                c["name"],
                len(training["history"]),
                f"{c['validation_loss']:.5f}",
                c["threshold"],
                f"{c['hits']}/{c['targets']}",
                f"{c['false_clips']}/{c['negative_clips']}",
                "合成验证选中" if c["name"] == selection["selected"]["name"] else "对照",
            ]
        )
    sections.append(
        "<section><h2>这次训练了什么</h2><p>训练 "
        + str(split["train"])
        + " 条，验证 "
        + str(split["validation"])
        + " 条，测试 "
        + str(split["test"])
        + " 条；每段源合成语音附一段确定性的声学变体。20种合成声线固定分组，变体跟随原始声音留在同一组。真人训练数据为 <strong>0</strong>。</p><p>新增正样本覆盖短词、长词、疑问语气及紧接指令；负样本覆盖“督促、监督、独立、读读、肚子、速度”等。轻微变速、滤波、回声和噪声仅用于离线语料增强，没有对实时播放加增益。</p>"
        + table(["训练方式", "轮数", "验证CTC损失", "阈值", "验证命中", "验证误触发", "状态"], train_rows)
        + "<p>continue 从上一代定制权重继续训练；rebase 从公开基础权重重新微调；focused 继续训练上一代权重，但将无关文字合并为背景，只优化触发音节的 CTC 目标。不同目标的 CTC 损失不可横向比较，最终按同一流式唤醒验证指标选候选。各自按合成验证损失选检查点，再用合成流式验证分数校准阈值，以漏检率与误触发率的平均值选择工作点；选择冻结后才评估真人。训练设置轮数是上限，启用早停的方案连续5轮验证损失不改善就停止。</p></section>"
    )
    speed = []
    for label, run in [("V1", old), ("V2", new)]:
        for row in run["paced"]:
            speed.append(
                [label, row["id"], f"{row['events'][0]['wall_s']:.3f}s" if row["events"] else "未触发"]
            )
    sections.append(
        "<section><h2>速度和资源</h2>"
        + table(
            ["模型", "静音整机CPU", "推理进程RSS"],
            [
                [
                    label,
                    f"{run['silent_stream']['machine_capacity_percent']:.2f}%",
                    f"{run['resident_idle']['rss_end_mib']:.1f} MiB",
                ]
                for label, run in [("V1", old), ("V2", new)]
            ],
        )
        + f"<p>新候选独立复测：静音 {resources['silence']['machine_capacity_percent']:.2f}%，人声 {resources['speech']['machine_capacity_percent']:.2f}%，人声阶段 RSS {resources['speech']['rss_end_mib']:.1f} MiB。各阶段60秒，六核整机能力为100%。不是完整助手的资源占用。</p>"
        + table(["模型", "文件", "文件开始至触发"], speed)
        + '<p class="note">录音开头包括等待和提示声音；没有人工标注唤醒词结束位置，因此这里不是词尾延迟。模型结构与约260ms右侧上下文保持不变。</p></section>'
    )
    sections.append(
        "<section><h2>结论与证据</h2><p><strong>"
        + verdict
        + '</strong>。V2 已正式部署，没有根据真人结果再调参数。新增句子和声学变体增加覆盖，但仍来自同一个合成器；实际改善必须通过新的真人验收确认。</p><p><a href="../state/fsmn-v2-20261013/summary.json">结果汇总</a> · <a href="../state/fsmn-v2-20261013/paired-trials.csv">逐条件对照 CSV</a> · <a href="../state/fsmn-v2-20261013/frozen-selection.json">冻结的选择记录</a> · <a href="../state/fsmn-v2-20261013/selected-c80.json">新候选完整测试</a> · <a href="../state/fsmn-v2-20261013/v1-c80.json">旧候选同条件重测</a> · <a href="../state/fsmn-v2-20261013/selected-resources.json">资源复测</a></p></section>'
    )
    title = "FSMN 第二轮定制 · 仅合成数据训练"
    doc = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + title
        + "</title><style>"
        + style
        + '</style><main><header>PERSONAL AGENT / 2026.10.13</header><div class="hero"><h1>'
        + title
        + '</h1><p class="lead">'
        + verdict
        + '</p><p>当前部署：FSMN V2 · 阈值 0.485 · 80ms · 单线程；旧 Zipformer 运行实现和部署模型已删除。三种本轮训练方案只按合成验证集选择，随后仅对选中的候选做真人评估。</p><a href="LIGHT_WAKE_COMPARISON.html">返回完整模型对比</a></div>'
        + "".join(sections)
        + "</main></html>"
    )
    (ROOT / "reports/FSMN_REFINEMENT.html").write_text(doc)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
