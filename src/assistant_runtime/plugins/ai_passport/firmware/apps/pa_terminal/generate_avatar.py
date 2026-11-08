"""Offline conversion of the approved Demo character, never run on the device.

Usage: python generate_avatar.py --open cat-companion-v2.png --blink cat-companion-blink.png
The generated native RGB565+A8 Flash assets need no PNG decoder or runtime scaling.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct

from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", type=Path, required=True)
    parser.add_argument("--blink", type=Path, required=True)
    args = parser.parse_args()
    directory = Path(__file__).parent / "main/assets"
    directory.mkdir(exist_ok=True)
    metadata = {"format": "rgb565a8", "source": {}, "images": {}}
    for state, path in (("open", args.open), ("blink", args.blink)):
        metadata["source"][state] = {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, size in (("idle", (120, 144)), ("input", (60, 72))):
            image = Image.open(path).convert("RGBA").resize(size, Image.Resampling.LANCZOS)
            rgba = image.tobytes()
            pixels = [rgba[i : i + 4] for i in range(0, len(rgba), 4)]
            rgb = b"".join(
                struct.pack("<H", (r >> 3) << 11 | (g >> 2) << 5 | b >> 3) for r, g, b, _ in pixels
            )
            raw = rgb + bytes(a for _, _, _, a in pixels)
            target = directory / f"cat_{name}_{state}.rgb565a8"
            target.write_bytes(raw)
            metadata["images"][target.name] = {
                "width": size[0],
                "height": size[1],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
    (directory / "avatar.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
