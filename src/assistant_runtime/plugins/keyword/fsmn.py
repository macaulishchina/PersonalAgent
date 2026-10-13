"""Bounded streaming FSMN V2 keyword inference (16 kHz mono, no ASR or Torch)."""

import hashlib
import json
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import onnxruntime as ort

from .ctc import KwsCtcPrefixDecoder, Tensor

RATE = 16000
CHUNK = 1280  # 80 ms, the frozen streaming calibration point


class Fsmn:
    def __init__(self, folder: Path, wake_words):
        self.meta = json.loads((folder / "manifest.json").read_text())
        expected = {"id": "fsmn-v2", "format": 1, "sample_rate": RATE, "chunk_ms": 80, "threads": 1}
        if any(self.meta.get(k) != v for k, v in expected.items()):
            raise ValueError("Unsupported FSMN runtime model manifest")
        for name in ("model.onnx", "cmvn.npy", "tokens.txt", "lexicon.json"):
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != self.meta["files"][name]:
                raise ValueError(f"FSMN asset checksum mismatch: {name}")
        if not wake_words or set(wake_words) - set(self.meta["wake_words"]):
            raise ValueError("Wake phrases must be supported by the trained FSMN model: 嘟嘟 / 你好嘟嘟")
        tokens = (folder / "tokens.txt").read_text().splitlines()
        self.decoder = KwsCtcPrefixDecoder(
            None, ",".join(wake_words), tokens, json.loads((folder / "lexicon.json").read_text())
        )
        for entry in self.decoder.keywords_token.values():
            if not entry["token_id"] or any(tokens[i] in {"<unk>", "<blank>"} for i in entry["token_id"]):
                raise ValueError("Unsupported FSMN keyword token")
        self.threshold = float(self.meta["threshold"])
        if not 0 < self.threshold <= 1:
            raise ValueError("Invalid FSMN threshold")
        self.cmvn = np.load(folder / "cmvn.npy", allow_pickle=False)
        if self.cmvn.shape != (2, 400):
            raise ValueError("Invalid FSMN normalization shape")
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(
            str(folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self):
        options = knf.FbankOptions()
        options.frame_opts.samp_freq = RATE
        options.frame_opts.frame_length_ms = 25
        options.frame_opts.frame_shift_ms = 10
        options.frame_opts.dither = 0
        options.frame_opts.window_type = "hamming"
        options.frame_opts.snip_edges = True
        options.mel_opts.num_bins = 80
        options.energy_floor = 0
        self.fbank = knf.OnlineFbank(options)
        self.pending = np.empty(0, dtype=np.float32)
        self.cursor = self.popped = self.total_samples = 0
        self.probabilities = np.zeros((0, 2599), dtype=np.float32)
        self.last_hit = -10.0
        self.high = False
        self.last_score = 0.0

    def feed(self, samples):
        """Normalize caller chunking; emit (audio clock, phrase, score) events."""
        events = []
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not np.isfinite(samples).all():
            raise ValueError("Expected finite mono samples")
        # Never retain a caller's large input array in the permanent buffer.
        for start in range(0, len(samples), CHUNK):
            self.pending = np.concatenate((self.pending, samples[start : start + CHUNK]))
            if len(self.pending) < CHUNK:
                continue
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:].copy()
            event = self._step(chunk)
            if event:
                events.append(event)
        return events

    def _step(self, samples):
        self.total_samples += len(samples)
        self.fbank.accept_waveform(RATE, samples * 32768)
        lfr_ready = max(0, (self.fbank.num_frames_ready - 3) // 3 + 1)
        stable = lfr_ready - 8
        if stable <= self.cursor:
            return None
        start = max(0, self.cursor - 36)
        centers = np.arange(start, lfr_ready) * 3
        indexes = np.maximum(0, centers[:, None] + np.arange(-2, 3)[None])
        features = np.stack([self.fbank.get_frame(int(i)) for i in indexes.reshape(-1)]).reshape(-1, 400)
        features = ((features + self.cmvn[0]) * self.cmvn[1]).astype(np.float32)[None]
        logits = self.session.run(None, {"input": features})[0][0, self.cursor - start : stable - start]
        logits -= np.max(logits, axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        self.probabilities = np.concatenate((self.probabilities, probabilities))[-100:].copy()
        self.cursor = stable
        # Native fbank retains absolute indexes. Release only frames strictly
        # before the next inference's required left context; never reset at a
        # periodic boundary where a wake phrase might straddle the boundary.
        discard_before = max(0, (self.cursor - 36) * 3 - 2)
        self.fbank.pop(discard_before - self.popped)
        self.popped = discard_before
        hit, word, probability = self.decoder._decode_inside(Tensor(self.probabilities), None)
        self.last_score = float(probability) if hit else 0.0
        clock = self.total_samples / RATE
        if self.last_score >= self.threshold and not self.high and clock - self.last_hit >= 1:
            self.last_hit, self.high = clock, True
            return clock, word, self.last_score
        if self.last_score < self.threshold * 0.5:
            self.high = False
        return None

    def diagnostics(self):
        return {
            "model": self.meta["id"],
            "model_sha256": self.meta["files"]["model.onnx"],
            "threshold": self.threshold,
            "chunk_ms": 80,
            "threads": 1,
            "retained_feature_frames": self.fbank.num_frames_ready - self.popped,
            "retained_probability_frames": len(self.probabilities),
        }
