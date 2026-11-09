"""Verify the single release source, bundled descriptors and optional built wheel."""

import argparse
from pathlib import Path
import sys
import json
import tomllib


def validate(root, wheel=None):
    from assistant_runtime.app.versions import CORE_VERSION, framework, compatibility, check_dependencies
    from assistant_runtime.core.plugin_catalog import OWNERS, release

    root = Path(root)
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata["tool"]["setuptools"]["dynamic"]["version"] != {"file": "src/assistant_runtime/VERSION"}:
        raise ValueError("Package version must derive from the framework VERSION file")
    records = {name: release(name) for name in OWNERS}
    for name, data in records.items():
        if data["id"] != name or compatibility(data):
            raise ValueError("Bundled plugin release is incompatible: " + name)
    check_dependencies(records, list(records))
    configurations = {}
    for name, module in OWNERS.items():
        relative = Path(*module.split(".")) / "configuration.json"
        source = root / "src" / relative
        if source.is_file():
            configurations[relative] = source.read_bytes()
        if module.startswith("assistant_runtime.plugins."):
            if (
                relative not in configurations
                or json.loads(configurations[relative]).get("namespace") != f"plugin_settings.{name}"
            ):
                raise ValueError(f"Bundled extension must declare its own configuration scope: {name}")
    if list((root / "src/assistant_runtime").rglob("*.platform.json")) or list(
        (root / "src/assistant_runtime").rglob("platform.json")
    ):
        raise ValueError("Obsolete platform descriptors must not ship alongside plugin.json")
    from assistant_runtime.app.versions import read_descriptor

    sample_root = root / "examples/sample-plugin"
    sample = read_descriptor(sample_root / "src/personal_agent_sample/plugin.json")
    sample_project = tomllib.loads((sample_root / "pyproject.toml").read_text())
    if sample["version"] != sample_project["project"]["version"] or compatibility(sample):
        raise ValueError("Sample distribution and plugin release differ")
    if wheel:
        from assistant_runtime.app.release_package import read_wheel

        packaged = read_wheel(wheel, framework_package=True)
        if packaged["release"] != framework() or packaged["plugins"] != records:
            raise ValueError("Wheel release differs from source descriptors")
        from zipfile import ZipFile
        from assistant_runtime.core.configuration.pages import validate_page

        with ZipFile(wheel) as archive:
            packaged_files = set(archive.namelist())
            for descriptor, raw in configurations.items():
                if descriptor.as_posix() not in packaged_files or archive.read(descriptor.as_posix()) != raw:
                    raise ValueError(f"Wheel configuration differs from source: {descriptor}")
                package = descriptor.parent
                page = json.loads(raw).get("interface", {}).get("page")
                if page is None:
                    continue
                validate_page(page)
                directory = package / page["root"]
                resources = [p for p in (root / "src" / directory).rglob("*") if p.is_file()]
                expected = {(directory / page["entry"]).as_posix()}
                expected.update(p.relative_to(root / "src").as_posix() for p in resources)
                if not expected.issubset(packaged_files):
                    raise ValueError(f"Wheel is missing management page resources for {package}")
    return {
        "core_version": CORE_VERSION,
        "core_plugins": sum(m.startswith("assistant_runtime.core.") for m in OWNERS.values()),
        "extension_plugins": sum(m.startswith("assistant_runtime.plugins.") for m in OWNERS.values()),
        "wheel_checked": bool(wheel),
    }


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel")
    args = parser.parse_args()
    print(json.dumps(validate(root, args.wheel), indent=2))


if __name__ == "__main__":
    main()
