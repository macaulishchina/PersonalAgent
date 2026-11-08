"""Cloud settings are file-owned; versions and harmless apply receipts are derived."""

import copy
import json
import sqlite3
import unittest
from types import SimpleNamespace

from assistant_runtime.core.plugin_catalog import declaration
from assistant_runtime.core.configuration.schema import resolve_definition
from assistant_runtime.plugins.ai_passport.repository import Devices
from assistant_runtime.plugins.ai_passport.settings import desired
from assistant_runtime.plugins.ai_passport.plugin import AiPassportPlugin

DEVICE = "d" * 32


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.devices = Devices(self.db)
        self.spec = declaration("ai_passport")

    def tearDown(self):
        self.db.close()

    def test_defaults_overrides_and_private_wifi_validation(self):
        settings = resolve_definition(
            self.spec,
            {
                "volume": 56,
                "devices": {
                    DEVICE: {
                        "sleep_seconds": 300,
                        "wifi_networks": {
                            "2": {"ssid": "Phone", "password": ""},
                            "1": {"ssid": "家里", "password": "sample-secret"},
                        },
                    }
                },
            },
        )
        value = desired(settings, DEVICE)
        self.assertEqual(value["volume"], 56)
        self.assertEqual(value["sleep_seconds"], 300)
        self.assertEqual([n["ssid"] for n in value["wifi_networks"]], ["家里", "Phone"])
        self.assertNotIn("volume", settings["devices"][DEVICE])
        self.assertEqual(value["brightness"], 65)
        self.assertEqual(value["text_size"], 16)
        self.devices.configuration(DEVICE, value)
        dumped = "\n".join(self.db.iterdump())
        self.assertNotIn("sample-secret", dumped)
        self.assertNotIn("家里", dumped)
        self.assertNotIn("password", json.dumps(self.devices.configuration_state(DEVICE)))
        for override in (
            {"brightness": 0},
            {"text_size": 17},
            {"wifi_networks": {"1": {"ssid": "中文" * 6, "password": "password"}}},
            {"wifi_networks": {"1": {"ssid": "a", "password": "short"}}},
            {"wifi_networks": {"1": {"ssid": "a", "password": ""}, "2": {"ssid": "a", "password": ""}}},
            {"server_url": "http://user:secret@example.com"},
            {"server_url": "http://example.com/elsewhere"},
            {"trusted_ca": "-----BEGIN PRIVATE KEY-----"},
        ):
            with self.subTest(override=list(override)), self.assertRaises(ValueError):
                resolve_definition(self.spec, {"devices": {DEVICE: override}})
        self.assertEqual(
            resolve_definition(self.spec, {"devices": {DEVICE: {"server_url": "http://127.0.0.1:8047/"}}})[
                "devices"
            ][DEVICE]["server_url"],
            "http://127.0.0.1:8047/",
        )

    def test_only_remote_changed_field_advances_and_removed_field_releases(self):
        value = {
            "volume": 45,
            "keep_online": False,
            "wifi_networks": [{"ssid": "home", "password": "password"}],
        }
        first = self.devices.configuration(DEVICE, value)
        versions = {k: v["version"] for k, v in first.items()}
        self.devices.configuration_receipt(DEVICE, versions, {"volume": 80})
        self.assertEqual(self.devices.configuration(DEVICE, copy.deepcopy(value)), first)
        changed = self.devices.configuration(DEVICE, {**value, "keep_online": True})
        self.assertEqual(changed["volume"]["version"], first["volume"]["version"])
        self.assertGreater(changed["keep_online"]["version"], first["keep_online"]["version"])
        state = self.devices.configuration_state(DEVICE)
        self.assertEqual(state["volume"]["actual"], 80)
        self.assertEqual(state["volume"]["applied"], state["volume"]["desired"])
        self.assertNotEqual(state["keep_online"]["applied"], state["keep_online"]["desired"])
        removed = self.devices.configuration(DEVICE, {"volume": 45, "keep_online": True})
        self.assertIsNone(removed["wifi_networks"]["value"])
        self.assertGreater(removed["wifi_networks"]["version"], first["wifi_networks"]["version"])
        with self.assertRaisesRegex(ValueError, "version_mismatch"):
            self.devices.configuration_receipt(DEVICE, versions, {"volume": 50})
        self.assertEqual(self.devices.configuration_state(DEVICE)["volume"]["actual"], 80)

    def test_new_private_nested_tables_use_field_operations_and_keep_saved_password(self):
        from assistant_runtime.core.configuration.fields import patch_documents, present

        snapshot = SimpleNamespace(contents={"config.toml": b""}, version="test")
        base = ["ai_passport", "devices", DEVICE]
        network = [*base, "wifi_networks", "1"]
        patches = [
            {"path": base, "op": "create", "value": {}},
            {"path": network, "op": "create", "value": {}},
            {"path": [*network, "ssid"], "op": "set", "value": "Home"},
            {"path": [*network, "password"], "op": "replace_secret", "value": "synthetic-private"},
            {"path": [*base, "volume"], "op": "set", "value": 60},
        ]
        documents = patch_documents(snapshot, "config.toml", patches)
        raw = documents["config.toml"]["ai_passport"]
        self.assertEqual(resolve_definition(self.spec, raw)["devices"][DEVICE]["volume"], 60)
        import tomlkit

        snapshot.contents["config.toml"] = tomlkit.dumps(documents["config.toml"]).encode()
        response = present(snapshot, "config.toml")
        self.assertNotIn("synthetic-private", json.dumps(response))
        changed = patch_documents(
            snapshot, "config.toml", [{"path": [*base, "volume"], "op": "set", "value": 80}]
        )
        self.assertEqual(
            changed["config.toml"]["ai_passport"]["devices"][DEVICE]["wifi_networks"]["1"]["password"],
            "synthetic-private",
        )
        for bad in (
            [{"path": base, "op": "create", "value": {}}],
            [{"path": base, "op": "create", "value": {"volume": 80}}],
            [{"path": base, "op": "set", "value": {"volume": 80}}],
        ):
            with self.assertRaises(ValueError):
                patch_documents(snapshot, "config.toml", bad)


class ConfigurationApplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_rollback_and_committed_notification(self):
        old = {"volume": 45, "devices": {}}
        plugin = AiPassportPlugin(SimpleNamespace(config=old))
        notifications = []

        async def send():
            notifications.append(plugin.settings["volume"])

        plugin.connections["device"] = SimpleNamespace(send_configuration=send)
        update = await plugin.prepare_configuration({"volume": 80, "devices": {}})
        self.assertIs(plugin.settings, old)
        await update.commit()
        await update.rollback()
        await update.release(False)
        self.assertIs(plugin.settings, old)
        self.assertEqual(notifications, [])
        update = await plugin.prepare_configuration({"volume": 80, "devices": {}})
        await update.commit()
        await update.release(True)
        self.assertEqual(notifications, [80])
