"""Host-only checks: a package must never guess offsets or include NVS."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "tools/package_firmware.py"
spec = importlib.util.spec_from_file_location("ppdemo_package", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackageTest(unittest.TestCase):
    def test_unexpected_nvs_offset_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "project_description.json").write_text(
                json.dumps(
                    {
                        "target": "esp32c3",
                        "project_name": "ppdemo",
                        "project_version": "0.2.0",
                        "idf_ver": "v5.5.3",
                    }
                )
            )
            (root / "flasher_args.json").write_text(
                json.dumps(
                    {
                        "flash_files": {
                            "0x0": "bootloader.bin",
                            "0x8000": "partition-table.bin",
                            "0x9000": "nvs.bin",
                            "0x10000": "ppdemo.bin",
                        }
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "NVS overwrite"):
                module.package(root, root / "result.zip")
            self.assertFalse((root / "result.zip").exists())

    def test_wrong_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "project_description.json").write_text(
                json.dumps(
                    {
                        "target": "esp32s3",
                        "project_name": "ppdemo",
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "ESP32-C3"):
                module.package(root, root / "result.zip")
            self.assertFalse((root / "result.zip").exists())


if __name__ == "__main__":
    unittest.main()
