"""Explicitly refresh the packaged models.dev snapshot; never runs at application startup."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

URL = "https://models.dev/api.json"


def compile_catalog(raw):
    models = {}
    for provider, spec in json.loads(raw).items():
        for ident, row in spec.get("models", {}).items():
            value = {}
            for source, target in (
                ("tool_call", "tools"),
                ("reasoning", "reasoning"),
                ("structured_output", "structured"),
            ):
                if type(row.get(source)) is bool:
                    value[target] = row[source]
            inputs = row.get("modalities", {}).get("input")
            if isinstance(inputs, list):
                value["images"] = "image" in inputs
            for source, target in (("context", "context_window"), ("output", "output_limit")):
                n = row.get("limit", {}).get(source)
                if type(n) is int and 1 <= n <= 10000000:
                    value[target] = n
            # Read explicit effort choices only. Toggle and token budgets are different controls.
            efforts = []
            for option in (
                row.get("reasoning_options", []) if isinstance(row.get("reasoning_options"), list) else []
            ):
                if (
                    not isinstance(option, dict)
                    or option.get("type") != "effort"
                    or not isinstance(option.get("values"), list)
                ):
                    continue
                for effort in option["values"]:
                    if (
                        effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                        and effort not in efforts
                    ):
                        efforts.append(effort)
            if efforts and row.get("reasoning") is True:
                value["reasoning_efforts"] = efforts
            canonical = row.get("canonical_model_id")
            if (
                isinstance(canonical, str)
                and len(canonical) <= 512
                and not any(c.isspace() or ord(c) < 32 for c in canonical)
                and canonical.partition("/")[0]
                and canonical.partition("/")[2]
            ):
                value["canonical_model_id"] = canonical
            if value:
                models[provider + "/" + ident] = value
    if not models:
        raise ValueError("Empty model catalog")
    return {
        "source": URL,
        "revision": hashlib.sha256(raw).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
    }


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
