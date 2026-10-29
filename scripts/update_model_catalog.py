"""Explicitly refresh the packaged models.dev snapshot; never runs at application startup."""

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from assistant_runtime.core.models.reference_catalog import URL, compile_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Use a previously downloaded JSON for reproducibility")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src/assistant_runtime/core/models/model_catalog.json",
    )
    args = parser.parse_args()
    if args.source:
        raw = args.source.read_bytes()
    else:
        with urlopen(
            Request(URL, headers={"User-Agent": "personal-agent-catalog/1.0"}), timeout=30
        ) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Model catalog exceeds 16 MiB")
    result = compile_catalog(raw)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Prepared {len(result['models'])} model records; review the diff before release")


if __name__ == "__main__":
    main()
