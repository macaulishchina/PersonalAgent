"""Fixed Mandarin neural prompts, synthesized locally in an owned worker."""

from assistant_runtime.api.system import system

import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
import wave

from .guide import CUES, RATE, VOICE_ID, VOICE_SPEED, load_prompt, prompt_folder


async def command(arguments, *, timeout=30, env=None):
    process = await system.spawn(
        *arguments,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise RuntimeError(
                f"Voice preparation failed ({Path(arguments[0]).name}): {error.decode(errors='replace')[-1200:]}"
            )
        return output
    finally:
        await system.stop_process(process, timeout=2)


async def prepare_prompts(state_dir, model_dir):
    destination = prompt_folder(state_dir)
    if destination.exists():
        for cue in CUES:
            load_prompt(state_dir, cue)
        return destination
    if not (model_dir / "model.int8.onnx").is_file():
        raise RuntimeError("缺少本地中文神经语音模型：" + str(model_dir))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="building-") as temporary:
        staged = Path(temporary) / "prompts"
        staged.mkdir(mode=0o700)
        await command(
            [sys.executable, "-m", __name__, "--model-dir", str(model_dir), "--output", str(staged)],
            timeout=300,
        )
        for cue in CUES:
            load_prompt(state_dir, cue, folder=staged)
        staged.rename(destination)
    for cue in CUES:
        load_prompt(state_dir, cue)
    return destination


def synthesize(model_dir, output):
    import numpy as np
    import sherpa_onnx

    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(model_dir / "model.int8.onnx"),
                voices=str(model_dir / "voices.bin"),
                tokens=str(model_dir / "tokens.txt"),
                data_dir=str(model_dir / "espeak-ng-data"),
                lexicon=str(model_dir / "lexicon-zh.txt") + "," + str(model_dir / "lexicon-us-en.txt"),
                lang="zh",
            ),
            num_threads=2,
        ),
        max_num_sentences=1,
    )
    if not config.validate():
        raise ValueError("中文神经语音模型文件不完整。")
    engine = sherpa_onnx.OfflineTts(config)
    for cue, text in CUES.items():
        audio = engine.generate(text, sid=VOICE_ID, speed=VOICE_SPEED)
        samples = np.asarray(audio.samples, dtype=np.float32)
        if (
            audio.sample_rate != RATE
            or not 0 < len(samples) <= RATE * 25
            or not np.isfinite(samples).all()
            or np.max(np.abs(samples)) > 1
        ):
            raise ValueError("Invalid neural prompt samples: " + cue)
        path = output / (cue + ".wav")
        with wave.open(str(path), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(RATE)
            file.writeframes((samples * 32767).astype("<i2").tobytes())
        system.private(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    synthesize(options.model_dir, options.output)
