"""Offline PCM measurements. These do not certify intelligibility or acoustic SNR."""

import math

import numpy as np

RATE = 16000
FRAME_BYTES = 640


class SignalStats:
    def __init__(self):
        self.samples = self.clipped = self.zero_run = self.max_zero_run = 0
        self.total = self.squares = self.peak = 0.0

    def add(self, pcm):
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("Expected whole PCM16 samples")
        values = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        absolute = np.abs(values)
        self.samples += len(values)
        self.total += float(values.sum())
        self.squares += float(values @ values)
        self.peak = max(self.peak, float(absolute.max()))
        self.clipped += int(np.count_nonzero(absolute >= 32767))
        zero = values == 0
        edges = np.diff(np.r_[False, zero, False].astype(np.int8))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        if len(starts):
            lengths = ends - starts
            if starts[0] == 0:
                lengths[0] += self.zero_run
            self.max_zero_run = max(self.max_zero_run, int(lengths.max()))
        self.zero_run = (
            self.zero_run + len(values) if zero.all() else (len(values) - int(starts[-1]) if zero[-1] else 0)
        )

    def report(self):
        def db(power):
            return round(10 * math.log10(power), 3) if power > 0 else None

        return {
            "samples": self.samples,
            "rms_dbfs": db(self.squares / self.samples / 32768**2) if self.samples else None,
            "peak_dbfs": db((self.peak / 32768) ** 2),
            "clipped_sample_percent": round(100 * self.clipped / self.samples, 5) if self.samples else None,
            "dc_offset_percent_fs": round(100 * self.total / self.samples / 32768, 4)
            if self.samples
            else None,
            "longest_zero_run_ms": round(self.max_zero_run / RATE * 1000, 3),
        }


def checks(measurement):
    signal, stream = measurement["signal"], measurement["stream"]
    findings = []
    if stream["longest_delivery_gap_ms"] > 200:
        findings.append("采集交付空档超过200毫秒；需核对缓冲和链路，不能直接认定蓝牙丢包。")
    if not 0.9 <= stream["elapsed_seconds"] / measurement["requested_seconds"] <= 1.1:
        findings.append("采样时长与实际交付时间差异超过10%，可能存在缓冲积压或断流。")
    if signal["clipped_sample_percent"] > 0.1:
        findings.append("超过0.1%的采样触及满量程，存在削波风险。")
    if signal["longest_zero_run_ms"] > 200:
        findings.append("出现超过200毫秒的数字零；可能是静音或设备门控，需要回听确认。")
    if abs(signal["dc_offset_percent_fs"]) > 1:
        findings.append("直流偏移超过满量程的1%。")
    return findings


def noise_comparison(phases):
    comparisons = []
    for quiet in phases:
        if quiet["environment"] != "quiet" or quiet["kind"] != "background":
            continue
        noisy = next(
            (
                p
                for p in phases
                if p["environment"] == "noise"
                and p["position"] == quiet["position"]
                and p["kind"] == "background"
            ),
            None,
        )
        if noisy is None:
            continue
        a, b = (p["measurement"]["signal"]["rms_dbfs"] for p in (quiet, noisy))
        delta = round(b - a, 3) if a is not None and b is not None else None
        comparisons.append(
            {
                "position": quiet["position"],
                "background_increase_db": delta,
                "status": "observed"
                if delta is not None and delta >= 3 and not quiet["findings"] and not noisy["findings"]
                else "unconfirmed",
            }
        )
    return comparisons
