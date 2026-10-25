"""Offline metadata never grants acceptance; explicit selections survive route changes safely."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from assistant_runtime.core.models.capabilities import facts, profile
from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort
from assistant_runtime.core.models.gateway_transport import prepare_request
from assistant_runtime.core.models.catalog import models


class CapabilityTest(unittest.TestCase):
    def spec(self):
        return {
            "enabled": True,
            "providers": {
                "p": {
                    "protocol": "openai_responses",
                    "base_url": "https://relay.example/v1",
                    "key_env": "TEST_KEY",
                    "enabled": True,
                }
            },
            "entries": {"m": {"provider": "p", "model": "private-alias"}},
        }

    def test_unknown_is_automatic_not_none_or_false_claims(self):
        cfg = ModelSettings.parse(self.spec())
        entry = cfg.entry("m")
        self.assertEqual(reasoning_effort(entry), "auto")
        self.assertEqual(entry["reasoning_efforts"], [])
        self.assertNotIn("images", entry["capabilities"])
        self.assertFalse(entry.get("execution_validated", False))
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            reasoning_effort(entry, "none")

    def test_snapshot_bounds_budgets_and_preserves_explicit_overrides(self):
        spec = self.spec()
        snapshot = profile(
            spec["providers"]["p"],
            "private-alias",
            remote={
                "context_window": 8192,
                "output_limit": 1024,
                "images": True,
                "reasoning": True,
            },
        )
        spec["entries"]["m"].update(capabilities=snapshot, images=False, context_window=4096)
        entry = ModelSettings.parse(spec).entry("m")
        self.assertFalse(entry["images"])
        self.assertEqual(entry["context_window"], 4096)
        self.assertEqual(entry["max_output_tokens"], 1024)
        self.assertEqual(entry["execution_max_output_tokens"], 1024)
        self.assertEqual(entry["reasoning_efforts"], [])  # reasoning=true is not a level list
        self.assertEqual(spec["entries"]["m"]["capabilities"], snapshot)

    def test_snapshot_cannot_follow_changed_connection_or_model(self):
        original = self.spec()
        original["entries"]["m"]["capabilities"] = profile(original["providers"]["p"], "private-alias")
        for target, field, value in [
            ("provider", "base_url", "https://other.example/v1"),
            ("provider", "protocol", "openai_chat"),
            ("model", "model", "different"),
        ]:
            spec = deepcopy(original)
            (spec["providers"]["p"] if target == "provider" else spec["entries"]["m"])[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "changed"):
                ModelSettings.parse(spec)
        original["providers"]["p"]["enabled"] = False
        ModelSettings.parse(original)  # disabled settings remain editable

    def test_public_reference_is_explicit_and_cannot_invent_thinking_levels(self):
        public = {
            "revision": "test",
            "models": {
                "vendor/model": {
                    "context_window": 64000,
                    "reasoning": True,
                    "images": True,
                }
            },
        }
        provider = self.spec()["providers"]["p"]
        with patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=public):
            self.assertEqual(profile(provider, "model")["source"], "unknown")
            ref = profile(provider, "model", reference="vendor/model")
            self.assertEqual(ref["context_window"], 64000)
            self.assertNotIn("reasoning_efforts", ref)

    def test_malformed_metadata_cannot_poison_settings(self):
        self.assertEqual(
            facts(
                {
                    "images": "true",
                    "context_window": True,
                    "reasoning_efforts": ["invented"],
                    "output_limit": -1,
                }
            ),
            {},
        )
        spec = self.spec()
        spec["entries"]["m"]["capabilities"] = {
            **profile(spec["providers"]["p"], "private-alias"),
            "images": "true",
        }
        with self.assertRaisesRegex(ValueError, "snapshot"):
            ModelSettings.parse(spec)

    def test_gateway_auto_omits_control_but_none_is_explicit(self):
        for protocol in ("openai_chat", "openai_responses", "anthropic", "gemini"):
            route = {
                "protocol": protocol,
                "reasoning_efforts": ["none", "high"],
                "default_reasoning_effort": "auto",
            }
            for incoming in ({}, {"reasoning": {"effort": "auto"}}, {"reasoning_effort": "auto"}):
                result = prepare_request(route, deepcopy(incoming), {}, [])
                self.assertNotIn("reasoning", result)
                self.assertNotIn("reasoning_effort", result)
                self.assertNotIn("thinking", result)
            result = prepare_request(route, {"reasoning": {"effort": "none"}}, {}, [])
            self.assertEqual(result["reasoning"]["effort"], "none")
            if protocol == "anthropic":
                self.assertEqual(result["thinking"], {"type": "disabled"})

    def test_gateway_effort_update_requires_external_application(self):
        from assistant_runtime.core.models.gateway import application_steps

        raw = self.spec()
        before = ModelSettings.parse(raw)
        raw["entries"]["m"].update(reasoning_efforts=["low"], default_reasoning_effort="low")
        after = ModelSettings.parse(raw)
        self.assertTrue(application_steps(before, after))
        self.assertFalse(application_steps(after, after))

    def test_directory_keeps_only_bounded_capabilities(self):
        value = models(
            [
                {
                    "id": "m",
                    "images": True,
                    "context_window": 32000,
                    "reasoning_efforts": ["high"],
                    "api_key": "never-return",
                }
            ]
        )[0]
        self.assertTrue(value["capabilities"]["images"])
        self.assertNotIn("api_key", str(value))
        gemini = models(
            [{"name": "models/m", "inputTokenLimit": 64000, "outputTokenLimit": 2048}], gemini=True
        )[0]
        self.assertEqual(gemini["capabilities"]["output_limit"], 2048)


if __name__ == "__main__":
    unittest.main()
