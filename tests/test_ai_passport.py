"""No-feature plugin lifecycle and independently maintained firmware skeleton."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from assistant_runtime.core.plugin_catalog import metadata


class AiPassportTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_enable_is_unavailable_without_tools_or_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            host = AssistantService(Config(root=Path(folder), plugins=["ai_passport"]))
            try:
                await host.runtime.start()
                health = host.runtime.health()["ai_passport"]
                self.assertEqual(health["state"], "degraded")
                self.assertIn("ai_passport_scaffold_only", health["error"])
                self.assertEqual(set(host.runtime.plugins), {"ai_passport"})
                self.assertEqual(host.runtime.handlers, {})
                self.assertFalse(host.runtime.contexts["ai_passport"].tasks)
                self.assertFalse(host.runtime.contexts["ai_passport"].calls)
                plugin = host.runtime.plugins["ai_passport"]
                self.assertEqual(plugin.instance.diagnostics()["state"], "unavailable")
                self.assertEqual(plugin.public.config, {})
                self.assertFalse(hasattr(plugin.public, "host"))
            finally:
                await host.runtime.close()
                host.store.db.close()
                host.lock_file.close()

    async def test_disabled_catalog_and_presets_do_not_import_plugin(self):
        code = """
import sys
from assistant_runtime.core.configuration.catalog import catalog
items = catalog()['plugins']
assert any(item['id'] == 'ai_passport' for item in items)
assert 'assistant_runtime.plugins.ai_passport.plugin' not in sys.modules
assert 'assistant_runtime.plugins.ai_passport.declaration' not in sys.modules
"""
        # Inspect templates as data; importing or enabling the extension is unnecessary.
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)

        root = Path(__file__).resolve().parents[1] / "src/assistant_runtime/app/templates"
        for preset in ("slim", "standard"):
            values = tomllib.loads((root / preset / "config.toml").read_text())
            self.assertNotIn("ai_passport", values["plugins"])


class FirmwareProjectTest(unittest.TestCase):
    def test_layout_and_pinned_driver_checksums(self):
        root = metadata("ai_passport").parent / "firmware"
        upstream = json.loads((root / "upstream.json").read_text())
        self.assertRegex(upstream["commit"], r"^[a-f0-9]{40}$")
        self.assertTrue((root / upstream["license_file"]).is_file())
        import hashlib

        hashes = json.loads((root / "bsp.sha256.json").read_text())
        self.assertTrue(hashes)
        for name, expected in hashes.items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), expected, name)
        end = 0
        for line in (root / "partitions.csv").read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            _, _, _, offset, size, *_ = (value.strip() for value in line.split(","))
            offset, size = int(offset, 0), int(size, 0)
            self.assertGreaterEqual(offset, end)
            self.assertGreater(size, 0)
            end = offset + size
        self.assertEqual(end, 8 * 1024 * 1024)
