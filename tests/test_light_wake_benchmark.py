"""Numerator, negative controls and deterministic frontend benchmark checks."""

import unittest

import numpy as np

try:
    from scripts.light_wake_features import features, new_fbank, RATE
    from scripts.benchmark_light_wake import match
    from scripts.light_wake_ctc import make_decoder, score
except ImportError:
    features = None


@unittest.skipIf(features is None, "optional benchmark dependencies are not installed")
class LightWakeBenchmarkTests(unittest.TestCase):
    def test_ctc_calibration_covers_scores_below_classifier_default(self):
        from scripts.calibrate_light_wake import choose

        selected, _ = choose(
            [
                {"positive": True, "peak_score": 0.41},
                {"positive": True, "peak_score": 0.9},
                {"positive": False, "peak_score": 0.264},
                {"positive": False, "peak_score": 0.0},
            ]
        )
        self.assertEqual(selected["threshold"], 0.265)
        self.assertEqual(selected["hits"], 2)
        self.assertEqual(selected["false_clips"], 0)
        with self.assertRaises(ValueError):
            choose([{"positive": True, "peak_score": 0.99}])

    def test_balanced_calibration_does_not_reward_rejecting_most_wakes(self):
        from scripts.calibrate_light_wake import choose

        samples = [{"positive": True, "peak_score": value} for value in (0.4, 0.8, 0.9)]
        samples += [{"positive": False, "peak_score": 0.85}]
        samples += [{"positive": False, "peak_score": 0.2}] * 99
        balanced, _ = choose(samples, "balanced")
        conservative, _ = choose(samples, "zero-false")
        self.assertEqual(balanced["hits"], 3)
        self.assertEqual(balanced["false_clips"], 1)
        self.assertEqual(conservative["hits"], 1)
        self.assertEqual(conservative["false_clips"], 0)

    def test_native_features_are_independent_of_capture_chunk(self):
        rng = np.random.default_rng(3)
        samples = rng.normal(0, 0.03, RATE).astype(np.float32)
        whole = features(samples)
        stream = new_fbank()
        for offset in range(0, len(samples), 320):
            stream.accept_waveform(RATE, samples[offset : offset + 320] * 32768)
        stream.input_finished()
        chunks = np.stack([stream.get_frame(i) for i in range(stream.num_frames_ready)])
        np.testing.assert_allclose(whole, chunks, atol=1e-6, rtol=0)

    def test_matching_requires_separate_hits_and_preserves_false_events(self):
        windows = [{"start_s": 1, "end_s": 2}, {"start_s": 4, "end_s": 5}]
        hits, extra = match([0.5, 1.7, 2.0, 4.6, 9.0], windows)
        self.assertEqual(hits, 2)
        self.assertEqual(extra, [0.5, 2.0, 9.0])
        self.assertEqual(match([1.7], windows), (1, []))
        self.assertEqual(match([1.0, 3.0], []), (0, [1.0, 3.0]))

    def test_repeated_keyword_needs_two_ctc_tokens(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "models/fsmn-kws-xiaoyun"
        if not root.exists():
            self.skipTest("upstream private test assets not downloaded")
        decoder, _ = make_decoder(root)
        index = decoder.keywords_token["嘟嘟"]["token_id"][0]
        width = len(decoder.token_list)
        one = np.zeros((3, width), dtype=np.float32)
        one[0, 0] = one[2, 0] = 1
        one[1, index] = 1
        self.assertEqual(score(decoder, one)[0], 0)
        two = np.concatenate([one, one[1:]])
        self.assertEqual(score(decoder, two), (1, "嘟嘟"))
        self.assertEqual(decoder.keywords_token["嘟嘟"]["token_id"], (index, index))


if __name__ == "__main__":
    unittest.main()
