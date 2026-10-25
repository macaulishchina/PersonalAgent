"""Operator file transaction invariants; no models, devices or running instance."""

from platform_support import private_mode, symlink_or_skip

import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from assistant_runtime.api import ConfigurationSpec, PluginDefinition, Manifest
from assistant_runtime.core.configuration.files import ConfigurationFiles, Conflict, MASK, atomic
from assistant_runtime.core.configuration.validation import validate_candidate
from assistant_runtime.core.configuration.catalog import catalog, describe_definition


class ConfigurationFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "config"
        self.folder.mkdir()
        self.entry = self.folder / "config.toml"
        self.entry.write_text(
            f'# instance\nroot = "{self.root.as_posix()}"\nplugins = []\ninclude = ["usage.toml"]\n'
        )
        (self.folder / "usage.toml").write_text("# policy\n[ai_usage]\nmax_requests = 256 # shared work\n")
        self.files = ConfigurationFiles(self.entry, self.root / "private", self.validate)

    def validate(self, contents):
        return validate_candidate(self.entry.name, contents, root=self.root)

    def documents(self):
        return self.files.read()

    def change(self, amount=512):
        snapshot = self.documents()
        snapshot["files"]["usage.toml"]["ai_usage"]["max_requests"] = amount
        return snapshot


