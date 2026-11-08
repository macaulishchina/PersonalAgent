"""Generate the terminal's GB2312 Chinese + punctuation and ASCII font, LVGL 9.5.0."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
FIRMWARE = ROOT.parents[1]


def generate(converter):
    if subprocess.check_output([converter, "--version"], text=True).strip() != "1.5.3":
        raise ValueError("Use lv_font_conv 1.5.3")
    font = FIRMWARE / "managed_components/lvgl__lvgl/scripts/built_in_font/SourceHanSansSC-Normal.otf"
    symbols = set()
    for high in range(0xA1, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                symbols.update(bytes([high, low]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    symbols.update("".join(p.read_text() for p in (ROOT / "main").glob("*.c")))
    symbols = "".join(sorted(c for c in symbols if ord(c) > 127))
    target = ROOT / "main/fonts"
    for size in (16, 18):
        subprocess.run(
            [
                converter,
                "--font",
                str(font),
                "-r",
                "0x20-0x7e",
                "--symbols",
                symbols,
                "--size",
                str(size),
                "--bpp",
                "2",
                "--no-compress",
                "--format",
                "lvgl",
                "--lv-include",
                "lvgl.h",
                "--lv-font-name",
                f"terminal_zh_{size}",
                "-o",
                str(target / f"terminal_zh_{size}.c"),
            ],
            check=True,
        )
        output = target / f"terminal_zh_{size}.c"
        output.write_text(output.read_text().rstrip() + "\n")
    (target / "LICENSE.txt").write_text(
        (font.parent / "font_license/SourceHanSansSC/LICENSE.txt").read_text()
    )
    (target / "source.json").write_text(
        json.dumps(
            {
                "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
                "lvgl": "9.5.0",
                "converter": "1.5.3",
                "sizes": [16, 18],
                "bpp": 2,
                "characters": symbols,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--converter", required=True)
    generate(parser.parse_args().converter)
