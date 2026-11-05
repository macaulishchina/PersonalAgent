"""Task model recommendations share file/schema validation and never dispatch inference."""

from pathlib import Path
import tempfile
import unittest

import tomlkit
from assistant_runtime.api.configuration import ConfigurationError, configuration_spec
from assistant_runtime.core.configuration.service import ConfigurationService
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.core.configuration.schema import resolve
from assistant_runtime.core.tasks.selection import ModelPolicy
from assistant_runtime.core.tasks.settings import TaskSettings


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / "config.toml"
        self.entry.write_text(
            'plugins=["models","ai_usage"]\ninclude=["models.toml","tasks.toml"]\n', encoding="utf-8"
        )
        (self.root / "models.toml").write_text(
            tomlkit.dumps(
                {
                    "model_api": {
                        "providers": {
                            "local": {
                                "protocol": "openai_responses",
                                "base_url": "http://127.0.0.1:1/v1",
                                "key_env": "TEST_KEY",
                                "enabled": False,
                            }
                        },
                        "entries": {
                            "one": {
                                "provider": "local",
                                "model": "synthetic",
                                "reasoning_efforts": ["low", "high"],
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.root / "tasks.toml").write_text(
            "# Operator-owned task settings\n[tasks]\ntimezone='Asia/Shanghai'\n", encoding="utf-8"
        )
        self.service = ConfigurationService(ManagementSettings(self.root, self.entry))

    def save(self, patches):
        return self.service.files.patch(self.service.files.snapshot().version, patches, actor="test")

    def row(self, **changes):
        return {
            "name": "诊断建议",
            "model": "one",
            "reasoning_effort": "high",
            "reason": "适合对比证据",
            "description": "根因未明时使用",
            **changes,
        }

    def test_defaults_have_no_hidden_recommendations_or_models(self):
        spec = configuration_spec(TaskSettings.parse.__code__.co_filename)
        defaults = resolve(spec, {})
        policy = ModelPolicy.parse(defaults["model_policy"])
        self.assertEqual(TaskSettings.parse({}).execution, {"model": "auto", "reasoning_effort": "auto"})
        self.assertEqual(policy.recommendations, {})
        self.assertEqual(policy.max_upgrades, defaults["model_policy"]["max_upgrades"])
        self.assertNotIn("profiles", spec.schema["properties"]["model_policy"]["properties"])

    def test_recommendations_save_to_owner_fragment_and_keep_comments(self):
        self.save(
            [
                {
                    "path": ["tasks", "model_policy", "recommendations", "diagnosis"],
                    "op": "set",
                    "value": self.row(),
                }
            ]
        )
        text = (self.root / "tasks.toml").read_text(encoding="utf-8")
        self.assertIn("# Operator-owned task settings", text)
        self.assertNotIn("model_policy", self.entry.read_text(encoding="utf-8"))
        self.assertEqual(
            tomlkit.parse(text)["tasks"]["model_policy"]["recommendations"]["diagnosis"]["name"], "诊断建议"
        )
        self.assertTrue(self.service.check()["valid"])

    def test_dormant_recommendations_reject_invalid_model_effort_and_name(self):
        for row in (self.row(model="missing"), self.row(reasoning_effort="medium"), self.row(name=" ")):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.save(
                    [
                        {
                            "path": ["tasks", "model_policy", "recommendations", "diagnosis"],
                            "op": "set",
                            "value": row,
                        }
                    ]
                )
        self.assertNotIn("model_policy", (self.root / "tasks.toml").read_text(encoding="utf-8"))

    def test_removing_referenced_model_requires_adjusting_recommendation(self):
        self.save(
            [
                {
                    "path": ["tasks", "model_policy", "recommendations", "diagnosis"],
                    "op": "set",
                    "value": self.row(),
                }
            ]
        )
        with self.assertRaises(ConfigurationError) as error:
            self.save([{"path": ["model_api", "entries", "one"], "op": "default"}])
        self.assertEqual(
            error.exception.path, ["tasks", "model_policy", "recommendations", "diagnosis", "model"]
        )
        self.save(
            [
                {"path": ["tasks", "model_policy", "recommendations", "diagnosis"], "op": "default"},
                {"path": ["model_api", "entries", "one"], "op": "default"},
            ]
        )
        self.assertTrue(self.service.check()["valid"])
