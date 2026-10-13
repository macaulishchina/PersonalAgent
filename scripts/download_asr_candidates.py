"""Download pinned official ASR checkpoints for local-only CPU evaluation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download


MODELS = {
    "paraformer": ("funasr/paraformer-zh-streaming", "fd2af606b37d7fb8b3b8a218c5be5b07b53ef6ba"),
    "qwen": ("Qwen/Qwen3-ASR-0.6B-hf", "7f1569a48a89f3e3f4dc3a5c9d28bddd903bc76c"),
}


def download(name, root):
    repo, revision = MODELS[name]
    destination = root / name
    print(f"Downloading {name}: {repo}@{revision}", flush=True)
    snapshot_download(
        repo,
        revision=revision,
        local_dir=destination,
        max_workers=2,
        ignore_patterns=["fig/*", "example/*", ".gitattributes"],
    )
    hashes = {}
    for path in sorted(destination.iterdir()):
        if path.is_file() and path.name != "download-metadata.json":
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(4 * 1024 * 1024):
                    digest.update(block)
            hashes[path.name] = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    (destination / "download-metadata.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "revision": revision,
                "files": hashes,
            },
            indent=2,
        )
    )
    print(f"Ready {name}: {sum(x['bytes'] for x in hashes.values()) / 1024**2:.1f} MiB", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("models/asr-candidates"))
    args = p.parse_args()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda name: download(name, args.root), MODELS))


if __name__ == "__main__":
    main()
