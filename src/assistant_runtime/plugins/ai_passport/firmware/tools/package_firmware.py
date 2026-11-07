"""Package the actual IDF outputs, without merging over the NVS partition."""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
import zipfile


def package(build: Path, destination: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    build = build.resolve()
    description = json.loads((build / "project_description.json").read_text())
    if description["target"] != "esp32c3" or description["project_name"] not in {"ppdemo", "pa_terminal"}:
        raise ValueError("Expected an actual ESP32-C3 PPDemo or PA terminal build")
    project = description["project_name"]
    app_root = root / "apps/pa_terminal" if project == "pa_terminal" else root
    args = json.loads((build / "flasher_args.json").read_text())
    files = {int(address, 0): name for address, name in args["flash_files"].items()}
    if set(files) != {0, 0x8000, 0x10000}:
        raise ValueError("Unexpected flash layout; never package a guessed NVS overwrite")
    configuration = json.loads((build / "config/sdkconfig.json").read_text())
    if configuration["IDF_INIT_VERSION"] != "5.5.3":
        raise ValueError("Expected the pinned ESP-IDF 5.5.3 build")
    images = {}
    manifest = {
        "application": "PA-Terminal"
        if project == "pa_terminal"
        else "PPDemo-Zh"
        if configuration.get("PPDEMO_LANGUAGE_ZH", False)
        else "PPDemo",
        "version": description["project_version"],
        "target": description["target"],
        "idf_version": configuration["IDF_INIT_VERSION"],
        "hardware_acceptance": "pending",
        "flash_files": [],
    }
    limits = {0: 0x8000, 0x8000: 0x1000, 0x10000: 0x7F0000}
    for offset, name in sorted(files.items()):
        path = (build / name).resolve()
        if not path.is_relative_to(build) or not path.is_file():
            raise ValueError("Image must belong to this build")
        data = path.read_bytes()
        if not data or len(data) > limits[offset]:
            raise ValueError("Image exceeds the declared partition")
        label = path.name
        if label in images:
            raise ValueError("Ambiguous image name")
        images[label] = data
        manifest["flash_files"].append(
            {
                "offset": hex(offset),
                "file": label,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    resources = {
        "README.md": app_root / "README.md",
        "docs/publishing-research.md": root / "docs/publishing-research.md",
        "LICENSE.folotoy": root / "LICENSE.folotoy",
        **(
            {
                "docs/acceptance.md": root.parent / "docs/acceptance.md",
                "docs/protocol.md": root.parent / "docs/protocol.md",
            }
            if project == "pa_terminal"
            else {}
        ),
        **(
            {"SourceHanSansSC.LICENSE.txt": app_root / "main/fonts/LICENSE.txt"}
            if project == "pa_terminal" or configuration.get("PPDEMO_LANGUAGE_ZH", False)
            else {}
        ),
        f"debug/{project}.elf": build / f"{project}.elf",
        f"debug/{project}.map": build / f"{project}.map",
        "build-info/dependencies.lock": app_root / "dependencies.lock",
        "build-info/sdkconfig": Path(description.get("config_file", root / "sdkconfig")),
    }
    if any(not path.is_file() for path in resources.values()):
        raise ValueError("Missing build evidence; no package generated")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        staged = Path(temp.name)
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in images.items():
                archive.writestr(name, data)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for name, path in resources.items():
                if name == "README.md" and project == "pa_terminal":
                    archive.writestr(
                        name, path.read_text().replace("../../../docs/acceptance.md", "docs/acceptance.md")
                    )
                else:
                    archive.write(path, name)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    print(destination.resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, default=Path("build"))
    parser.add_argument("--output", type=Path, default=Path("build/ppdemo-0.2.0.zip"))
    options = parser.parse_args()
    package(options.build, options.output)
