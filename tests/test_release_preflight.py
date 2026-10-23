"""Release preparation validates isolated configuration without changing the instance."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant_runtime.config import Config
from scripts.check_pi_release import check
from test_pi_skeleton import configuration


class ReleasePreflightTest(unittest.TestCase):
    def config(self, root):
        return Config(
            root=root,
            model_api=configuration("http://127.0.0.1:1/v1"),
            main_model="model-a",
            main_reasoning_effort="low",
            memory_enabled=True,
            plugins=["ai_usage", "models", "pi", "voice_disabled", "tasks", "memory"],
            tasks={
                "model_policy": {
                    "profiles": {
                        name: {"model": "model-a", "reasoning_effort": "low"}
                        for name in ("fast", "balanced", "deep", "ultra")
                    }
                }
            },
        )

    def test_success_does_not_create_files_or_claim_live_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = self.config(root)
            with (
                patch("scripts.check_pi_release.credential", return_value="secret"),
                patch("scripts.check_pi_release.gateway_credential", return_value="secret"),
            ):
                result = check(config, native=False)
            self.assertEqual(result["state"], "passed")
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(result["real_provider_acceptance"], "separate_required")
            self.assertEqual(result["production_cutover"], "not_performed")

    def test_missing_credential_and_invalid_main_or_task_do_not_leak_secret_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.config(Path(folder))
            config.main_reasoning_effort = "ultra"
            config.tasks["model_policy"]["profiles"]["deep"]["model"] = "missing"
            with patch("scripts.check_pi_release.credential", side_effect=ValueError("SECRET")):
                result = check(config, native=False)
            failures = {r["check"] for r in result["checks"] if r["state"] == "failed"}
            self.assertEqual(failures, {"execution_binding", "task_profiles", "credential_references"})
            self.assertNotIn("SECRET", str(result))

    def test_missing_memory_binding_or_usage_plugin_blocks_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.config(Path(folder))
            config.model_api.pop("memory")
            config.plugins.remove("ai_usage")
            with (
                patch("scripts.check_pi_release.credential"),
                patch("scripts.check_pi_release.gateway_credential"),
            ):
                result = check(config, native=False)
            failures = {r["check"] for r in result["checks"] if r["state"] == "failed"}
            self.assertEqual(failures, {"execution_binding", "memory_binding"})
