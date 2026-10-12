"""Offline comparison on the explicitly recorded room fixtures; no playback."""

from pathlib import Path
import json
import wave
import numpy as np
import sherpa_onnx

root = Path(__file__).resolve().parents[1]
files = [
    root / "state/acceptance/wake.wav",
    root / "state/r1791735024/room.wav",
    root / "state/r1791735477/room.wav",
]
files += list((root / "state/room-tests").glob("*-wake-*/raw.wav"))
# A no-keyword speech capture is a negative control.
files += [root / "state/room-tests/1791734693-speech-aec/raw.wav"]
results = []
for folder, epoch, chunk, quant in [
    ("wenetspeech-3.3M-2024-01-01", "99-avg-1", 16, False),
    ("zh-en-3M-2025-12-20", "13-avg-2", 16, True),
    ("zh-en-3M-2025-12-20", "13-avg-2", 8, True),
]:
    p = root / "models/kws" / ("sherpa-onnx-kws-zipformer-" + folder)
    ext = ".int8" if quant else ""
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(p / "tokens.txt"),
        encoder=str(p / f"encoder-epoch-{epoch}-chunk-{chunk}-left-64{ext}.onnx"),
        decoder=str(p / f"decoder-epoch-{epoch}-chunk-{chunk}-left-64.onnx"),
        joiner=str(p / f"joiner-epoch-{epoch}-chunk-{chunk}-left-64{ext}.onnx"),
        keywords_file=str(root / "state/keywords.txt"),
        num_threads=2,
    )
    report = {"model": folder, "chunk": chunk, "int8": quant, "results": []}
    for f in files:
        with wave.open(str(f)) as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
        stream = spotter.create_stream()
        found = []
        for i in range(0, len(a) - 320, 320):
            stream.accept_waveform(16000, a[i : i + 320])
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                if spotter.get_result(stream):
                    found.append(round(i / 16000, 2))
                    spotter.reset_stream(stream)
        report["results"].append({"file": str(f.relative_to(root)), "wakes": found})
        print(folder, chunk, f.parent.name, found, flush=True)
    results.append(report)
(root / "state/room-tests/kws-comparison.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
