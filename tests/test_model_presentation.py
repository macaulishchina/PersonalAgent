"""Appearance does not change routes; images commit through configuration resources."""

import base64
import asyncio
from io import BytesIO
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from assistant_runtime.core.models import presentation
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.configuration.files import ConfigurationFiles, atomic
from assistant_runtime.core.configuration.resources import Resources
from test_configuration import ConfigurationFixture
from test_models import settings
from assistant_runtime.core.models.plugin import ModelsPlugin
from assistant_runtime.api.models import ModelRequest


def image_bytes(format="PNG"):
    out = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "should disappear")
    Image.new("RGB", (640, 360), "green").save(out, format=format, pnginfo=metadata)
    return out.getvalue()


class ImageTest(unittest.TestCase):
    def test_normalized_formats_are_bounded_deterministic_and_metadata_free(self):
        for format in ("PNG", "JPEG", "WEBP"):
            raw = presentation.normalize(image_bytes(format))
            self.assertEqual(raw, presentation.normalize(raw))
            with Image.open(BytesIO(raw)) as image:
                self.assertEqual(image.format, "PNG")
                self.assertLessEqual(max(image.size), 256)
                self.assertNotIn("private", image.info)
        for raw in (b"<svg/>", b"not-an-image", b"x" * (presentation.MAX_UPLOAD + 1)):
            with self.assertRaises((ValueError, OSError)):
                presentation.normalize(raw)

    def test_display_edits_keep_route_and_capability_binding_stable(self):
        raw = settings()
        before = ModelSettings.parse(raw)
        raw["providers"]["test"]["presentation"] = {"display_name": "Team", "avatar": "builtin:openai"}
        raw["entries"]["test-model"]["presentation"] = {"display_name": "Helpful model"}
        after = ModelSettings.parse(raw)
        self.assertEqual(before.route("test-model"), after.route("test-model"))
        self.assertEqual(
            before.entries["test-model"]["capabilities"]["binding"],
            after.entries["test-model"]["capabilities"]["binding"],
        )
        row = after.public_catalog()[0]
        self.assertEqual(row["display_name"], "Helpful model")
        self.assertEqual(row["displayName"], "Helpful model")
        self.assertEqual(after.catalog()[0]["display_name"], "Helpful model")
        self.assertEqual(after.catalog()[0]["displayName"], "Helpful model")
        self.assertEqual(row["model"], "test-model")
        self.assertEqual(row["provider_display_name"], "Team")
        self.assertEqual(row["avatar"], {"kind": "builtin", "name": "openai"})
        for avatar in ("https://example.com/a.png", "file:///private", "../../secret", "builtin:unknown"):
            raw["providers"]["test"]["presentation"]["avatar"] = avatar
            with self.assertRaises(ValueError):
                ModelSettings.parse(raw)


