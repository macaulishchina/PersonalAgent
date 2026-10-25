"""Offline metadata never grants acceptance; explicit selections survive route changes safely."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from assistant_runtime.core.models.capabilities import facts, profile, search_references
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

    def test_reference_search_ranks_all_suppliers_without_selecting(self):
        public = {
            "revision": "fixture",
            "models": {
                "a/deepseek-v4.1-flash-preview": {"context_window": 1000},
                "b/DeepSeek-V4.1-Flash": {"context_window": 2000},
                "c/deepseek-v4-1-flash": {"context_window": 3000},
                "d/other-model": {},
            },
        }
        with patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=public):
            result = search_references("deepseek-v4.1-flash")
            self.assertEqual([c["provider"] for c in result["candidates"]], ["b", "c", "a"])
            self.assertEqual(result["candidates"][0]["match"], "exact")
            self.assertEqual(result["candidates"][-1]["match"], "related")
            self.assertEqual(search_references("b deepseek")["total"], 1)
            self.assertEqual(search_references("unmatched")["total"], 0)
            self.assertEqual(search_references("")["total"], 0)
            self.assertEqual(
                profile(self.spec()["providers"]["p"], "deepseek-v4.1-flash")["source"], "unknown"
            )
        public["models"] = {f"supplier-{i:03}/same-model": {"tools": True} for i in range(121)}
        with patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=public):
            result = search_references("same-model")
            self.assertEqual(len(result["candidates"]), 121)
            self.assertEqual(result["total"], 121)
            self.assertEqual(len({c["reference"] for c in result["candidates"]}), 121)

    def test_catalog_origin_uses_upstream_identity_for_any_lab_and_alias(self):
        public = {
            "revision": "fixture",
            "models": {
                "relay/novel-model": {"canonical_model_id": "newlab/novel-model"},
                "newlab/current-alias": {"canonical_model_id": "newlab/novel-model"},
                "newlab/unattributed": {},
                "another/cloud-alias": {"canonical_model_id": "unseen-maker/novel-model"},
                "newlab/bad-attribution": {"canonical_model_id": "missing-separator"},
            },
        }
        with patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=public):
            results = search_references("novel-model")["candidates"]
            self.assertEqual(results[0]["reference"], "newlab/current-alias")
            self.assertEqual(results[0]["origin"], "original")
            self.assertEqual(results[0]["canonical_model_id"], "newlab/novel-model")
            self.assertEqual({r["origin"] for r in results[1:]}, {"other"})
            self.assertEqual(search_references("unattributed")["candidates"][0]["origin"], "unknown")
            self.assertEqual(search_references("bad-attribution")["candidates"][0]["origin"], "unknown")

    def test_catalog_compiler_retains_explicit_attribution_only(self):
        import json
        from scripts.update_model_catalog import compile_catalog

        raw = {
            "unseen-maker": {
                "models": {
                    "alias": {"reasoning": True, "canonical_model_id": "unseen-maker/original"},
                    "unknown": {"reasoning": False},
                    "bad": {"canonical_model_id": "bad/no spaces allowed", "reasoning": True},
                }
            }
        }
        catalog = compile_catalog(json.dumps(raw).encode())
        self.assertEqual(
            catalog["models"]["unseen-maker/alias"]["canonical_model_id"], "unseen-maker/original"
        )
        self.assertNotIn("canonical_model_id", catalog["models"]["unseen-maker/unknown"])
        self.assertNotIn("canonical_model_id", catalog["models"]["unseen-maker/bad"])

    def test_public_efforts_are_imported_without_inventing_toggle_or_budget_levels(self):
        import json
        from scripts.update_model_catalog import compile_catalog

        raw = {
            "any-lab": {
                "models": {
                    "m": {
                        "reasoning": True,
                        "reasoning_options": [
                            {"type": "toggle"},
                            {"type": "budget_tokens", "min": 10, "max": 1000},
                            {
                                "type": "effort",
                                "values": ["low", "high", "max", "default", None, "unsupported"],
                            },
                        ],
                    },
                    "toggle-only": {"reasoning": True, "reasoning_options": [{"type": "toggle"}]},
                }
            }
        }
        catalog = compile_catalog(json.dumps(raw).encode())
        self.assertEqual(catalog["models"]["any-lab/m"]["reasoning_efforts"], ["low", "high", "max"])
        self.assertNotIn("reasoning_efforts", catalog["models"]["any-lab/toggle-only"])
        with patch("assistant_runtime.core.models.capabilities.public_catalog", return_value=catalog):
            provider = self.spec()["providers"]["p"]
            for protocol in ("openai_chat", "openai_responses"):
                metadata = profile({**provider, "protocol": protocol}, "private-alias", reference="any-lab/m")
                self.assertEqual(metadata["reasoning_efforts"], ["low", "high", "max"])
                for effort in metadata["reasoning_efforts"]:
                    request = prepare_request(
                        {
                            "protocol": protocol,
                            "reasoning_efforts": metadata["reasoning_efforts"],
                            "default_reasoning_effort": "auto",
                        },
                        {"reasoning": {"effort": effort}},
                        {},
                        [],
                    )
                    self.assertEqual(request["reasoning"]["effort"], effort)
            self.assertEqual(
                profile(
                    {"protocol": "pi"}, "m", reference="any-lab/m", native={"reasoning_efforts": ["high"]}
                )["reasoning_efforts"],
                ["high"],
            )
            self.assertNotIn(
                "reasoning_efforts",
                profile({**provider, "protocol": "anthropic"}, "m", reference="any-lab/m"),
            )

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

    def test_model_output_setting_is_authoritative_and_validated(self):
        spec = self.spec()
        spec["entries"]["m"].update(
            capabilities=profile(spec["providers"]["p"], "private-alias", remote={"output_limit": 1024}),
            output_limit=2048,
        )
        entry = ModelSettings.parse(spec).entry("m")
        self.assertEqual(entry["max_output_tokens"], 2048)
        self.assertEqual(entry["execution_max_output_tokens"], 2048)
        self.assertEqual(entry["capabilities"]["output_limit"], 1024)  # provenance, not another active value
        for value in (0, -1, True, 10000001, "large"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "output limit"):
                spec["entries"]["m"]["output_limit"] = value
                ModelSettings.parse(spec)

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
