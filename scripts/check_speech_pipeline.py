"""Offline recognition/rendering comparison. Never opens devices or calls cloud APIs."""

import argparse
import asyncio
import json
from pathlib import Path
import time
import wave

from assistant_runtime.api.speech import InputChunk
from assistant_runtime.plugins.speech.recognition import LocalRecognition
from assistant_runtime.plugins.speech.rendering import LocalSpeech


async def evaluate(args):
    recognition = LocalRecognition(args.asr_model)
    rows = []
    try:
        for path in sorted(args.recordings.glob("*.wav")):
            with wave.open(str(path)) as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
                    continue
                pcm = source.readframes(source.getnframes())
            if len(pcm) > 60 * 32000:
                continue
            await recognition.reset()
            start = time.monotonic()
            final, last = [], ""
            first_partial = None
            maximum = 0.0
            for offset in range(0, len(pcm) + 64000, 640):
                frame = pcm[offset : offset + 640].ljust(640, b"\0")
                before = time.monotonic()
                result = await recognition.accept(
                    InputChunk(offset // 640, offset / 32000, (offset + 640) / 32000, frame)
                )
                maximum = max(maximum, time.monotonic() - before)
                if result.text and first_partial is None:
                    first_partial = (offset + 640) / 32000
                if result.text:
                    last = result.text
                if result.final:
                    final.append({"text": result.text, "audio_at": (offset + 640) / 32000})
                    await recognition.reset()
                    last = ""
            elapsed = time.monotonic() - start
            row = {
                "file": path.name,
                "audio_seconds": len(pcm) / 32000,
                "compute_seconds": round(elapsed, 3),
                "rtf": round(elapsed / max(len(pcm) / 32000, 0.01), 3),
                "max_frame_compute_ms": round(maximum * 1000, 1),
                "first_partial_audio_at": first_partial,
                "segments": final,
                "unfinished": last,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await recognition.close()
    if args.tts_model:
        renderer = LocalSpeech(args.tts_model)
        try:
            for text in ("我在电视上找克拉克森的农场，找到就播放。", "没能确认电视的状态，我没有重复操作。"):
                start = time.monotonic()
                pcm = await renderer.render(text)
                row = {
                    "tts_text": text,
                    "render_seconds": round(time.monotonic() - start, 3),
                    "audio_seconds": len(pcm) / 48000,
                }
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        finally:
            await renderer.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        args.output.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asr-model", type=Path, required=True)
    parser.add_argument("--tts-model", type=Path)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    asyncio.run(evaluate(parser.parse_args()))
