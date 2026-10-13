"""Generate a complete local Markdown comparison from immutable model reports."""

import argparse
from collections import Counter
import json
from pathlib import Path


def count(result, predicate):
    trials = [trial for trial in result["trials"] if predicate(trial)]
    hits = sum(t["hits"] for t in trials)
    targets = sum(t["targets"] for t in trials)
    return f"{hits}/{targets} ({hits / targets:.1%})" if targets else "—"


def make_report(folder, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    summaries = json.loads((folder / "summary.json").read_text())
    results = [json.loads((folder / f"result-{item['model']}.json").read_text()) for item in summaries]
    if len({r["manifest_sha256"] for r in results}) != 1:
        raise ValueError("Cannot compare results produced from different corpora")
    manifest = json.loads((folder / "manifest.json").read_text())
    hardware = json.loads((folder / "hardware.json").read_text())
    groups = Counter(record["group"] for record in manifest["records"])
    by_model = {result["model"]: result for result in results}
    current = by_model["zh_en_c16_int8"]
    floating = by_model["zh_en_c16_fp32"]

    def digital_original(trial):
        return trial["group"] == "digital_positive" and trial["variant"] == "original"

    def room_original(trial):
        return trial["group"] == "fresh_room" and trial["variant"] == "original"

    def historical_a(trial):
        return trial["id"] == "1791734490-wake-direct"

    lines = [
        "# 本地唤醒模型对比测试报告",
        "",
        "测试日期：2026-10-12（Asia/Shanghai）。",
        "",
        "## 结论与建议",
        "",
        "本轮没有出现足以直接替换生产配置的明显赢家。下面的判断仅针对固定默认参数与本轮语料，不能替代真人使用验收。",
        "",
        f"- 当前 chunk-16/int8：数字正样本 {count(current, digital_original)}，客厅麦克风原始位置 {count(current, room_original)}。需要结合弱回放和相位结果判断可靠性。",
        f"- 浮点 chunk-16：数字正样本 {count(floating, digital_original)}，历史 A 相位从当前的 {count(current, historical_a)} 到 {count(floating, historical_a)}；客厅原始位置为 {count(floating, room_original)}。同时检查近音词误触发代价。",
        "- chunk-8：成功样本的估计检测等待更短，但本轮召回未改善，计算总 RTF 反而更高。低延迟不能作为高唤醒率的替代。",
        "- 中文 WenetSpeech 的命中和误触发均与当前模型有明显差异，具体计数见主结果表；未经各自阈值标定，不能仅按召回选型。",
        "- 长词“你好嘟嘟”在本轮各配置中普遍比短词更容易命中，但样本量小、不是用户真人录音，继续保留两词等效。",
        "",
        "优先补充用户本人近距离和日常距离的录音，控制输入链路后，再分别为两种词标定 boosting score 与 threshold；用留出的正/负样本验收漏检、误触发和延迟。若默认模型族仍不满足，再评估专用词训练。生产服务本轮保持原配置，恢复后继续使用。",
        "",
        "## 范围与结论边界",
        "",
        "本轮比较七种配置、两个模型家族。所有配置同时登记“嘟嘟”和“你好嘟嘟”，命中任一词都算同一次有效唤醒。固定 decoder score=1.0、threshold=0.25、max_active_paths=4、trailing_blanks=1、CPU 两线程；未给各模型单独调参。因此比较的是这些默认配置在本测试集的表现，不是各模型最佳可达到的性能。",
        "",
        "原始语料包含四种 Kokoro 合成音色（两女声、两男声）、0.8/1.0/1.2 三档语速、独立叫词、自然接问题和带停顿的问候。实际客厅回放使用摄像头原始麦克风和音箱监视器同时录音，PCM 输入振幅为 0.75/0.35；没有改变设备音量或给生产系统加增益。书房音箱未能连接，没有计入书房录音。真人样本为 0。",
        "",
        "所有波形保存在本地私有 state/，不写入 Git。合成音色不等同于四位真人；回放、监视器及压力变体共享来源，不可当作独立人群准确率。",
        "",
        "## 环境和可重复性",
        "",
        f"- CPU：{hardware['cpu']}",
        f"- 系统：{hardware['platform']}",
        f"- Python：{hardware['python'].split()[0]}",
        f"- sherpa-onnx：{results[0]['sherpa_onnx']}",
        "- 输入：16 kHz、单声道 PCM16、20 ms 帧；各模型独立进程、排除预热。",
        f"- 主语料 manifest SHA256：`{results[0]['manifest_sha256']}`",
        "",
        "WenetSpeech 的 epoch-12 和本机 epoch-99 文件逐一核对后完全一致，未作为两个独立模型计数。七种配置实际为：zh-en chunk-16/8 × int8/fp32，以及 WenetSpeech chunk-16 的 fp32、encoder/joiner int8（decoder fp32）、全 int8。",
        "",
        "## 语料构成",
        "",
        "| 分组 | 原始音频数 | 说明 |",
        "|---|---:|---|",
    ]
    descriptions = {
        "digital_positive": "本地多音色合成唤醒语音",
        "digital_negative": "普通讲话、近音词和较长讲话",
        "fresh_room": "本轮客厅音箱→摄像头原始麦克风",
        "sink_monitor": "本轮音箱输出监视器回录，未经过房间声学路径",
        "historical_room": "历史 A/B 原始麦克风录音",
        "continuous_negative": "约十分钟连续流，由负样本重新排列重复组成",
        "silence": "60 秒静音",
    }
    for name, number in groups.items():
        lines.append(f"| {name} | {number} | {descriptions.get(name, name)} |")
    lines += [
        "",
        f"主语料共 {len(manifest['records'])} 份；每模型 {len(results[0]['trials'])} 个条件，七配置总计 {sum(len(r['trials']) for r in results)} 次回放。",
        "客厅/历史正样本做 0–300 ms、步长 20 ms 的完整相位扫描。数字独立词样本另测 80/160/240 ms 偏移、0.25/0.1 振幅及 15/5 dB 白噪声；噪声随机种子跨模型相同。",
        "",
        "## 主要结果",
        "",
        "分子为有效命中次数，分母为目标次数。A/B 相位列包含原始位置和 15 个偏移位置。负样本列是误触发次数，不能读作成功率。",
        "",
        "| 配置 | 数字正样本 | 客厅 0.75 | 客厅 0.35 | A 相位 | B 相位 | 数字负样本误触发 | 连续负样本误触发 | 计算 P95 ms |",
        "|---|---|---|---|---|---|---:|---:|---:|",
    ]
    for result in results:
        gs = result["groups"]
        cells = [
            result["model"],
            count(result, lambda t: t["group"] == "digital_positive" and t["variant"] == "original"),
            count(
                result,
                lambda t: t["group"] == "fresh_room"
                and t["variant"] == "original"
                and t["playback_scale"] == 0.75,
            ),
            count(
                result,
                lambda t: t["group"] == "fresh_room"
                and t["variant"] == "original"
                and t["playback_scale"] == 0.35,
            ),
            count(result, lambda t: t["id"] == "1791734490-wake-direct"),
            count(result, lambda t: t["id"] == "1791734683-wake-aec"),
            str(gs["digital_negative/original"]["negative_false_hits"]),
            str(gs["continuous_negative/original"]["negative_false_hits"]),
            str(result["call_compute_ms"]["p95"]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "## 两个唤醒词分别统计",
        "",
        "| 配置 | 数字“嘟嘟” | 数字“你好嘟嘟” | 客厅“嘟嘟” | 客厅“你好嘟嘟” | 客厅完整相位扫描 |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        row = [result["model"]]
        for group in ("digital_positive", "fresh_room"):
            for phrase in ("嘟嘟", "你好嘟嘟"):
                row.append(
                    count(
                        result,
                        lambda t, g=group, p=phrase: t["group"] == g
                        and t["phrase"] == p
                        and t["variant"] == "original",
                    )
                )
        row.append(count(result, lambda t: t["group"] == "fresh_room"))
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        "## 音色、语速与输入路径",
        "",
        "下表音色栏包含各自的全部九条数字正样本；语速栏只看独立唤醒词，避免把接句风格差异混入语速比较。",
        "",
        "| 配置 | 女声 zf_001 | 女声 zf_026 | 男声 zm_009 | 男声 zm_052 | 慢速独立词 | 正常独立词 | 快速独立词 | 接问题 | 停顿问候 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        row = [result["model"]]
        for voice in ("zf_001", "zf_026", "zm_009", "zm_052"):
            row.append(
                count(
                    result,
                    lambda t, v=voice: t["group"] == "digital_positive"
                    and t["variant"] == "original"
                    and t["voice"] == v,
                )
            )
        record_by_id = {record["id"]: record for record in manifest["records"]}
        for speed in (0.8, 1.0, 1.2):
            row.append(
                count(
                    result,
                    lambda t, s=speed: t["group"] == "digital_positive"
                    and t["variant"] == "original"
                    and t["speed"] == s
                    and record_by_id[t["id"]]["style"] == "isolated",
                )
            )
        for style in ("inline", "paused_greeting"):
            row.append(
                count(
                    result,
                    lambda t, s=style: t["group"] == "digital_positive"
                    and t["variant"] == "original"
                    and record_by_id[t["id"]]["style"] == s,
                )
            )
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        "相同八条正常语速独立词的配对结果：",
        "",
        "| 配置 | 原始数字语音 | 监视器 0.75 | 麦克风 0.75 | 监视器 0.35 | 麦克风 0.35 |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        row = [
            result["model"],
            count(
                result,
                lambda t: t["group"] == "digital_positive"
                and t["variant"] == "original"
                and t["speed"] == 1.0
                and record_by_id[t["id"]]["style"] == "isolated",
            ),
        ]
        for level in (0.75, 0.35):
            for group in ("sink_monitor", "fresh_room"):
                row.append(
                    count(
                        result,
                        lambda t, scale=level, source_group=group: t["group"] == source_group
                        and t["variant"] == "original"
                        and t["playback_scale"] == scale,
                    )
                )
        lines.append("| " + " | ".join(row) + " |")
    integrity_path = folder / "capture-integrity.json"
    if integrity_path.exists():
        checks = json.loads(integrity_path.read_text())
        values = sorted(item["correlation"] for item in checks)
        lines += [
            "",
            f"24 条监视器录音与源波形做时移对齐后的归一化相关系数最小 {min(values):.4f}，中位数 {(values[11] + values[12]) / 2:.4f}；未发现削波。这支持回放源在监视器处被完整保留。监视器之后的蓝牙/扬声器、房间和麦克风路径仍可能改变信号；本实验不能进一步把损失归因于某一个环节。",
        ]
    cross_folder = folder / "cross-generator"
    if (cross_folder / "summary.json").exists():
        lines += [
            "",
            "## 历史 Edge 语音交叉检查",
            "",
            "补充旧 Edge 的独立短词、自然接问题短词和拼接长词，每条做 16 种相位，共每配置 48 条件、336 次回放。第三条长词是明确的拼接样本，不是自然真人发音。该检查不与主测试混算准确率。",
            "",
            "| 配置 | 短词 | 短词接问题 | 拼接长词 |",
            "|---|---|---|---|",
        ]
        for result in results:
            cross = json.loads((cross_folder / f"result-{result['model']}.json").read_text())
            lines.append(
                "| "
                + " | ".join(
                    [result["model"]]
                    + [
                        count(cross, lambda t, n=name: t["id"] == n)
                        for name in ("edge_short_only", "edge_short_inline", "edge_long_constructed")
                    ]
                )
                + " |"
            )
        lines += [
            "",
            "旧 Edge 数字语音基本都能通过，而新音色和实际麦克风录音更困难，说明对单一合成声线的通过不能代表普遍可靠性。主测试加交叉检查合计 **4550 次条件回放**。",
        ]
    lines += [
        "",
        "## 负样本暴露与误触发",
        "",
        "只将 targets=0 的额外命中计入负样本误触发。正样本中的多次或越界命中单独保留在 JSON，不混入负样本计数。长连续流包含重复语料；一次或零次误触发不足以证明小时级长期可靠性。这里测的是持续运行的 KWS 层，不模拟助手误唤醒后进入会话的时间，因此不等于产品每小时打扰次数。",
        "",
        "| 配置 | 数字讲话 秒/次 | 客厅负样本 秒/次 | 监视器负样本 秒/次 | 连续流 秒/次 | 静音 秒/次 |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        row = [result["model"]]
        for group in ("digital_negative", "fresh_room", "sink_monitor", "continuous_negative", "silence"):
            item = result["groups"][group + "/original"]
            row.append(f"{item['negative_seconds']:.2f} / {item['negative_false_hits']}")
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        "## 压力测试",
        "",
        "| 配置 | 数字相位 | 振幅 0.25 | 振幅 0.1 | 白噪声 15 dB | 白噪声 5 dB |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        row = [result["model"]]
        for condition in ("phase", "amplitude_0.25", "amplitude_0.1", "white_noise_15db", "white_noise_5db"):
            item = result["groups"]["digital_positive/" + condition]
            row.append(f"{item['hits']}/{item['targets']}")
        lines.append("| " + " | ".join(row) + " |")
    lines += [
        "",
        "## 计算开销与检测延迟",
        "",
        "计算时间仅为每帧 keyword() 的运行时间，包含仅缓存数据的调用。RTF 为计算墙钟时间/输入音频时间。内存为整个测试进程峰值（包括 Python、模型、音频和统计数组），不是模型单独占用。",
        "",
        "词尾延迟只对成功识别的独立数字词统计，词尾由能量估计，非人工标注。负值可表示提前识别或标注误差。漏检不计为零毫秒；不包含 Codex 连接、提示音或实际音箱停止延迟。客厅录音没有可靠词尾标注，不报告其精确词尾延迟。",
        "",
        "| 配置 | 初始化 ms | 计算 P50/P95/最大 ms | RTF | 进程峰值 MiB | 估计词尾延迟 P50/P95 ms（成功样本数） |",
        "|---|---:|---|---:|---:|---|",
    ]
    for result in results:
        d = result["call_compute_ms"]
        delay = result["groups"]["digital_positive/original"]["estimated_word_end_delay_ms"]
        lines.append(
            f"| {result['model']} | {result['initialization_ms']:.1f} | {d['p50']}/{d['p95']}/{d['max']} | {result['compute_rtf']:.4f} | {result['worker_peak_rss_mb']:.1f} | {delay.get('p50', '—')}/{delay.get('p95', '—')} ({delay['count']}) |"
        )
    lines += ["", "## 误触发实例", ""]
    by_id = {record["id"]: record for record in manifest["records"]}
    for result in results:
        false = [t for t in result["trials"] if not t["targets"] and t["extra_hits"]]
        lines.append(f"- **{result['model']}**：" + ("未检测到负样本触发。" if not false else ""))
        for trial in false[:8]:
            record = by_id[trial["id"]]
            lines.append(
                f"  - {record.get('text', trial['id'])}；{trial['group']}；音频位置 {trial['extra_hits']} s；输出词 {trial['detected_words']}。"
            )
    lines += [
        "",
        "## 复现与资产",
        "",
        "```sh",
        "uv sync --locked --extra voice --extra memory --extra dev",
        ".venv/bin/python -m scripts.wake_corpus prepare --config config.toml --folder state/wake-comparison --tts-dir models/test-tts/kokoro-int8-multi-lang-v1_1",
        ".venv/bin/python -m scripts.wake_corpus record --config config.toml --folder state/wake-comparison",
        ".venv/bin/python -m scripts.compare_wake_suite --config config.toml --folder state/wake-comparison --all",
        ".venv/bin/python -m scripts.report_wake_suite --folder state/wake-comparison --output reports/WAKE_MODEL_COMPARISON.md",
        ".venv/bin/python scripts/check.py",
        "```",
        "",
        "录音命令会播放样本、记录麦克风/监视器，要求助手空闲；通过 finally 恢复常驻服务。分析命令不播放、不录音、不连接云端。每个结果含实际权重哈希和清单哈希；完整逐条件结果位于 state/wake-comparison/result-*.json，summary.json 保存摘要。",
        "",
        "测试用 Kokoro 模型下载：`https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2`，下载后解压至 models/test-tts。TTS 只用于生成语料，不改变生产语音后端。",
        "",
        "## 未覆盖项",
        "",
        "- 用户真人录音：本轮用户暂不方便，没有以合成语音冒充真人。",
        "- 合成语音按生成文本标注，未进行真人逐条发音核听；语音生成器可能有其自身发音偏差。",
        "- 书房→客厅录音：设备未能连接，未记作模型失败。",
        "- 每个模型单独调参、专用词训练及较长独立真人负语料。",
        "- FSMN/其他工具包未运行：当前环境没有 Torch/FunASR，需要额外依赖及流式接口验证，本轮先覆盖可直接运行的七种配置。",
        "- 生产服务保持原先双词、chunk-16/int8 配置；未依据有限实验自动换模。",
        "",
        "参考：[Sherpa KWS 模型](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)、[检测参数](https://k2-fsa.github.io/sherpa/onnx/kws/index.html)、[Kokoro 中文音色](https://k2-fsa.github.io/sherpa/onnx/tts/all/Chinese-English/kokoro-multi-lang-v1_1.html)。",
        "",
    ]
    import csv

    with (folder / "trials.csv").open("w", newline="") as stream:
        fields = [
            "suite",
            "model",
            "id",
            "group",
            "phrase",
            "voice",
            "speed",
            "room",
            "playback_scale",
            "variant",
            "seconds",
            "targets",
            "hits",
            "negative_false_hits",
            "first_detection_audio_s",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        suites = [("main", result) for result in results]
        if (cross_folder / "summary.json").exists():
            suites += [
                ("cross_generator", json.loads((cross_folder / f"result-{result['model']}.json").read_text()))
                for result in results
            ]
        for suite, result in suites:
            for trial in result["trials"]:
                row = {key: trial.get(key) for key in fields}
                row.update(
                    suite=suite,
                    model=result["model"],
                    negative_false_hits=len(trial["extra_hits"]) if not trial["targets"] else 0,
                    first_detection_audio_s=trial["hits_s"][0] if trial["hits_s"] else None,
                )
                writer.writerow(row)
    lines += ["", "## 可试听的配对样本", ""]
    for phrase in ("嘟嘟", "你好嘟嘟"):
        source = next(
            r
            for r in manifest["records"]
            if r["group"] == "digital_positive"
            and r["voice"] == "zf_001"
            and r["speed"] == 1.0
            and r["style"] == "isolated"
            and r["phrase"] == phrase
        )
        lines.append(f"- {phrase}：[源数字语音]({source['path']})")
        for group, label in (("sink_monitor", "监视器"), ("fresh_room", "麦克风")):
            record = next(
                r
                for r in manifest["records"]
                if r["group"] == group and r["source_id"] == source["id"] and r["playback_scale"] == 0.75
            )
            lines.append(f"  - [{label}录音]({record['path']})")
    lines += [
        "",
        f"完整逐条件 CSV：`state/wake-comparison/trials.csv`（{sum(len(result['trials']) for _, result in suites)} 行）。录音及 JSON/CSV 为私有本地数据，未上传到远程仓库。",
        "",
    ]
    output.write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/WAKE_MODEL_COMPARISON.md"))
    args = parser.parse_args()
    make_report(args.folder, args.output)


if __name__ == "__main__":
    main()
