"""Offline avatar assets, public readers, schema compatibility and SVG safety."""

import base64
from importlib.resources import files
import json
import unittest
import xml.etree.ElementTree as ET

from assistant_runtime.api.models import ModelRequest
from assistant_runtime.core.models import builtin_avatars, presentation
from assistant_runtime.core.models.plugin import ModelsPlugin
from assistant_runtime.core.models.settings import ModelSettings
from test_models import settings


class BuiltinAvatarTest(unittest.TestCase):
    def test_brands_and_forty_general_avatars_are_versioned_packaged_and_safe(self):
        catalog = builtin_avatars.catalog()
        self.assertEqual(len(catalog["avatars"]), 71)
        self.assertEqual(sum(item["group"] == "providers" for item in catalog["avatars"]), 30)
        self.assertEqual(sum(item["group"] == "general" for item in catalog["avatars"]), 41)
        self.assertEqual(len(set(builtin_avatars.names())), 71)
        for item in catalog["avatars"]:
            raw, version = builtin_avatars.read(item["name"], item["version"])
            root = ET.fromstring(raw)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            self.assertEqual(root.attrib["viewBox"], "0 0 64 64")
            self.assertEqual(version, item["version"])
            for element in root.iter():
                self.assertNotIn(
                    element.tag.rsplit("}", 1)[-1].lower(), {"script", "foreignobject", "image", "style"}
                )
                for name, value in element.attrib.items():
                    self.assertFalse(name.lower().startswith("on"))
                    self.assertNotIn(name.rsplit("}", 1)[-1].lower(), {"href", "src"})
                    self.assertNotIn("javascript:", value.lower())
            presentation.validate({"avatar": "builtin:" + item["name"]})

    def test_all_choices_pass_owner_and_configuration_schema(self):
        schema = json.loads(
            files("assistant_runtime.core.models").joinpath("configuration.json").read_text(encoding="utf-8")
        )
        for group in ("providers", "entries"):
            pattern = schema["schema"]["properties"][group]["additionalProperties"]["properties"][
                "presentation"
            ]["properties"]["avatar"]["pattern"]
            for name in builtin_avatars.names():
                self.assertRegex("builtin:" + name, pattern)
        for name in ("generic", "openai", "anthropic", "google", "deepseek", "qwen", "cat"):
            raw = settings()
            raw["providers"]["test"]["presentation"] = {"avatar": "builtin:" + name}
            parsed = ModelSettings.parse(raw)
            self.assertEqual(parsed.public_catalog()[0]["avatar"], {"kind": "builtin", "name": name})

    def test_unknown_paths_and_stale_versions_are_rejected_without_fallback(self):
        for name in ("../credentials", "openai.svg", "https://example.com/logo.svg", "unknown", None):
            with self.assertRaises(ValueError):
                builtin_avatars.read(name)
        with self.assertRaises(ValueError):
            builtin_avatars.read("openai", "stale")
        catalog = builtin_avatars.catalog()
        catalog["avatars"][0]["name"] = "changed-by-caller"
        self.assertEqual(builtin_avatars.catalog()["avatars"][0]["name"], "openai")

    def test_licenses_and_pinned_provenance_ship_with_assets(self):
        root = files("assistant_runtime.core.models").joinpath("avatars")
        self.assertIn("MIT License", root.joinpath("LOBE_ICONS_LICENSE.txt").read_text())
        self.assertIn("ISC License", root.joinpath("LUCIDE_LICENSE.txt").read_text())
        source = json.loads(root.joinpath("catalog.json").read_text(encoding="utf-8"))
        self.assertRegex(source["source_commit"], r"^[a-f0-9]{40}$")


class PublicBuiltinTest(unittest.IsolatedAsyncioTestCase):
    async def test_directory_and_builtin_reader_work_without_model_or_instance(self):
        plugin = object.__new__(ModelsPlugin)
        directory = await plugin.handle(ModelRequest("avatars"))
        for name in ("openai", "deepseek", "qwen", "robot", "cat"):
            item = next(item for item in directory["avatars"] if item["name"] == name)
            result = await plugin.handle(
                ModelRequest("avatar_read", {"builtin": name, "version": item["version"]})
            )
            self.assertEqual(result["media_type"], "image/svg+xml")
            self.assertEqual(base64.b64decode(result["data_base64"]), builtin_avatars.read(name)[0])
        with self.assertRaises(ValueError):
            await plugin.handle(ModelRequest("avatar_read", {"builtin": "openai", "version": "stale"}))
