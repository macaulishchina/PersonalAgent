"""Standalone microphone benchmark; stop the assistant before running --guided."""

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys

from assistant_runtime.config import Config
from scripts.audio_check.capture import Capture, duration, endpoint_fingerprint, require_stopped
from scripts.audio_check.guide import CUES, POSITIONS, READING, VOICE_MODEL, GuidePlayer
from scripts.audio_check.metrics import noise_comparison
from scripts.audio_check.prepare import prepare_prompts
from scripts.audio_check.protocol import Protocol


def save(report, output):
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")


def summary(report):
    lines = ["流程：" + ("完成" if report["completed"] else "中止") + "；完成不代表质量合格。"]
    for row in report["phases"]:
        if row["kind"] == "background" and not row.get("offline_keyword_events"):
            continue
        label = POSITIONS.get(row["position"], row["position"])
        environment = {"quiet": "安静", "noise": "日常背景声", "observe": "观察"}[row["environment"]]
        findings = "；".join(row["findings"]) or "采集统计未见明显异常"
        hits = row.get("offline_keyword_events")
        wake = ""
        if hits is not None:
            wake = (
                ("；本地唤醒词命中" if hits else "；本地唤醒词未命中")
                if row["kind"] == "wake_reading"
                else ("；发现误唤醒候选" if hits else "；未发现误唤醒候选")
            )
        kind = {
            "observe": "链路观察",
            "background": "背景",
            "reading": "普通朗读",
            "wake_reading": "唤醒和首句",
        }[row["kind"]]
        lines.append(f"{environment} / {label} / {kind}：{findings}{wake}。")
    for item in report.get("noise_comparison", []):
        label = POSITIONS[item["position"]]
        status = "已观测到噪声增量" if item["status"] == "observed" else "噪声对比尚未确认"
        lines.append(f"{label}背景增量：{item['background_increase_db']} dB，{status}。")
    if report.get("failure"):
        lines.append("中止原因：" + report["failure"])
    lines.append(
        "服务没有自动停启；没有测试原生语音连接。可懂度和唤醒后首句需回听确认，小样本不代表长期误唤醒率。"
    )
    return "\n".join(lines)


async def run(args):
    config = Config.load(args.config)
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.text_only and not args.guided:
        raise ValueError("--text-only 需要 --guided")
    if args.output and args.output.exists():
        raise ValueError("结果文件已存在，请使用新文件名。")
    descriptor = os.open(
        config.state_dir / "audio-input-check.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    capture = player = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("本实例已有测试脚本运行，不会打断它。") from exc
        if args.prepare_guide:
            folder = await prepare_prompts(config.state_dir, config.models / "test-tts" / VOICE_MODEL)
            print("固定中文提示音已准备：" + str(folder), file=sys.stderr)
            if not args.guided:
                return
        await require_stopped(config)
        detector = None
        if args.guided:
            from assistant_runtime.assistant.commands import normalize
            from assistant_runtime.plugins.keyword.fsmn import Fsmn

            if any(
                normalize(word) in normalize(text)
                for word in config.wake_words
                for text in [*CUES.values(), READING]
            ):
                raise ValueError("固定提示含配置的唤醒词，请先调整提示文案，避免污染测量。")
            detector = await asyncio.to_thread(Fsmn, config.models / "fsmn-v2", config.wake_words)
            if not args.text_only:
                await prepare_prompts(config.state_dir, config.models / "test-tts" / VOICE_MODEL)
                player = GuidePlayer(config)
        folder = (
            config.state_dir
            / "acceptance/audio-input"
            / datetime.now(timezone.utc).strftime("standalone-%Y%m%dT%H%M%S%fZ")
        )
        folder.mkdir(parents=True, mode=0o700)
        output = args.output or folder / "result.json"
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = {
            "schema_version": 2,
            "completed": False,
            "service_at_start": "inactive",
            "service_at_end": "not_checked",
            "auto_restore": False,
            "audio_retained": args.save_audio,
            "voice_io": {"input_device": config.input_device, "output_device": config.default_output},
            "endpoint_fingerprint": endpoint_fingerprint(config),
            "wake_words": config.wake_words,
            "positions": POSITIONS if args.guided else {},
            "utterances_per_trial": 1,
            "wake_test": "offline_detector_only" if args.guided else "not_tested",
            "native_voice_acceptance": "not_tested_service_stopped",
            "speech_intelligibility": "manual_review_required_no_asr",
            "conditions": "operator_followed_instructions_not_sensor_verified",
            "phases": [],
        }
        failure = None
        try:
            capture = Capture(config)
            await capture.start()
            protocol = Protocol(capture, player, report, folder, detector, save_audio=args.save_audio)
            print("独立测试：助手已停止，脚本不会自动恢复服务。", file=sys.stderr, flush=True)
            if args.guided:
                print(
                    "每句只说一遍。唤醒测试句：" + config.wake_words[0] + "，" + READING,
                    file=sys.stderr,
                    flush=True,
                )
                await protocol.run()
            else:
                await protocol.measure("observe", "configured_position", "observe", args.seconds)
            await require_stopped(config)
            report["service_at_end"] = "inactive"
            report["completed"] = True
        except (asyncio.CancelledError, RuntimeError, OSError, ValueError, EOFError, TimeoutError) as exc:
            failure = exc
            report["failure"] = str(exc) or type(exc).__name__
        finally:
            results = await asyncio.gather(
                *(item.close() for item in (capture, player) if item is not None), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    report.update(completed=False, failure="测试资源释放失败：" + str(result))
                    failure = failure or result
            player = None
            report["noise_comparison"] = noise_comparison(report["phases"])
            save(report, output)
            print("检测结果已保存：" + str(output), file=sys.stderr, flush=True)
            print(json.dumps(report, ensure_ascii=False) if args.json else summary(report), flush=True)
        if failure is not None:
            raise failure
    finally:
        try:
            if player is not None:
                await player.close()
        finally:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--guided", action="store_true", help="四个房间，各做安静与日常背景声对照")
    parser.add_argument("--prepare-guide", action="store_true", help="只准备固定本地提示音，不采音、不播放")
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--save-audio", action="store_true", help="明确保存私有WAV，供回听可懂度和首句")
    parser.add_argument("--output", type=Path, help="新JSON文件，不覆盖")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        args.seconds = duration(args.seconds)
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("检测已取消。助手服务不会自动恢复。", file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, RuntimeError, ValueError, EOFError, TimeoutError) as exc:
        print("检测失败：" + (str(exc) or type(exc).__name__), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
