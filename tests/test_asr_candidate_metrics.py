import unittest

from scripts.benchmark_asr_candidates import distance, normalize, score


class CandidateMetricsTest(unittest.TestCase):
    def test_homophones_do_not_silently_count_as_wake_hits(self):
        positive = {"text": "你好嘟嘟。", "expected": [1]}
        result = score(positive, "你好，读读。", ["嘟嘟", "你好嘟嘟"])
        self.assertFalse(result["hit"])
        self.assertEqual(result["character_errors"], 2)

    def test_name_bias_false_alarm_is_counted_on_negative_audio(self):
        negative = {"text": "你好，读读这本书。", "expected": []}
        result = score(negative, "你好嘟嘟这本书。", ["嘟嘟", "你好嘟嘟"])
        self.assertTrue(result["false_wake"])
        self.assertFalse(result["hit"])

    def test_cer_keeps_insertions_and_numeric_spelling_differences(self):
        self.assertEqual(normalize("你好，嘟嘟！"), "你好嘟嘟")
        self.assertEqual(distance("嘟嘟", "嘟嘟你好"), 2)
        self.assertNotEqual(normalize("二加三"), normalize("2加3"))
