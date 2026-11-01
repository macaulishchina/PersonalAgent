"""All bundled owners follow one declaration contract; quality gates are development-only."""

import ast
from dataclasses import is_dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from assistant_runtime.core.plugin_catalog import OWNERS, declaration, metadata, release
from assistant_runtime.app.versions import compatibility, check_dependencies
from assistant_runtime.app.bundled import RESOURCE_OWNERS, resources_for


class PluginStandardsTest(unittest.TestCase):
    def test_every_owner_has_consistent_release_configuration_skills_and_entry(self):
        releases = {name: release(name) for name in OWNERS}
        check_dependencies(releases, list(OWNERS))
        skills = set()
        for name, module in OWNERS.items():
            with self.subTest(plugin=name):
                spec = declaration(name)
                self.assertEqual(spec.manifest.name, name)
                self.assertIsNone(compatibility(releases[name]))
                if module.startswith("assistant_runtime.core."):
                    self.assertTrue(callable(spec.internal_factory))
                    self.assertIsNone(spec.create)
                else:
                    self.assertTrue(callable(spec.create))
                    self.assertIsNone(spec.internal_factory)
                self.assertEqual(bool(spec.service), bool(spec.tools_factory))
                self.assertEqual(len(spec.provides), len(set(spec.provides)))
                self.assertEqual(len(spec.uses), len(set(spec.uses)))
                if spec.config_key:
                    self.assertIsNotNone(spec.configuration)
                    self.assertTrue(callable(spec.validate))
                if spec.configuration:
                    self.assertEqual(spec.configuration.version, 1)
                for skill in spec.skills:
                    self.assertTrue(skill.is_relative_to(metadata(name).parent))
                    self.assertTrue((skill / "SKILL.md").is_file())
                    self.assertNotIn(skill.name, skills)
                    skills.add(skill.name)

    def test_provided_and_consumed_public_requests_match_owning_source(self):
        from assistant_runtime.api import contracts, models, ai_usage, files, conversation

        public = {
            key: value
            for module in (contracts, models, ai_usage, files, conversation)
            for key, value in vars(module).items()
            if isinstance(value, type) and is_dataclass(value)
        }
        providers = {}
        for name in OWNERS:
            spec = declaration(name)
            for kind in spec.provides:
                self.assertNotIn(kind, providers, f"Duplicate public capability owner: {kind}")
                providers[kind] = name
        for name, module in OWNERS.items():
            root = Path(__file__).resolve().parents[1] / "src" / Path(*module.split("."))
            paths = root.rglob("*.py") if root.is_dir() else (root.with_suffix(".py"),)
            registered, used = set(), set()
            for path in paths:
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if (
                        not isinstance(node, ast.Call)
                        or not isinstance(node.func, ast.Attribute)
                        or not node.args
                    ):
                        continue
                    value = node.args[0]
                    if node.func.attr == "register" and isinstance(value, ast.Name) and value.id in public:
                        registered.add(public[value.id])
                    if (
                        node.func.attr == "invoke"
                        and isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id in public
                    ):
                        used.add(public[value.func.id])
            spec = declaration(name)
            with self.subTest(plugin=name):
                self.assertLessEqual(
                    registered, set(spec.provides), "Registered capability missing from declaration"
                )
                self.assertLessEqual(
                    used - set(spec.provides), set(spec.uses), "Consumed capability missing from declaration"
                )
                for kind in spec.uses:
                    self.assertIn(kind, providers, f"No bundled provider for {kind}")

    def test_missing_resource_owner_has_an_explicit_preconstruction_error(self):
        host = SimpleNamespace(config=SimpleNamespace(enabled_plugins=()))
        for resource, owner in RESOURCE_OWNERS.items():
            with self.subTest(resource=resource):
                with self.assertRaisesRegex(ValueError, "requires enabled plugin " + owner):
                    resources_for(host, "synthetic", (resource,))

    def test_explicitly_provided_resource_does_not_require_a_configuration_flag(self):
        host = SimpleNamespace(conversation=object())
        resource = resources_for(host, "synthetic", ("conversation",))
        self.assertIsNotNone(resource["conversation"])
