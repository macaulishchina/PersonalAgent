"""Benchmark-only deterministic native Kaldi features, no Torch import."""

import numpy as np
import kaldi_native_fbank as knf
import wave

RATE = 16000


def load_wav(path):
    with wave.open(str(path)) as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"Expected 16kHz mono PCM16: {path}")
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32768


def new_fbank():
    options = knf.FbankOptions()
    options.frame_opts.samp_freq = RATE
    options.frame_opts.frame_length_ms = 25
    options.frame_opts.frame_shift_ms = 10
    options.frame_opts.dither = 0
    options.frame_opts.window_type = "hamming"
    options.frame_opts.snip_edges = True
    options.mel_opts.num_bins = 80
    options.energy_floor = 0
    return knf.OnlineFbank(options)


def features(samples):
    fbank = new_fbank()
    fbank.accept_waveform(RATE, np.asarray(samples, dtype=np.float32) * 32768)
    fbank.input_finished()
    return np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)]).astype(np.float32)
