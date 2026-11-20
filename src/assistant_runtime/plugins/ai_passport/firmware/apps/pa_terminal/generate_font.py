"""Generate the terminal's text and battery fonts for LVGL 9.5.0."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
FIRMWARE = ROOT.parents[1]


def generate(converter):
    executable = shutil.which(converter)
    if executable is None:
        raise FileNotFoundError(converter)
    converter = str(Path(executable).resolve())
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
    faces = (
        ("terminal_zh_16", 16, ("-r", "0x20-0x7e"), symbols),
        ("terminal_zh_18", 18, ("-r", "0x20-0x7e"), symbols),
        ("terminal_battery_12", 12, (), "0123456789-"),
    )
    for name, size, ranges, glyphs in faces:
        output = target / f"{name}.c"
        subprocess.run(
            [
                converter,
                "--font",
                str(Path("../..") / font.relative_to(FIRMWARE)),
                *ranges,
                "--symbols",
                glyphs,
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
                name,
                "-o",
                str(output.relative_to(ROOT)),
            ],
            cwd=ROOT,
            check=True,
        )
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
