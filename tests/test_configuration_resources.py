"""Whole-version resource transactions and immutable reads, without external services."""

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_configuration import ConfigurationFixture
from assistant_runtime.core.configuration.files import ConfigurationFiles, Conflict, atomic
from assistant_runtime.core.configuration.resources import Resources
from assistant_runtime.api.configuration_io import configuration_path


class ResourceTests(ConfigurationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.resources = Resources(self.root)
        self.files = ConfigurationFiles(
            self.entry, self.root / "state/configuration", self.validate, resources=self.resources
        )
        (self.root / ".env").write_text("# private\nDEEPSEEK_API_KEY=old-secret\n")

    def save_resource(self, operations):
        return self.files.patch(self.files.fields()["version"], [], resource_changes=operations)

    def test_secret_replace_private_backup_restore_and_environment_provenance(self):
        with self.files.locked():
            before = self.files.snapshot()
            public = self.resources.public(before.contents, before.resources)
        self.assertNotIn("old-secret", json.dumps(public))
        self.save_resource(
            [{"id": "credentials", "key": "DEEPSEEK_API_KEY", "op": "replace_secret", "value": "new-secret"}]
        )
        receipt = self.files.history()[0]
        self.assertIn("credentials", receipt["resource_present"])
        self.assertEqual(len(receipt["id"]), 32)
        self.assertNotIn("secret", json.dumps(self.files.history()))
        self.files.restore(receipt["id"], self.files.fields()["version"])
        self.assertIn("old-secret", (self.root / ".env").read_text())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "external"}):
            current = self.files.snapshot()
            credential = next(
                r
                for r in self.resources.public(current.contents, current.resources)
                if r["id"] == "credentials"
            )
            self.assertEqual(credential["keys"][0]["source"], "environment")
        self.assertNotIn("new-secret", (self.files.state / "audit.jsonl").read_text())

    def test_multifile_failure_does_not_delete_untouched_credentials(self):
        version = self.files.fields()["version"]
        failed = False

        def fail_once(path, content):
            nonlocal failed
            if Path(path) == self.folder / "usage.toml" and not failed:
                failed = True
                raise OSError("disk")
            atomic(path, content)

        with (
            patch("assistant_runtime.core.configuration.files.atomic", side_effect=fail_once),
            self.assertRaises(OSError),
        ):
            self.files.patch(version, [{"path": ["ai_usage", "max_requests"], "op": "set", "value": 512}])
        self.assertIn("old-secret", (self.root / ".env").read_text())
        self.assertEqual(self.files.fields()["version"], version)

    def test_registry_owner_revision_restore_and_external_writer_conflict(self):
        from assistant_runtime.core.integrations.registry import McpRegistry

        registry = McpRegistry(self.root, {})
        self.save_resource(
            [
                {
                    "id": "mcp",
                    "op": "replace_secret",
                    "value": json.dumps({"sample": {"transport": {"type": "stdio", "command": "echo"}}}),
                }
            ]
        )
        data = json.loads(registry.path.read_text())
        self.assertEqual(data["revision"], 1)
        with self.assertRaises(ValueError):
            registry.configure("other", {"transport": {"type": "stdio", "command": "echo"}}, 0)
        receipt = self.files.history()[0]
        self.files.restore(receipt["id"], self.files.fields()["version"])
        self.assertFalse(registry.path.exists())

    def test_resource_conflict_is_in_configuration_version(self):
        version = self.files.fields()["version"]
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=external-edit\n")
        with self.assertRaises(Conflict):
            self.files.patch(
                version,
                [],
                resource_changes=[{"id": "credentials", "key": "DEEPSEEK_API_KEY", "op": "clear_secret"}],
            )

    def test_frozen_credentials_used_until_restart_even_if_source_deleted(self):
        from assistant_runtime.core.models.gateway import credential

        frozen = self.root / "frozen.env"
        frozen.write_text("DEEPSEEK_API_KEY=frozen\n")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({str(self.root / ".env"): str(frozen)}))
        with patch.dict(os.environ, {"PERSONAL_AGENT_RESOURCE_MANIFEST": str(manifest)}):
            (self.root / ".env").unlink()
            self.assertEqual(configuration_path(self.root / ".env"), frozen)
            self.assertEqual(credential(self.root, "DEEPSEEK_API_KEY"), "frozen")

    def test_repair_damaged_fragment_does_not_echo_secrets_and_checks_version(self):
        self.files.fields()  # persist the registered file list before damage
        (self.folder / "usage.toml").write_text('secret = "sensitive-broken')
        repair = self.files.repair_inventory()
        self.assertNotIn("sensitive-broken", json.dumps(repair))
        with self.assertRaises(Conflict):
            self.files.repair("obsolete", {"usage.toml": "[ai_usage]\nmax_requests=256\n"})
        self.files.repair(repair["version"], {"usage.toml": "[ai_usage]\nmax_requests=256\n"})
        self.assertIn("old-secret", (self.root / ".env").read_text())
        self.assertEqual(self.files.fields()["entry"], "config.toml")

    def test_unknown_paths_and_invalid_resources_never_write(self):
        before = (self.root / ".env").read_bytes()
        for operation in [
            {"id": "../../etc/passwd", "op": "set", "value": "bad"},
            {"id": "credentials", "op": "set", "value": "bad"},
            {"id": "credentials", "op": "replace_secret", "key": "BAD", "value": "multiline\nsecret"},
            {"id": "mcp", "op": "set", "value": "{}bad"},
        ]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.save_resource([operation])
        self.assertEqual((self.root / ".env").read_bytes(), before)

    def test_registered_skill_assets_follow_frozen_generation_and_unselected_skill_is_hidden(self):
        skill = self.root / "config/skills/report"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: report\ndescription: example\n---\nUse helper.txt\n")
        (skill / "helper.txt").write_text("private helper")
        other = self.root / "config/skills/unused"
        other.mkdir()
        (other / "SKILL.md").write_text("unused")
        self.files.patch(
            self.files.fields()["version"], [{"path": ["instance_skills"], "op": "set", "value": ["report"]}]
        )
        snapshot = self.files.snapshot()
        self.assertIn("asset:report:helper.txt", snapshot.resources)
        self.assertNotIn("skill:unused", snapshot.resources)
        for ident, resource in self.resources.registered(snapshot.contents).items():
            if resource.kind == "skill":
                target = self.resources.frozen_target(self.root / "frozen", resource)
                self.assertEqual(target.name, "SKILL.md")
                self.assertEqual(target.parent.name, "report")

    def test_local_schema_reference_defaults_and_secret_redaction_share_shape(self):
        from assistant_runtime.api import ConfigurationSpec
        from assistant_runtime.core.configuration.schema import resolve
        from assistant_runtime.core.configuration.fields import present

        schema = {
            "type": "object",
            "$defs": {"secret": {"type": "string", "writeOnly": True, "default": "private-default"}},
            "properties": {"credential": {"$ref": "#/$defs/secret"}},
        }
        self.assertEqual(resolve(ConfigurationSpec(schema=schema), {}), {"credential": "private-default"})
        inventory = {"application": {"schema": schema}, "plugins": []}
        view = present(self.files.snapshot(), "config.toml", inventory)
        self.assertNotIn("private-default", json.dumps(view))

    def test_repair_cannot_move_instance_by_changing_relative_root(self):
        repair = self.files.repair_inventory()
        with self.assertRaises(ValueError):
            self.files.repair(
                repair["version"], {"config.toml": 'root="../outside"\nplugins=[]\ninclude=["usage.toml"]\n'}
            )

    def test_gateway_requires_deployment_for_changed_credentials_only(self):
        from assistant_runtime.core.models.settings import ModelSettings
        from assistant_runtime.core.models.gateway import application_steps

        settings = ModelSettings.parse(
            {
                "enabled": True,
                "providers": {"deepseek": {"preset": "deepseek", "enabled": True}},
                "entries": {
                    "fast": {"provider": "deepseek", "model": "synthetic", "reasoning_efforts": ["none"]}
                },
            }
        )
        before = {"credentials": b"DEEPSEEK_API_KEY=before\n"}
        after = {"credentials": b"DEEPSEEK_API_KEY=after\n"}
        self.assertTrue(application_steps(settings, settings, before, after))
        self.assertFalse(application_steps(settings, settings, before, before))
        unrelated = {"credentials": b"DEEPSEEK_API_KEY=before\nTYPESAFE_API_KEY=unrelated\n"}
        self.assertFalse(application_steps(settings, settings, before, unrelated))
