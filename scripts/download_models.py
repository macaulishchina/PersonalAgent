"""Download pinned upstream model assets; existing complete models are reused."""

import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / "models"
ASSETS = [
    (
        "kws",
        "kws-models",
        "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
        "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    ),
    ("", "asr-models", "silero_vad.onnx", "silero_vad.onnx"),
]


def main():
    ROOT.mkdir(exist_ok=True)
    for folder, release, name, check in ASSETS:
        dest = ROOT / folder
        dest.mkdir(exist_ok=True)
        if any(dest.rglob(check)):
            print(f"Already installed: {name}")
            continue
        url = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/{release}/{name}"
        partial = ROOT / (name + ".partial")
        print(f"Downloading {name}", flush=True)
        urllib.request.urlretrieve(url, partial)
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        if name.endswith(".tar.bz2"):
            with tarfile.open(partial, "r:bz2") as archive:
                archive.extractall(dest, filter="data")
            partial.unlink()
        else:
            partial.replace(dest / name)
        (ROOT / (name + ".metadata.json")).write_text(json.dumps({"url": url, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
