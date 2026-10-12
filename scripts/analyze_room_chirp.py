"""Compare monitor/room timing of the explicit 8 second chirp fixture."""

import json
from pathlib import Path
import sys
import wave
import numpy as np


def load(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)


def analyze(root):
    monitor = load(root / "monitor.wav")
    room = load(root / "raw.wav")
    results = []
    for t in [2, 3, 4, 5, 6, 7]:
        at = t * 16000
        ref = monitor[at : at + 8000]
        data = room[at : at + 8000 + 16000 * 3]
        n = 1 << (len(data) + len(ref) - 2).bit_length()
        corr = np.fft.irfft(np.fft.rfft(data, n) * np.fft.rfft(ref[::-1], n), n)[len(ref) - 1 : len(data)]
        square = np.r_[0, np.cumsum(data * data)]
        norm = np.sqrt(np.maximum(1, (square[len(ref) :] - square[: -len(ref)]) * np.sum(ref * ref)))
        fit = np.abs(corr) / norm
        offset = int(np.argmax(fit))
        results.append(
            {"reference_s": t, "delay_ms": round(offset / 16, 1), "correlation": round(float(fit[offset]), 3)}
        )
    return {"folder": str(root), "matched_windows": results}


results = [analyze(Path(p)) for p in sys.argv[1:]]
print(json.dumps(results, indent=2))
