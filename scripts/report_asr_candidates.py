"""Offline visual ASR report with separate inference, residency and listening costs."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import statistics
from urllib.parse import quote


NAMES = {"kws": "当前关键词模型", "paraformer": "Paraformer 中文流式", "qwen": "Qwen3-ASR 0.6B"}
MODES = {
    "keyword": "原有关键词检测",
    "stream600": "600ms分块",
    "stream300": "300ms分块",
    "no_context": "无名称上下文",
    "name_context": "助手名称上下文",
}


def prepare(folder, corpus):
    manifest_file = corpus / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    source_hash = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    runs, profiles, rows = [], [], []
    for model, threads in [
        ("kws", 2),
        ("kws", 1),
        ("paraformer", 2),
        ("paraformer", 6),
        ("qwen", 2),
        ("qwen", 6),
    ]:
        r = json.loads((folder / f"{model}-t{threads}.json").read_text())
        if r["error"] or "worker_peak_rss_mib" not in r or r["corpus_sha256"] != source_hash:
            raise ValueError(f"Incomplete or mismatched result: {model} threads={threads}")
        if model != "kws" and r["actual_torch_threads"] != threads:
            raise ValueError("Recorded thread count differs from requested profile")
        runs.append({k: v for k, v in r.items() if k != "trials"})
        for mode in dict.fromkeys(t["mode"] for t in r["trials"]):
            trials = [t for t in r["trials"] if t["mode"] == mode]
            if len(trials) != 18 or sum(t["positive"] for t in trials) != 14:
                raise ValueError("Incomplete human cohort")
            wall = sum(t["metrics"]["wall_s"] for t in trials)
            cpu = sum(t["metrics"]["cpu_s"] for t in trials)
            seconds = sum(t["audio_seconds"] for t in trials)
            profile = {
                "id": f"{model}-t{threads}-{mode}",
                "model": model,
                "name": NAMES[model],
                "threads": threads,
                "mode": mode,
                "mode_name": MODES[mode],
                "hits": sum(t["hit"] for t in trials),
                "targets": 14,
                "negative_count": 4,
                "false_wakes": sum(t["false_wake"] for t in trials),
                "cer": sum(t["character_errors"] for t in trials) / sum(t["reference_chars"] for t in trials)
                if model != "kws"
                else None,
                "exact_transcripts": sum(t["exact_transcript"] for t in trials) if model != "kws" else None,
                "median_s": statistics.median(t["metrics"]["wall_s"] for t in trials),
                "max_s": max(t["metrics"]["wall_s"] for t in trials),
                "rtf": wall / seconds,
                "busy_process_cpu_percent": cpu / wall * 100,
                "busy_machine_percent": cpu / wall * 100 / r["logical_cores"],
                "cpu_budget_for_this_audio_percent": cpu / seconds * 100 / r["logical_cores"],
                "resident_mib": r["resident_idle"]["rss_end_mib"],
                "worker_peak_mib": r["worker_peak_rss_mib"],
                "load_s": r["load"]["wall_s"],
                "load_host_swap_out_bytes": r["load"]["host_swap_out_delta_bytes"],
                "resident_idle": r["resident_idle"],
                "listening": r.get("silent_stream", r.get("silent_vad_gated")),
                "listening_kind": "Silero VAD门控，静音期间Qwen调用0次"
                if model == "qwen"
                else "模型持续解码静音",
                "trials": [{k: v for k, v in t.items() if k != "events"} for t in trials],
            }
            profiles.append(profile)
            for t in trials:
                rows.append(
                    {
                        "profile": profile["id"],
                        "id": t["id"],
                        "reference": t["reference"],
                        "text": t["text"],
                        "hit": t["hit"],
                        "false_wake": t["false_wake"],
                        "character_errors": t["character_errors"] if model != "kws" else None,
                        "wall_s": t["metrics"]["wall_s"],
                        "cpu_s": t["metrics"]["cpu_s"],
                        "machine_cpu_percent": t["metrics"]["machine_capacity_percent"],
                        "rtf": t["rtf"],
                    }
                )
    with (folder / "trials.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    paced = []
    paced_file = folder / "paced/paraformer-t2.json"
    if paced_file.exists():
        extra = json.loads(paced_file.read_text())
        if (
            extra.get("error")
            or not extra.get("worker_peak_rss_mib")
            or extra["corpus_sha256"] != source_hash
        ):
            raise ValueError("Incomplete paced supplement")
        runs.append({k: v for k, v in extra.items() if k != "trials"})
    for run in runs:
        for case in run.get("paced_speech", []):
            chunk = case.get("chunk") or (10 if run["model"] == "paraformer" else None)
            key = (run["model"], run["threads"], chunk, case["id"])
            hit = next((e for e in case["events"] if e["wake"]), None)
            paced = [p for p in paced if (p["model"], p["threads"], p["chunk"], p["id"]) != key]
            paced.append(
                {
                    "model": run["model"],
                    "threads": run["threads"],
                    "chunk": chunk,
                    "id": case["id"],
                    "audio_end_s": hit["audio_end_s"] if hit else None,
                    "wall_end_s": hit["wall_end_s"] if hit else None,
                    "call_s": hit["call_s"] if hit else None,
                }
            )
    replicate = json.loads((folder / "replicate/kws-t2.json").read_text())
    if replicate.get("error") or not replicate.get("worker_peak_rss_mib"):
        raise ValueError("Incomplete idle replication")
    standby300 = json.loads((folder / "standby300/paraformer-t2.json").read_text())
    if (
        standby300.get("error")
        or not standby300.get("worker_peak_rss_mib")
        or standby300["corpus_sha256"] != source_hash
    ):
        raise ValueError("Incomplete 300ms listening measurement")
    for p in profiles:
        if p["model"] == "paraformer":
            p["listening_kind"] = "600ms分块持续解码静音"
        if p["id"] == "paraformer-t2-stream300":
            p["listening"] = standby300["silent_stream"]
            p["listening_kind"] = "300ms分块持续解码静音"
    # Keep detailed traces in private JSON; browser needs only measured summaries.
    for run in runs:
        for key in ["silent_stream", "silent_vad_gated"]:
            if key in run:
                run[key] = {k: v for k, v in run[key].items() if k != "events"}
        run.pop("paced_speech", None)
    for p in profiles:
        p["listening"] = {k: v for k, v in p["listening"].items() if k != "events"}
    data = {
        "profiles": profiles,
        "runs": runs,
        "paced": paced,
        "human_samples": 18,
        "decodes": len(rows),
        "corpus_sha256": source_hash,
        "logical_cores": 6,
        "kws_idle_replication": {k: v for k, v in replicate["silent_stream"].items() if k != "events"},
        "csv_href": f"{folder.as_posix()}/trials.csv",
        "audio": [
            {"id": r["id"], "text": r["text"], "path": Path(r["path"]).relative_to(Path.cwd()).as_posix()}
            for r in manifest["records"]
            if r["group"] in ("human_positive", "human_negative")
        ],
    }
    # The reference corpora remain byte-identical throughout model inference.
    for r in manifest["records"]:
        if hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() != r["sha256"]:
            raise ValueError("Recording no longer matches frozen corpus")
    return data


def markdown(data, output):
    lines = [
        "# CPU本地ASR候选真人测试报告",
        "",
        "测试日期：2026-10-13（Asia/Shanghai）。",
        "",
        "## 结论",
        "",
        "本批真人录音上，Paraformer中文流式命中13/14，Qwen3-ASR 0.6B不提供名称上下文时13/14，提供正常助手名称上下文时14/14。各配置四段真人负样本均未误唤醒。",
        "本机两线程比六线程更快且CPU更低。Paraformer是可继续验证的流式候选；Qwen的本地CPU逐段识别更准确，但内存和句后计算等待明显更高。没有替换部署。",
        "只有一位说话人、14段唤醒和四段负样本；本次成功不能代替独立验收或长期误触发评估。",
        "",
        "## 公平性与准确率口径",
        "",
        "原始PCM16录音16kHz单声道，用户已完整试听确认；未加增益、改速或降噪。每个配置处理相同18段音频，未给模型提供某段正确答案。",
        "唤醒规则严格匹配配置的嘟嘟/你好嘟嘟，去空格标点；读读、都都、多多、兜嘟不自动算成功。两个词等效。",
        "Qwen上下文固定为“助手的名字是嘟嘟。词汇：嘟嘟、你好嘟嘟。”，所有正、负样本使用相同上下文；它不是专用词训练。",
        "CER为按提示文字参考的字符编辑距离/参考字符数，去标点空格；数字拼写和口头语差异仍计入错误。录音未逐字人工重标注，所以CER是参考指标。关键词模型不输出一般转写，CER不适用。",
        "",
        "## 识别与文件处理速度",
        "",
        "|配置|命中|负样本误唤醒|CER|完整参考匹配|文件处理中位秒|最大秒|RTF|推理忙时全机CPU|按本批音频时长折算CPU预算|",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for p in data["profiles"]:
        cer = f"{p['cer']:.1%}" if p["cer"] is not None else "不适用"
        exact = f"{p['exact_transcripts']}/18" if p["exact_transcripts"] is not None else "不适用"
        lines.append(
            f"| {p['name']} / {p['threads']}线程 / {p['mode_name']} | {p['hits']}/14 | {p['false_wakes']}/4 | {cer} | {exact} | {p['median_s']:.3f} | {p['max_s']:.3f} | {p['rtf']:.3f} | {p['busy_machine_percent']:.1f}% | {p['cpu_budget_for_this_audio_percent']:.1f}% |"
        )
    lines += [
        "",
        "RTF=完整文件处理墙钟时间/原始文件时长，原文件含静音和提示音。上表是加速离线回放，不是现场首句出声速度。CPU进程100%代表一核，全机百分比除以六个逻辑核；未把主机其他进程的CPU当作模型消耗。折算预算仅对这批含静音的录音成立，不等于任何真实持续讲话负载。",
        "",
        "## 模型驻留、静音监听和加载",
        "",
        "|模型 / 线程|驻留MiB|全过程峰值MiB|驻留等待全机CPU|持续静音全机CPU|静音监听方式|模型构造加载秒|加载时系统换出MiB|",
        "|---|---:|---:|---:|---:|---|---:|---:|",
    ]
    for run in data["runs"][:6]:
        listen = run.get("silent_stream", run.get("silent_vad_gated"))
        lines.append(
            f"| {NAMES[run['model']]} / {run['threads']} | {run['resident_idle']['rss_end_mib']:.1f} | {run['worker_peak_rss_mib']:.1f} | {run['resident_idle']['machine_capacity_percent']:.2f}% | {listen['machine_capacity_percent']:.2f}% | {'Silero门控，ASR 0调用' if run['model'] == 'qwen' else '模型持续解码'} | {run['load']['wall_s']:.2f} | {run['load']['host_swap_out_delta_bytes'] / 1024**2:.1f} |"
        )
    lines += [
        "",
        "驻留等待=模型加载并预热后不送音频；不代表实际监听。Paraformer/KWS持续静音为按真实时间送音频解码；Qwen静音项是模型驻留并运行Silero VAD，ASR调用零次，不是Qwen原生连续流式。",
        "Paraformer两线程300ms持续静音另外测得全机CPU "
        + f"{next(p for p in data['profiles'] if p['id'] == 'paraformer-t2-stream300')['listening']['machine_capacity_percent']:.2f}%（30秒）"
        + "；上表Paraformer的模型行使用600ms静音测量。六线程300ms未单独测静音，不套用600ms结果。",
        "线程选项不是整个进程的CPU配额，框架可能存在额外工作线程。短时驻留/监听测量不构成小时级泄漏或长期稳定性验收。",
        "采样期间系统原助手继续运行。RSS包含整个测试进程及框架，线程采样本身有少量CPU开销；初始化值包含模型构造但不包含构造之前的公共导入，文件可能已在页缓存，不是严格磁盘冷启动。",
        "全机swap增量包括其他进程的换页，不能归因于模型独占swap；加载内存峰值对这台约8GB的主机有实际意义。",
        "",
        "## 按真实时间输入的首次唤醒输出",
        "",
        "|模型|线程|分块|录音|首次输出时已收到音频秒|墙钟秒|当前块计算秒|",
        "|---|---:|---|---|---:|---:|---:|",
    ]
    for p in data["paced"]:
        lines.append(
            f"| {NAMES[p['model']]} | {p['threads']} | {p['chunk'] * 60 if p['chunk'] else 20}ms | {p['id']} | {p['audio_end_s']:.2f} | {p['wall_end_s']:.3f} | {p['call_s']:.3f} |"
        )
    lines += [
        "",
        "时间从录音文件开始计，包含提示音、反应和静音。未标注词尾，不能读作词尾延迟。这里只是固定PCM的真实时钟模拟，不包含麦克风驱动、云端连接、音箱提示或完整对话。Qwen此次后端为逐段CPU识别，没有把离线结果伪装成原生流式。",
        "",
        "## 原模型静音CPU诊断",
        "",
        f"当前KWS两线程静音监听占全机CPU {data['profiles'][0]['listening']['machine_capacity_percent']:.2f}%，独立重跑为 {data['kws_idle_replication']['machine_capacity_percent']:.2f}%。",
        "一线程诊断中原始录音命中仍9/14，静音CPU明显下降。原部署未改；这是资源参数优化线索，不能称为提高了唤醒率。忙时线程等待可能是原因之一，本轮未追踪到原生库调用栈，不把推断作为已定位原因。",
        "",
        "## 逐条结果",
    ]
    for p in data["profiles"]:
        lines += [
            "",
            f"### {p['name']} / {p['threads']}线程 / {p['mode_name']}",
            "",
            "|录音|参考|输出|有效唤醒/误唤醒|处理秒|",
            "|---|---|---|---|---:|",
        ]
        for t in p["trials"]:
            outcome = (
                ("命中" if t["hit"] else "漏检")
                if t["positive"]
                else ("误唤醒" if t["false_wake"] else "正确未唤醒")
            )
            lines.append(f"|{t['id']}|{t['reference']}|{t['text']}|{outcome}|{t['metrics']['wall_s']:.3f}|")
    lines += [
        "",
        "## 环境、复现与边界",
        "",
        "i5-8400T，六核CPU，约7.6GiB物理内存。CPU float32 ASR，无GPU；当前KWS为INT8 encoder/joiner与FP32 decoder。",
        f"语料SHA256：`{data['corpus_sha256']}`。主配置评分{data['decodes']}次，来源仍为18段，重复与线程比较不算新独立样本。",
        "原始KWS、Paraformer、Qwen分别在独立进程加载，全部使用项目同一.venv；推理时离线，模型权重固定revision和SHA256。没有独立收费API。模型常驻仅指测试进程生命周期，完成后全部退出，未新增永久ASR服务。",
        "```sh",
        "uv sync --locked --extra voice --extra memory --extra dev --extra benchmark",
        ".venv/bin/python -m scripts.download_asr_candidates",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model paraformer",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model qwen",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model paraformer --threads 6 --idle-seconds 10",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model qwen --threads 6 --idle-seconds 10",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model kws",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model kws --threads 1 --idle-seconds 10",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model kws --threads 2 --idle-seconds 10 --output state/asr-candidates-20261013/replicate",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model paraformer --paced-only --idle-seconds 1 --output state/asr-candidates-20261013/paced",
        ".venv/bin/python -m scripts.benchmark_asr_candidates --model paraformer --paced-only --idle-seconds 1 --silent-seconds 30 --silent-chunk 5 --output state/asr-candidates-20261013/standby300",
        ".venv/bin/python -m scripts.report_asr_candidates",
        "```",
        "",
        "参考：[Paraformer模型](https://huggingface.co/funasr/paraformer-zh-streaming)、[Qwen原生CPU可加载检查点与名称上下文接口](https://huggingface.co/Qwen/Qwen3-ASR-0.6B-hf)、[Qwen官方流式接口边界](https://github.com/QwenLM/Qwen3-ASR)。",
        "真人录音、逐条JSON与CSV均只在本地state保存，模型在models保存；没有上传到远程仓库。",
    ]
    output.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=Path("state/asr-candidates-20261013"))
    parser.add_argument("--corpus", type=Path, default=Path("state/wake-human-comparison-20261013"))
    parser.add_argument("--output", type=Path, default=Path("reports/ASR_CANDIDATE_COMPARISON.html"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = prepare(args.folder, args.corpus)
    data["csv_href"] = quote(os.path.relpath(args.folder / "trials.csv", args.output.parent))
    for sample in data["audio"]:
        sample["path"] = quote(os.path.relpath(sample["path"], args.output.parent))
    templates = Path(__file__).with_name("templates")
    css = (templates / "wake-report.html").read_text().split("<style>", 1)[1].split("</style>", 1)[0]
    document = (templates / "asr-candidate-report.html").read_text().replace("__SHARED_CSS__", css)
    document = document.replace(
        "__REPORT_DATA__", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    )
    args.output.write_text(document)
    markdown(data, args.output.with_suffix(".md"))
    (args.folder / "summary.json").write_text(
        json.dumps(
            {k: v for k, v in data.items() if k not in ["runs", "audio"]}, ensure_ascii=False, indent=2
        )
    )
    print(
        f"Generated {args.output}, Markdown and {len(data['profiles'])} profiles / {data['decodes']} scored decodes"
    )


if __name__ == "__main__":
    main()
