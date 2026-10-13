import unittest

import numpy as np

from scripts.compare_wake_suite import summarize, trial_variants
from scripts.wake_corpus import bounds


class WakeSuiteTest(unittest.TestCase):
    def test_positive_extra_hits_do_not_inflate_negative_false_alarm_rate(self):
        def trial(targets, hits, extra, seconds, phrase=None):
            return {
                "variant": "original",
                "group": "fresh_room",
                "targets": targets,
                "hits": hits,
                "extra_hits": extra,
                "seconds": seconds,
                "phrase": phrase,
                "estimated_word_end_delay_ms": [],
            }

        group = summarize([trial(1, 1, [1.2], 3, "嘟嘟"), trial(0, 0, [2], 10)])["fresh_room/original"]
        self.assertEqual(group["recall"], 1)
        self.assertEqual(group["extra"], 2)
        self.assertEqual(group["negative_false_hits"], 1)
        self.assertEqual(group["negative_seconds"], 10)

    def test_signal_bounds_exclude_padding_and_reject_empty_speech(self):
        audio = np.concatenate([np.zeros(3200), np.full(1600, 0.1), np.zeros(3200)])
        self.assertEqual(bounds(audio), (0.2, 0.3))
        with self.assertRaises(ValueError):
            bounds(np.zeros(16000))

    def test_room_phase_scan_spans_one_full_chunk_period(self):
        record = {"expected": [1], "group": "fresh_room"}
        trials = list(trial_variants(record))
        self.assertEqual(len(trials), 16)
        self.assertEqual([round(t[2] * 1000) for t in trials], list(range(0, 320, 20)))

    def test_human_positive_has_full_phase_and_stress_without_duplicates(self):
        record = {"expected": [1], "group": "human_positive"}
        trials = list(trial_variants(record))
        self.assertEqual(len(trials), 20)
        self.assertEqual(len({t[0] for t in trials}), 20)
        self.assertEqual([round(t[2] * 1000) for t in trials[:16]], list(range(0, 320, 20)))
        self.assertEqual(
            [t[0] for t in trials[16:]],
            ["amplitude_0.25", "amplitude_0.1", "white_noise_15db", "white_noise_5db"],
        )

    def test_human_negative_and_continuous_streams_do_not_become_positives(self):
        for group in ("human_negative", "human_continuous_negative", "human_continuous_mixed"):
            self.assertEqual(
                list(trial_variants({"expected": [], "group": group})), [("original", 1.0, 0.0, None)]
            )