class ConfigurationTest(ConfigurationFixture, unittest.TestCase):
    def test_preserves_comments_layout_and_checks_whole_candidate(self):
        before = self.entry.read_bytes()
        changed = self.change()
        result = self.files.save(changed["version"], changed["files"])
        self.assertEqual(result["state"], "pending_restart")
        self.assertEqual(self.entry.read_bytes(), before)
        self.assertIn("max_requests = 512 # shared work", (self.folder / "usage.toml").read_text())
        self.assertIn("# policy", (self.folder / "usage.toml").read_text())
        self.assertEqual(self.files.history()[0]["version"], changed["version"])
        self.assertEqual(private_mode(self.files.state / "versions"), 0o700)

    def test_invalid_configuration_writes_nothing(self):
        changed = self.change("invalid type")
        before = self.documents()
        with self.assertRaises(ValueError):
            self.files.save(changed["version"], changed["files"])
        self.assertEqual(self.documents(), before)
        self.assertFalse((self.files.state / "transaction.json").exists())

    def test_external_edit_and_other_editor_conflict(self):
        changed = self.change()
        (self.folder / "usage.toml").write_text("# external\n[ai_usage]\nmax_requests=300\n")
        with self.assertRaises(Conflict):
            self.files.save(changed["version"], changed["files"])
        changed = self.change()
        self.files.save(changed["version"], changed["files"])
        with self.assertRaises(Conflict):
            self.files.save(changed["version"], changed["files"])

    def test_detects_edit_during_validation(self):
        changed = self.change()

        def editing(contents):
            self.validate(contents)
            (self.folder / "usage.toml").write_text("[ai_usage]\nmax_requests=300\n")

        self.files.validate = editing
        with self.assertRaises(Conflict):
            self.files.save(changed["version"], changed["files"])
        self.assertIn("300", (self.folder / "usage.toml").read_text())

    def test_multifile_disk_failure_rolls_back_all_files(self):
        changed = self.change()
        changed["files"]["config.toml"]["assistant_name"] = "test"
        before = self.documents()
        failed = False

        def fail_once(path, content):
            nonlocal failed
            if Path(path) == self.folder / "usage.toml" and not failed:
                failed = True
                raise OSError("disk unavailable")
            return atomic(path, content)

        with patch("assistant_runtime.core.configuration.files.atomic", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.files.save(changed["version"], changed["files"])
        self.assertEqual(self.documents(), before)
        self.assertIn("rolled_back", (self.files.state / "audit.jsonl").read_text())

    def test_restart_recovers_uncommitted_transaction_before_read(self):
        changed = self.change()
        self.files.save(changed["version"], changed["files"])
        receipt = self.files.history()[0]
        atomic(self.files.state / "transaction.json", json.dumps({"backup": receipt["id"]}).encode())
        new = ConfigurationFiles(self.entry, self.files.state, self.validate)
        self.assertEqual(new.read()["version"], changed["version"])
        self.assertFalse((self.files.state / "transaction.json").exists())

    def test_restore_is_new_validated_transaction_and_checks_revision(self):
        before = self.documents()
        changed = self.change()
        result = self.files.save(changed["version"], changed["files"])
        ident = self.files.history()[0]["id"]
        with self.assertRaises(Conflict):
            self.files.restore(ident, before["version"])
        self.files.restore(ident, result["version"])
        self.assertEqual(self.documents()["files"], before["files"])
        self.assertEqual(len(self.files.history()), 2)

    def test_secrets_never_returned_or_audited_and_unrelated_patch_preserves(self):
        secret = "private-cookie-for-test"
        (self.folder / "usage.toml").write_text(f'[web]\npassword = "{secret}"\n')
        # Generic persistence has no knowledge of this synthetic owner's schema.
        self.files.validate = lambda contents: None
        snapshot = self.documents()
        self.assertNotIn(secret, json.dumps(snapshot))
        self.assertEqual(snapshot["files"]["usage.toml"]["web"]["password"], MASK)
        self.files.patch(snapshot["version"], [{"path": ["web", "enable"], "op": "set", "value": True}])
        self.assertEqual(tomllib.loads((self.folder / "usage.toml").read_text())["web"]["password"], secret)
        self.assertNotIn(secret, (self.files.state / "audit.jsonl").read_text())
        self.assertNotIn(secret, json.dumps(self.files.history()))

    def test_refuses_paths_include_edits_and_data_migration(self):
        snapshot = self.documents()
        for name in ("../outside.toml", "/tmp/outside.toml", "extra.toml"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.files.save(snapshot["version"], {name: {}})
        for key, value in (("root", "/tmp/other"), ("include", []), ("pi_binary", "/tmp/pi")):
            snapshot = self.documents()
            snapshot["files"]["config.toml"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.files.save(snapshot["version"], snapshot["files"])
        original = (self.folder / "usage.toml").read_bytes()
        (self.folder / "usage.toml").unlink()
        target = self.root / "other.toml"
        target.write_bytes(original)
        symlink_or_skip(self.folder / "usage.toml", target)
        with self.assertRaises(ValueError):
            self.documents()

    def test_missing_dependency_rejected_without_constructing_plugins(self):
        changed = self.documents()
        changed["files"]["config.toml"]["plugins"] = ["execution"]
        with self.assertRaises(ValueError):
            self.files.save(changed["version"], changed["files"])

    def test_descriptor_is_public_and_catalog_does_not_import_disabled_plugins(self):
        spec = PluginDefinition(
            Manifest("sample"),
            configuration=ConfigurationSpec(
                title="Sample", schema={"type": "object"}, sensitive=("password",)
            ),
        )
        self.assertEqual(describe_definition(spec)["sensitive"], ["password"])
        with patch("importlib.metadata.EntryPoint.load", side_effect=AssertionError("must not import")):
            result = catalog()
        self.assertIn("models", {p["id"] for p in result["plugins"]})


class SchemaTest(unittest.TestCase):
    def test_schema_defaults_nested_conditions_and_secret_safe_error(self):
        from assistant_runtime.core.configuration.schema import resolve

        spec = ConfigurationSpec(
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "limit": {"type": "integer", "default": 7},
                    "optional": {"type": "object", "properties": {"enabled": {"default": False}}},
                    "password": {"type": "integer"},
                },
            }
        )
        raw = {}
        self.assertEqual(resolve(spec, raw), {"limit": 7})
        self.assertEqual(raw, {})
        self.assertEqual(resolve(spec, {"optional": {}}), {"limit": 7, "optional": {"enabled": False}})
        with self.assertRaises(ValueError) as error:
            resolve(spec, {"password": "must-not-appear"})
        self.assertNotIn("must-not-appear", str(error.exception))
        with self.assertRaises(ValueError):
            resolve(ConfigurationSpec(version=99), {})

    def test_admin_reset_invalidates_existing_sessions_and_does_not_share_chat(self):
        from assistant_runtime.core.configuration.auth import AdminAccess

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "admin"
            admin = AdminAccess(path)
            token = path.read_text().strip()
            session = admin.login(token, "local")
            self.assertTrue(admin.valid(session))
            atomic(path, b"another-private-token-with-more-than-24-characters\n")
            self.assertFalse(admin.valid(session))
            with self.assertRaises(PermissionError):
                admin.login(token, "local")


class ConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_auth_origin_isolation_and_configuration_unavailable(self):
        from aiohttp.test_utils import TestClient, TestServer
        from assistant_runtime.core.configuration.console import Console, web
        from assistant_runtime.core.configuration.auth import AdminAccess

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            console = object.__new__(Console)
            console.access = AdminAccess(root / "admin")

            async def status(request):
                return web.json_response({"state": "failed"})

            app = web.Application(middlewares=[console.guard])
            app.add_routes([web.post("/api/login", console.login), web.get("/api/status", status)])
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/status", cookies={"personal_agent_session": "chat-session"})
                self.assertEqual(response.status, 401)
                response = await client.post(
                    "/api/login",
                    json={"token": (root / "admin").read_text().strip()},
                    headers={"Origin": "https://untrusted.example"},
                )
                self.assertEqual(response.status, 403)
                origin = str(client.make_url("/")).rstrip("/")
                response = await client.post(
                    "/api/login",
                    json={"token": (root / "admin").read_text().strip()},
                    headers={"Origin": origin},
                )
                self.assertEqual(response.status, 200)
                response = await client.get("/api/status")
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["state"], "failed")


class ManagementProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_business_start_and_stop_without_models_or_business_plugins(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text(f'root = "{root.as_posix()}"\nplugins = []\n')
            business = BusinessProcess(
                ManagementSettings(root, entry, startup_seconds=10, shutdown_seconds=10)
            )
            try:
                await business.start()
                self.assertEqual(business.outcome, "running", business.error)
                self.assertTrue(
                    (await business.request({"command": "management-status"}))["startup_complete"]
                )
                self.assertTrue((business.state / "applied.json").exists())
                saved = business.files.read()
                saved["files"]["config.toml"]["assistant_name"] = "new-name"
                business.files.save(saved["version"], saved["files"])
                self.assertNotEqual(business.files.read()["version"], business.loaded)
                self.assertIsNone(business.process.returncode)
            finally:
                await business.stop()
            self.assertIsNotNone(business.process.returncode)

    async def test_start_uses_frozen_files_and_verifies_worker_generation(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings
        import asyncio

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text(f'root = "{root.as_posix()}"\nplugins = []\nassistant_name="before"\n')
            business = BusinessProcess(
                ManagementSettings(root, entry, startup_seconds=10, shutdown_seconds=10)
            )
            initial = business.files.snapshot().version
            spawn = asyncio.create_subprocess_exec
            observed = []

            async def edit_then_spawn(*args, **kwargs):
                frozen = Path(args[args.index("--config") + 1])
                observed.append(tomllib.loads(frozen.read_text())["assistant_name"])
                business.files.patch(initial, [{"path": ["assistant_name"], "op": "set", "value": "after"}])
                return await spawn(*args, **kwargs)

            try:
                with patch(
                    "assistant_runtime.app.management.asyncio.create_subprocess_exec",
                    side_effect=edit_then_spawn,
                ):
                    await business.start()
                self.assertEqual(business.outcome, "running", business.error)
                self.assertEqual(observed, ["before"])
                self.assertEqual(business.loaded, initial)
                status = await business.request({"command": "management-status"})
                self.assertEqual(status["configuration_version"], initial)
                self.assertEqual(status["supervisor_pid"], business.process.pid)
                self.assertNotEqual(business.files.fields()["version"], initial)
            finally:
                await business.stop()

    async def test_invalid_business_configuration_does_not_create_worker(self):
        from assistant_runtime.app.management import BusinessProcess
        from assistant_runtime.core.configuration.settings import ManagementSettings

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / "config.toml"
            entry.write_text("broken=[\n")
            business = BusinessProcess(ManagementSettings(root, entry))
            await business.start()
            self.assertIsNone(business.process)
            self.assertIsNone(business.loaded)
            self.assertEqual(business.outcome, "failed")
            self.assertEqual((await business.status())["state"], "failed")


class FieldPatchTest(ConfigurationFixture, unittest.TestCase):
    def test_defaults_are_visible_but_not_materialized_on_unrelated_save(self):
        from assistant_runtime.core.ai_usage.settings import UsageSettings

        before = self.files.fields()
        fields = {tuple(f["path"]): f for f in before["fields"]}
        self.assertEqual(fields[("ai_usage", "queue_size")]["value"], UsageSettings().queue_size)
        self.assertFalse(fields[("ai_usage", "queue_size")]["explicit"])
        self.files.patch(before["version"], [{"path": ["assistant_name"], "op": "set", "value": "测试"}])
        self.assertNotIn("queue_size", (self.folder / "usage.toml").read_text())
        self.assertIn("# shared work", (self.folder / "usage.toml").read_text())
        version = self.files.fields()["version"]
        self.files.patch(version, [{"path": ["ai_usage", "max_requests"], "op": "default"}])
        field = next(f for f in self.files.fields()["fields"] if f["path"] == ["ai_usage", "max_requests"])
        self.assertEqual(field["value"], UsageSettings().max_requests)
        self.assertFalse(field["explicit"])

    def test_rejects_unknown_readonly_and_overlapping_paths(self):
        for patches in (
            [{"path": ["unknown"], "op": "set", "value": 1}],
            [{"path": ["root"], "op": "set", "value": "/tmp/other"}],
            [
                {"path": ["model_api", "entries"], "op": "set", "value": {}},
                {"path": ["model_api", "entries", "test"], "op": "set", "value": {}},
            ],
            [{"path": ["assistant_name"], "op": "replace_secret", "value": "x"}],
        ):
            with self.subTest(patches=patches), self.assertRaises(ValueError):
                self.files.patch(self.files.fields()["version"], patches)

    def test_declared_secret_without_secret_name_never_echoes_and_uses_explicit_operations(self):
        from assistant_runtime.core.configuration.fields import patch_documents, present

        secret = "synthetic-secret-value"
        (self.folder / "usage.toml").write_text(f'[plugin_settings.sample]\ncredential="{secret}"\n')
        inventory = {
            "application": {"schema": {"properties": {}}},
            "plugins": [
                {
                    "configuration": {
                        "namespace": "plugin_settings.sample",
                        "schema": {
                            "properties": {
                                "credential": {"type": "string", "writeOnly": True, "default": secret},
                                "limit": {"type": "integer", "default": 10},
                            }
                        },
                    }
                }
            ],
        }
        snapshot = self.files.snapshot()
        response = present(snapshot, "config.toml", inventory)
        self.assertNotIn(secret, json.dumps(response))
        self.assertEqual(response["fields"][0]["value"], {"configured": True})
        path = ["plugin_settings", "sample", "credential"]
        with self.assertRaises(ValueError):
            patch_documents(snapshot, "config.toml", [{"path": path, "op": "set", "value": "new"}], inventory)
        cleared = patch_documents(snapshot, "config.toml", [{"path": path, "op": "clear_secret"}], inventory)
        self.assertNotIn("credential", cleared["usage.toml"]["plugin_settings"]["sample"])
        changed = patch_documents(
            snapshot, "config.toml", [{"path": path, "op": "replace_secret", "value": "new"}], inventory
        )
        self.assertEqual(changed["usage.toml"]["plugin_settings"]["sample"]["credential"], "new")

    def test_split_tables_edit_child_without_moving_other_file(self):
        self.entry.write_text(self.entry.read_text() + "\n[ai_usage]\nqueue_size=128\n")
        before = self.entry.read_bytes()
        snapshot = self.files.fields()
        self.files.patch(
            snapshot["version"], [{"path": ["ai_usage", "max_requests"], "op": "set", "value": 512}]
        )
        self.assertEqual(self.entry.read_bytes(), before)
        self.assertIn("512 # shared work", (self.folder / "usage.toml").read_text())


class ListenerValidationTest(unittest.TestCase):
    def test_listener_collision_uses_plugin_metadata_without_runtime(self):
        from types import SimpleNamespace
        from assistant_runtime.core.configuration.validation import validate_ports

        configuration = SimpleNamespace(enabled_plugins=("apple_home",), apple_home={}, plugin_settings={})
        management = SimpleNamespace(host="0.0.0.0", port=8047)
        with self.assertRaisesRegex(ValueError, "Listener conflict"):
            validate_ports(configuration, management)
        configuration.apple_home = {"port": 8048}
        validate_ports(configuration, management)
        configuration.apple_home = {"host": "127.0.0.2"}
        management.host = "127.0.0.1"
        validate_ports(configuration, management)


class ConfigurationSafetyTest(unittest.TestCase):
    def test_corrupt_administrator_token_cannot_turn_into_empty_password(self):
        from assistant_runtime.core.configuration.auth import AdminAccess

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "admin"
            admin = AdminAccess(path)
            session = admin.login(path.read_text().strip(), "local")
            atomic(path, b"\n")
            for operation in (lambda: admin.valid(session), lambda: admin.login("", "local")):
                with self.assertRaises(ValueError):
                    operation()
            self.assertFalse(admin.sessions)

    def test_boolean_schema_and_declared_nested_secret_status(self):
        from assistant_runtime.core.configuration.fields import public_rule, redact_declared
        from assistant_runtime.core.configuration.files import redact
        from assistant_runtime.core.configuration.schema import resolve

        schema = {"type": "object", "properties": {"password": {"writeOnly": True}, "extra": True}}
        safe = redact(redact_declared({"password": "private-value", "extra": {"ok": True}}, schema))
        self.assertEqual(safe["password"], {"configured": True})
        self.assertNotIn("private-value", json.dumps(safe))
        self.assertEqual(public_rule(schema)["properties"]["extra"], True)
        self.assertEqual(resolve(ConfigurationSpec(schema=schema), {"extra": 42}), {"extra": 42})
        with self.assertRaisesRegex(ValueError, "local references"):
            resolve(ConfigurationSpec(schema={"$ref": "https://example.invalid/schema"}), {})


class MetadataIsolationTest(unittest.TestCase):
    def test_unselected_broken_package_does_not_break_minimal_configuration(self):
        from types import SimpleNamespace
        from assistant_runtime.app.config import Config
        from assistant_runtime.core.configuration.catalog import catalog

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad/configuration.json"
            path.parent.mkdir()
            path.write_text("not valid json")
            entry = SimpleNamespace(
                name="bad",
                value="bad:declaration",
                dist=SimpleNamespace(files=["bad/configuration.json"], locate_file=lambda unused: path),
            )
            with patch("assistant_runtime.core.configuration.catalog.entry_points", return_value=[entry]):
                config = Config(root=Path(folder), plugins=[])
                self.assertEqual(config.plugins, [])
                description = next(p for p in catalog()["plugins"] if p["id"] == "bad")
                self.assertIn("error", description["configuration"])
                retained = Config(root=Path(folder), plugins=[], plugin_settings={"bad": {"draft": True}})
                self.assertEqual(retained.plugin_settings, {"bad": {"draft": True}})
                with self.assertRaises(ValueError):
                    Config(root=Path(folder), plugins=["bad"], plugin_settings={"bad": {}})
