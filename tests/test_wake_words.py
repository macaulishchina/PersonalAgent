import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from assistant_runtime.assistant.commands import parse_command
from assistant_runtime.config import Config
from assistant_runtime.plugins.keyword.recognition import Recognizer


class WakeWordsTest(unittest.TestCase):
    def test_config_loads_equivalent_phrases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text('profile="headless"\nwake_words=["嘟嘟", " 你好嘟嘟 "]\n')
            config = Config.load(path)
            self.assertEqual(config.wake_words, ["嘟嘟", "你好嘟嘟"])

    def test_invalid_phrase_lists_fail_before_model_startup(self):
        for value in [[], "嘟嘟", [""], [" "], ["嘟嘟", None], ["嘟嘟", " 嘟嘟 "]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(wake_words=value)

    def test_both_prefixes_preserve_identical_control_intents(self):
        for phrase in ["嘟嘟", "你好嘟嘟"]:
            for text, expected in [("停一下", "stop"), ("取消任务", "cancel"), ("结束对话", "sleep")]:
                with self.subTest(phrase=phrase, text=text):
                    command = parse_command(phrase + "，" + text, wake_words=["嘟嘟", "你好嘟嘟"])
                    self.assertEqual(command.kind, expected)
            self.assertIsNone(
                parse_command("解释一下" + phrase + "停一下是什么意思", wake_words=["嘟嘟", "你好嘟嘟"])
            )

    def test_longest_prefix_is_removed_when_phrases_overlap(self):
        command = parse_command("你好嘟嘟，停一下", wake_words=["你好", "你好嘟嘟"])
        self.assertEqual(command.kind, "stop")

    def test_configured_phrases_reach_only_fsmn(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(root=Path(temporary), wake_words=["嘟嘟", "你好嘟嘟"])
            with (
                patch("assistant_runtime.plugins.keyword.recognition.Fsmn") as detector,
                patch("sherpa_onnx.VoiceActivityDetector"),
            ):
                recognizer = Recognizer(config)
                detector.assert_called_once_with(config.models / "fsmn-v2", config.wake_words)
                detector.return_value.feed.return_value = [(1.0, "你好嘟嘟", 0.9)]
                self.assertEqual(recognizer.keyword([0.0]), "你好嘟嘟")
                recognizer.reset_keyword()
                recognizer.keyword([0.0])
                detector.return_value.reset.assert_called_once()

    def test_sleep_invalidates_inflight_detection(self):
        config = Config()
        with (
            patch("assistant_runtime.plugins.keyword.recognition.Fsmn") as detector,
            patch("sherpa_onnx.VoiceActivityDetector"),
        ):
            recognizer = Recognizer(config)

            def finish_after_reset(samples):
                recognizer.reset_keyword()
                return [(1.0, "嘟嘟", 0.9)]

            detector.return_value.feed.side_effect = finish_after_reset
            self.assertIsNone(recognizer.keyword([0.0]))


class AsyncWakeTest(unittest.IsolatedAsyncioTestCase):
    async def test_reset_after_worker_finishes_still_invalidates_result(self):
        with (
            patch("assistant_runtime.plugins.keyword.recognition.Fsmn"),
            patch("sherpa_onnx.VoiceActivityDetector"),
        ):
            recognizer = Recognizer(Config())

            async def completion(*args):
                recognizer.reset_keyword()
                return "嘟嘟"

            with patch("assistant_runtime.plugins.keyword.recognition.asyncio.to_thread", completion):
                self.assertIsNone(await recognizer.detect([0.0]))
