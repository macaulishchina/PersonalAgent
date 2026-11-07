"""Regenerate the checked-in Chinese subsets with lv_font_conv 1.5.3.

The source font and its license come from the locked LVGL 9.5.0 component.
Font generation is a developer command, never part of PA startup or device flashing.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FONT = Path("managed_components/lvgl__lvgl/scripts/built_in_font/SourceHanSansSC-Normal.otf")


def required_characters():
    text = "".join(p.read_text() for p in (ROOT / "main").glob("*.[ch]"))
    return "".join(sorted({c for c in text if ord(c) > 127}))


def generate(converter):
    version = subprocess.check_output([converter, "--version"], text=True).strip()
    if version != "1.5.3":
        raise ValueError("Use the pinned lv_font_conv 1.5.3")
    output = ROOT / "main/fonts"
    output.mkdir(exist_ok=True)
    characters = required_characters()
    for size in (14, 16, 20):
        subprocess.run(
            [
                converter,
                "--font",
                str(FONT),
                "-r",
                "0x20-0x7e",
                "--symbols",
                characters,
                "--size",
                str(size),
                "--bpp",
                "4",
                "--no-compress",
                "--format",
                "lvgl",
                "--lv-include",
                "lvgl.h",
                "--lv-font-name",
                f"ppdemo_zh_{size}",
                "-o",
                f"main/fonts/ppdemo_zh_{size}.c",
            ],
            cwd=ROOT,
            check=True,
        )
    license = ROOT / FONT.parent / "font_license/SourceHanSansSC/LICENSE.txt"
    (output / "LICENSE.txt").write_text(license.read_text())
    (output / "source.json").write_text(
        json.dumps(
            {
                "font": str(FONT),
                "sha256": hashlib.sha256((ROOT / FONT).read_bytes()).hexdigest(),
                "converter": "lv_font_conv",
                "version": version,
                "lvgl": "9.5.0",
                "sizes": [14, 16, 20],
                "characters": characters,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"Generated 3 fonts: ASCII plus {len(characters)} Chinese/punctuation glyphs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--converter", default="lv_font_conv")
    generate(parser.parse_args().converter)
