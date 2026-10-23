"""Explicit installed-host acceptance with synthetic input and the fixed real speaker.

Run the host with --no-mic for this test. This script does not restart it, change
bindings, capture room audio or execute device tasks. Supply known synthetic WAVs.
"""

import argparse
import asyncio
import json
from pathlib import Path
import time

from assistant_runtime.app.config import Config
from assistant_runtime.mcp.client import LocalRuntimeClient


async def verify(config, request, exit_audio, chat_audio):
    for path in (request, exit_audio, chat_audio):
        if path is not None and not path.is_file():
            raise ValueError("Missing synthetic WAV: " + str(path))
    client = LocalRuntimeClient(config.socket)
    initial = await client.status()
    if initial["microphone_enabled"] or initial["mode"] != "standby" or initial["task_running"]:
        raise RuntimeError("Acceptance requires an idle run --no-mic host; never mix live microphone audio")
    if initial["quiet_hours"]["active"]:
        raise RuntimeError("Quiet hours are active; do not bypass the quiet policy")
    evidence = {"started": time.time(), "voice_io": initial["voice_io"], "checks": {}}
    started = False

    async def wait(predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await client.status()
            if predicate(status):
                return status
            await asyncio.sleep(0.25)
        raise TimeoutError("Voice acceptance timed out; inspect current state before any retry")

    async def inject(path):
        nonlocal started
        started = True
        return await client.request({"command": "inject-wav", "path": str(path.resolve())})

    try:
        await inject(request)
        status = await wait(
            lambda s: s["written_audio_bytes"] > initial["written_audio_bytes"]
            and s["input_count"] > initial["input_count"]
        )
        status = await wait(lambda s: s["mode"] == "standby")
        assert not status["task_running"], "Synthetic request did not finish; do not replay it"
        evidence["checks"]["short_reply_then_standby"] = {
            "written_bytes": status["written_audio_bytes"] - initial["written_audio_bytes"],
            "inputs": status["input_count"] - initial["input_count"],
        }
        if chat_audio:
            await inject(chat_audio)
            await wait(lambda s: s["voice_dialogue"]["mode"] == "continuous")
            await asyncio.sleep(config.voice_follow_up_seconds + 2)
            status = await client.status()
            assert status["mode"] == "active", (
                "Explicit continuous conversation exited at short-window deadline"
            )
            evidence["checks"]["explicit_continuous"] = True
        # Exit during real output, then verify that late PCM cannot reopen playback.
        if (await client.status())["mode"] == "standby":
            await inject(request)  # same known harmless synthetic question primes the connection
        await client.request(
            {
                "command": "say",
                "text": "这是一段退出验收播报。收到退出指令后，我应立即停止，随后保持待机。" * 5,
            }
        )
        await wait(lambda s: s["speaking"])
        await inject(exit_audio)
        stopped = await wait(lambda s: s["mode"] == "standby", timeout=30)
        before = stopped["written_audio_bytes"]
        await asyncio.sleep(4)
        final = await client.status()
        assert final["mode"] == "standby" and not final["speaking"]
        assert final["written_audio_bytes"] == before, "Late speech resumed after exit"
        assert final["voice_io"] == initial["voice_io"], "Voice binding changed"
        evidence["checks"]["exit_and_late_audio"] = {
            "software_stop_seconds": final["software_stop_seconds"],
            "late_written_bytes": final["written_audio_bytes"] - before,
        }
        evidence["result"] = "passed"
    except Exception as exc:
        evidence["result"] = "failed"
        evidence["error_type"] = type(exc).__name__
        raise
    finally:
        if started:
            try:
                await client.request({"command": "sleep"})
            except Exception:
                evidence["cleanup"] = "unknown"
        path = config.state_dir / "acceptance" / "voice"
        path.mkdir(parents=True, exist_ok=True)
        file = path / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
        file.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(file)
    print(json.dumps(evidence["checks"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument("--request", type=Path, required=True, help="Known synthetic harmless question WAV")
    parser.add_argument(
        "--exit", dest="exit_audio", type=Path, required=True, help="Known synthetic 退下 WAV"
    )
    parser.add_argument("--chat", dest="chat_audio", type=Path, help="Optional synthetic 陪我聊聊 WAV")
    args = parser.parse_args()
    asyncio.run(verify(Config.load(Path(args.config)), args.request, args.exit_audio, args.chat_audio))
