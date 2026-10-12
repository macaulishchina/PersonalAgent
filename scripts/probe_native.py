"""Exercise the actual native audio channel using an existing test WAV, never the mic."""
import argparse
import asyncio
import audioop
import base64
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dell_voice.rpc import CodexClient


async def probe(args):
    client = CodexClient(cwd=str(Path(__file__).resolve().parents[1]))
    output = bytearray()
    result = {"version": args.version, "audio_input_sent": False, "audio_output_bytes": 0, "errors": [], "events": {}}
    started = time.monotonic()
    thread = None
    sender = None
    async def feed():
        with wave.open(args.wav, "rb") as f:
            rate, width, channels = f.getframerate(), f.getsampwidth(), f.getnchannels()
            pcm = f.readframes(f.getnframes())
        if width != 2:
            pcm = audioop.lin2lin(pcm, width, 2)
        if channels == 2:
            pcm = audioop.tomono(pcm, 2, .5, .5)
        pcm = audioop.ratecv(pcm, 2, 1, rate, 24000, None)[0] + bytes(24000 * 2 * 3)
        for offset in range(0, len(pcm), 2400):
            part = pcm[offset:offset+2400]
            await client.request("thread/realtime/appendAudio", {"threadId": thread, "audio": {"data": base64.b64encode(part).decode(), "sampleRate": 24000, "numChannels": 1}}, timeout=10)
            await asyncio.sleep(.05)
        result["audio_input_sent"] = True
    try:
        await client.start()
        r = await client.request("thread/start", {"cwd": str(Path(__file__).resolve().parents[1]), "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "never"})
        thread = r["thread"]["id"]
        await client.request("thread/realtime/start", {
            "threadId": thread, "version": args.version,
            "transport": {"type": "websocket"}, "outputModality": "audio",
            "prompt": "这是一个音频联通测试。听到测试语音后，请简短地用中文说：音频测试成功。无需执行任何任务。",
        })
        print("native session request accepted", flush=True)
        sender = asyncio.create_task(feed())
        until = time.monotonic() + 40
        while time.monotonic() < until:
            try:
                event = await asyncio.wait_for(client.events.get(), 3)
            except TimeoutError:
                if result["audio_input_sent"] and len(output) > 1000:
                    break
                continue
            method, p = event.get("method", ""), event.get("params", {})
            result["events"][method] = result["events"].get(method, 0) + 1
            if method == "thread/realtime/outputAudio/delta":
                a = p["audio"]
                result["sample_rate"] = a["sampleRate"]
                result["channels"] = a["numChannels"]
                output.extend(base64.b64decode(a["data"]))
            elif method == "thread/realtime/error":
                result["errors"].append(p.get("message", "unknown error"))
                break
            elif method == "thread/realtime/closed":
                result["close_reason"] = p.get("reason")
                break
            elif "transcript" in method.lower():
                print(method, json.dumps(p, ensure_ascii=False)[:500], flush=True)
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        if sender:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if thread:
            try:
                await client.request("thread/realtime/stop", {"threadId": thread}, timeout=3)
            except Exception:
                pass
        await client.close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    result["audio_output_bytes"] = len(output)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if output:
        with wave.open(str(out.with_suffix(".wav")), "wb") as f:
            f.setnchannels(result["channels"])
            f.setsampwidth(2)
            f.setframerate(result["sample_rate"])
            f.writeframes(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v3")
    parser.add_argument("--wav", default="/home/macaulish/workspace/homepod-assistant/test.wav")
    parser.add_argument("--output", default="/tmp/dell-native-probe")
    asyncio.run(probe(parser.parse_args()))
