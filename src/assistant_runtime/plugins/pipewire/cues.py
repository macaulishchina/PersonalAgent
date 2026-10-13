"""Authored local wake feedback; independent of speech playback volume."""

from functools import lru_cache

import numpy as np


@lru_cache(maxsize=4)
def wake_chime(rate=24000):
    """A one-second rising, rounded chime with a low resonant body."""
    duration = 1.04
    timeline = np.arange(round(rate * duration), dtype=np.float64) / rate
    sound = np.zeros(len(timeline), dtype=np.float64)
    # Lower-register open fifth/octave and a soft attack keep acknowledgement
    # distinct while giving it a fuller, less sparkling sound.
    for onset, frequency, strength, decay in (
        (0.0, 293.665, 1.0, 0.30),
        (0.17, 440.0, 0.88, 0.34),
        (0.34, 587.33, 0.64, 0.38),
    ):
        local = np.maximum(0, timeline - onset)
        envelope = (1 - np.exp(-local / 0.014)) * np.exp(-local / decay)
        partials = (
            np.sin(2 * np.pi * frequency * local)
            + 0.24 * np.sin(2 * np.pi * frequency * 2.003 * local)
            + 0.035 * np.sin(2 * np.pi * frequency * 3.011 * local)
        )
        sound += strength * envelope * partials
    body = (1 - np.exp(-timeline / 0.02)) * np.exp(-timeline / 0.48)
    sound += body * (
        0.40 * np.sin(2 * np.pi * 146.8325 * timeline) + 0.12 * np.sin(2 * np.pi * 293.665 * timeline)
    )
    # Light early reflections enrich the authored sound without a long echo.
    dry = sound.copy()
    for delay, strength in ((0.047, 0.16), (0.089, 0.07)):
        offset = round(rate * delay)
        sound[offset:] += strength * dry[:-offset]
    tail = np.clip((duration - timeline) / 0.16, 0, 1)
    sound *= np.sin(tail * np.pi / 2) ** 2
    sound[0] = sound[-1] = 0
    # Set the source sound's authored peak; speech PCM is never scaled here.
    sound *= 0.78 * 32767 / np.max(np.abs(sound))
    return np.rint(sound).astype("<i2").tobytes()
