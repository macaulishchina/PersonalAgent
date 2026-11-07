"""Host-only checks: a package must never guess offsets or include NVS."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

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

    def test_language_identity_and_actual_configuration_are_packaged(self):
        for project, chinese in (("ppdemo", False), ("ppdemo", True), ("pa_terminal", True)):
            with self.subTest(project=project, chinese=chinese), tempfile.TemporaryDirectory() as folder:
                build = Path(folder)
                config = build / "zh-sdkconfig"
                config.write_text("actual build configuration")
                (build / "project_description.json").write_text(
                    json.dumps(
                        {
                            "target": "esp32c3",
                            "project_name": project,
                            "project_version": "0.2.0",
                            "config_file": str(config),
                        }
                    )
                )
                (build / "flasher_args.json").write_text(
                    json.dumps(
                        {
                            "flash_files": {
                                "0x0": "bootloader.bin",
                                "0x8000": "partition-table.bin",
                                "0x10000": project + ".bin",
                            }
                        }
                    )
                )
                (build / "config").mkdir()
                (build / "config/sdkconfig.json").write_text(
                    json.dumps(
                        {
                            "IDF_INIT_VERSION": "5.5.3",
                            "PPDEMO_LANGUAGE_ZH": chinese,
                        }
                    )
                )
                for name in (
                    "bootloader.bin",
                    "partition-table.bin",
                    project + ".bin",
                    project + ".elf",
                    project + ".map",
                ):
                    (build / name).write_bytes(b"synthetic build evidence")
                output = build / "candidate.zip"
                module.package(build, output)
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(
                        manifest["application"],
                        "PA-Terminal" if project == "pa_terminal" else "PPDemo-Zh" if chinese else "PPDemo",
                    )
                    if project == "pa_terminal":
                        self.assertIn("docs/acceptance.md", archive.namelist())
                        self.assertNotIn("../../../docs", archive.read("README.md").decode())
                    self.assertEqual(manifest["hardware_acceptance"], "pending")
                    self.assertEqual(archive.read("build-info/sdkconfig"), config.read_bytes())
                    self.assertEqual("SourceHanSansSC.LICENSE.txt" in archive.namelist(), chinese)

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
