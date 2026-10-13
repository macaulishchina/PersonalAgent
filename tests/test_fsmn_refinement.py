"""Protect the no-human-training and independent-split experiment constraints."""

import unittest

try:
    from scripts.prepare_fsmn_refinement import validate_records
except ImportError:
    validate_records = None


@unittest.skipIf(validate_records is None, "optional benchmark dependencies are not installed")
class RefinementDataTests(unittest.TestCase):
    def row(self, **values):
        return {
            "id": "a",
            "natural_human_recording": False,
            "split": "train",
            "speaker_id": 1,
            "sha256": "wave-a",
            **values,
        }

    def test_human_and_unverified_sources_are_rejected(self):
        for marker in (True, None):
            with self.assertRaises(ValueError):
                validate_records([self.row(natural_human_recording=marker)])

    def test_same_voice_or_waveform_cannot_cross_splits(self):
        for row in (
            self.row(id="b", split="test", sha256="wave-b"),
            self.row(id="b", split="test", speaker_id=2),
        ):
            with self.assertRaises(ValueError):
                validate_records([self.row(), row])
        validate_records([self.row(), self.row(id="b", split="test", speaker_id=2, sha256="wave-b")])


class FocusedCtcTests(unittest.TestCase):
    def test_background_collapse_preserves_repeated_wake_syllables(self):
        try:
            from scripts.finetune_fsmn_wake import focused_targets
        except ImportError:
            self.skipTest("optional training dependencies unavailable")
        self.assertEqual(focused_targets([8, 9, 2, 2, 7, 6], {2: 1}, 4).tolist(), [4, 1, 1, 4])

    def test_grouping_preserves_probability_mass_and_trainable_gradients(self):
        try:
            import torch
            from scripts.finetune_fsmn_wake import focused_log_probs
        except ImportError:
            self.skipTest("optional training dependencies unavailable")
        logits = torch.randn(1, 5, 7, requires_grad=True)
        grouped = focused_log_probs(logits.log_softmax(2), [2, 4, 5], [1, 3, 6])
        torch.testing.assert_close(grouped.exp().sum(2), torch.ones(1, 5))
        (-grouped[:, :, -1].mean()).backward()
        self.assertTrue(torch.isfinite(logits.grad).all())
        self.assertGreater(float(logits.grad.abs().sum()), 0)