class AvatarResourceTest(ConfigurationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.resources = Resources(self.root)
        self.files = ConfigurationFiles(
            self.entry, self.root / "private", self.validate, resources=self.resources
        )
        self.raw = presentation.normalize(image_bytes())
        self.ident = presentation.digest(self.raw)
        self.resource = {
            "id": "model-avatar-" + self.ident,
            "op": "set",
            "value": base64.b64encode(self.raw).decode(),
        }
        registry = settings()
        registry["providers"]["test"]["presentation"] = {
            "avatar": "asset:" + self.ident,
            "display_name": "Team",
        }
        self.changes = [
            {"path": ["model_api", key], "op": "set", "value": value} for key, value in registry.items()
        ]

    def test_image_and_reference_save_atomically_and_preview_is_bounded(self):
        version = self.files.snapshot().version
        with self.assertRaises(ValueError):
            self.files.patch(version, self.changes)
        self.assertEqual(self.files.snapshot().version, version)
        self.files.patch(version, self.changes, resource_changes=[self.resource])
        self.assertEqual(presentation.read(self.root, self.ident), self.raw)
        snapshot = self.files.snapshot()
        self.assertEqual(snapshot.resources[self.resource["id"]], self.raw)
        public = self.resources.public(snapshot.contents, snapshot.resources)
        item = next(item for item in public if item["id"] == self.resource["id"])
        self.assertNotIn("value", item)
        path = self.resources.frozen_target(
            self.root / "frozen", self.resources.registered(snapshot.contents)[self.resource["id"]]
        )
        self.assertNotIn(":", path.name)
        self.assertNotIn(base64.b64encode(self.raw).decode(), json.dumps(self.files.history()))

    def test_disk_failure_rolls_back_config_and_preserves_credentials(self):
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=original\n")
        before = self.files.snapshot().version
        failed = False

        def fail_once(path, content):
            nonlocal failed
            if Path(path).suffix == ".png" and not failed:
                failed = True
                raise OSError("synthetic disk failure")
            return atomic(path, content)

        with (
            patch("assistant_runtime.core.configuration.files.atomic", side_effect=fail_once),
            self.assertRaises(OSError),
        ):
            self.files.patch(before, self.changes, resource_changes=[self.resource])
        self.assertEqual(self.files.snapshot().version, before)
        self.assertEqual((self.root / ".env").read_text(), "DEEPSEEK_API_KEY=original\n")

    def test_corrupt_or_path_like_resources_are_rejected(self):
        bad = {**self.resource, "value": base64.b64encode(b"not an image").decode()}
        with self.assertRaises(ValueError):
            self.files.patch(self.files.snapshot().version, self.changes, resource_changes=[bad])
        with self.assertRaises(ValueError):
            presentation.read(self.root, "../credentials")

    def test_public_reader_only_serves_applied_references_and_exact_versions(self):
        self.files.patch(self.files.snapshot().version, self.changes, resource_changes=[self.resource])
        plugin = object.__new__(ModelsPlugin)
        raw = settings()
        raw["providers"]["test"]["presentation"] = {"avatar": "asset:" + self.ident}
        plugin.settings = ModelSettings.parse(raw)
        plugin.host = SimpleNamespace(config=SimpleNamespace(root=self.root))
        result = asyncio.run(
            plugin.handle(ModelRequest("avatar_read", {"asset_id": self.ident, "version": self.ident}))
        )
        self.assertEqual(base64.b64decode(result["data_base64"]), self.raw)
        with self.assertRaises(ValueError):
            asyncio.run(
                plugin.handle(ModelRequest("avatar_read", {"asset_id": self.ident, "version": "wrong"}))
            )
        plugin.settings = ModelSettings.parse(settings())
        with self.assertRaises(ValueError):
            asyncio.run(
                plugin.handle(ModelRequest("avatar_read", {"asset_id": self.ident, "version": self.ident}))
            )


class AvatarHTTPTest(ConfigurationFixture, unittest.IsolatedAsyncioTestCase):
    async def test_admin_origin_and_revision_protect_upload_and_preview(self):
        from aiohttp.test_utils import TestClient, TestServer
        from aiohttp import web
        from assistant_runtime.core.configuration.console import Console
        from assistant_runtime.core.configuration.auth import AdminAccess

        resources = Resources(self.root)
        files = ConfigurationFiles(self.entry, self.root / "private", self.validate, resources=resources)
        console = object.__new__(Console)
        console.files = files
        console.business = SimpleNamespace(settings=SimpleNamespace(root=self.root))
        console.access = AdminAccess(self.root / "admin")
        app = web.Application(middlewares=[console.guard], client_max_size=3 * 1024 * 1024)
        app.add_routes(
            [
                web.post("/api/login", console.login),
                web.post("/api/model-avatar", console.model_avatar_upload),
                web.get("/api/model-avatars", console.model_avatar_catalog),
                web.get("/api/model-avatar/builtin/{name}", console.model_avatar_builtin),
                web.get("/api/model-avatar/{asset_id}", console.model_avatar_read),
            ]
        )
        async with TestClient(TestServer(app)) as client:
            origin = str(client.make_url("/")).rstrip("/")
            self.assertEqual((await client.get("/api/model-avatars")).status, 401)
            self.assertEqual((await client.get("/api/model-avatar/builtin/openai")).status, 401)
            payload = {
                "version": files.snapshot().version,
                "data_base64": base64.b64encode(image_bytes()).decode(),
            }
            self.assertEqual(
                (await client.post("/api/model-avatar", json=payload, headers={"Origin": origin})).status, 401
            )
            await client.post(
                "/api/login",
                json={"token": (self.root / "admin").read_text().strip()},
                headers={"Origin": origin},
            )
            catalog = await (await client.get("/api/model-avatars")).json()
            item = next(item for item in catalog["avatars"] if item["name"] == "openai")
            url = "/api/model-avatar/builtin/openai?version=" + item["version"]
            icon = await client.get(url)
            self.assertEqual(icon.status, 200)
            self.assertIn("image/svg+xml", icon.headers["Content-Type"])
            self.assertEqual(
                (await client.get(url, headers={"If-None-Match": icon.headers["ETag"]})).status, 304
            )
            self.assertEqual((await client.get("/api/model-avatar/builtin/openai?version=stale")).status, 400)
            self.assertEqual(
                (
                    await client.post(
                        "/api/model-avatar", json=payload, headers={"Origin": "https://evil.example"}
                    )
                ).status,
                403,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/model-avatar", json={**payload, "version": "stale"}, headers={"Origin": origin}
                    )
                ).status,
                409,
            )
            response = await client.post("/api/model-avatar", json=payload, headers={"Origin": origin})
            self.assertEqual(response.status, 200)
            uploaded = await response.json()
            self.assertTrue(uploaded["avatar"].startswith("asset:"))
            self.assertFalse((self.root / "config/model-avatars").exists())
            self.assertEqual(
                (
                    await client.get(
                        "/api/model-avatar/" + uploaded["avatar"][6:] + "?version=" + uploaded["avatar"][6:]
                    )
                ).status,
                400,
            )
