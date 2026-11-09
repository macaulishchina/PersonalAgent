"""Build bundled plugin management pages from their declared package locations."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owners", nargs="*", help="Build only these owners, or all bundled pages")
    args = parser.parse_args()
    runtime = ROOT / "src" / "assistant_runtime"
    owners = json.loads((runtime / "owners.json").read_text(encoding="utf-8"))
    unknown = set(args.owners) - owners.keys()
    if unknown:
        parser.error(f"Unknown bundled plugins: {', '.join(sorted(unknown))}")
    for name in args.owners or owners:
        package = ROOT.joinpath("src", *owners[name].split("."))
        descriptor = package / "configuration.json"
        if not descriptor.is_file():
            continue
        page = json.loads(descriptor.read_text(encoding="utf-8")).get("interface", {}).get("page")
        if page is None:
            continue
        ui = package / "ui"
        if (ui / "package.json").is_file():
            npm = shutil.which("npm")
            if npm is None:
                parser.exit(1, "npm is required to build bundled plugin management pages.\n")
            print(f"Building management page for {name}", flush=True)
            subprocess.run([npm, "ci", "--ignore-scripts"], cwd=ui, check=True)
            subprocess.run([npm, "run", "build"], cwd=ui, check=True)
        if not (package / page["root"] / page["entry"]).is_file():
            raise RuntimeError(f"Management build did not produce the declared entry: {name}")


if __name__ == "__main__":
    main()
