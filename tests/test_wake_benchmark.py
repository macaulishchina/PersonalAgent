"""Protect wake evaluation accounting, rather than mock model accuracy."""

import unittest

from scripts.benchmark_wake import match_events


class WakeAccountingTest(unittest.TestCase):
    def test_duplicate_hits_do_not_inflate_recall(self):
        targets = [{"start_s": 1, "end_s": 2}, {"start_s": 4, "end_s": 5}]
        matches, extra = match_events([1.8, 2.2], targets)
        self.assertEqual(matches, [1.8, None])
        self.assertEqual(extra, [2.2])

    def test_late_false_wake_does_not_rescue_a_missed_utterance(self):
        matches, extra = match_events([0.2, 4.0], [{"start_s": 1, "end_s": 2}])
        self.assertEqual(matches, [None])
        self.assertEqual(extra, [0.2, 4.0])

    def test_negatives_count_all_hits_and_early_keyword_detection_is_valid(self):
        self.assertEqual(match_events([0.8, 1.1], []), ([], [0.8, 1.1]))
        self.assertEqual(
            match_events([1.5], [{"start_s": 1.0, "end_s": 1.8}]),
            ([1.5], []),
        )
