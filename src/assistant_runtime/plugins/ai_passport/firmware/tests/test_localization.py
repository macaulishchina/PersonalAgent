"""Check format safety and the actual generated glyph subset, without a device."""

import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("font_generator", ROOT / "tools/generate_fonts.py")
font = importlib.util.module_from_spec(spec)
spec.loader.exec_module(font)
STRING = r'"(?:\\.|[^"\\])*"'


class LocalizationTest(unittest.TestCase):
    def test_translated_format_arguments_match(self):
        count = 0
        pattern = re.compile(r"LAB_TEXT\((" + STRING + r"),\s*(" + STRING + r")\)")
        conversions = re.compile(r"%(?:[-+ #0]*\d*(?:\.\d+)?(?:hh|ll|[hljztL])?[diuoxXfFeEgGaAcspn]|%)")
        for path in (ROOT / "main").glob("*.c"):
            for en, zh in pattern.findall(path.read_text()):
                english, chinese = json.loads(en), json.loads(zh)
                self.assertEqual(conversions.findall(english), conversions.findall(chinese), english)
                self.assertLess(len(chinese.encode("utf-8")), 512)
                count += 1
        self.assertGreater(count, 60)

    def test_generated_fonts_cover_all_localized_characters(self):
        metadata = json.loads((ROOT / "main/fonts/source.json").read_text())
        self.assertEqual(
            metadata["characters"], font.required_characters(), "Regenerate fonts after changing Chinese text"
        )
        expected = set(map(ord, metadata["characters"])) | set(range(32, 127))
        for size in (14, 16, 20):
            text = (ROOT / f"main/fonts/ppdemo_zh_{size}.c").read_text()
            actual = {int(c, 16) for c in re.findall(r"/\* U\+([0-9A-Fa-f]+)", text)}
            self.assertFalse(expected - actual, (size, "".join(map(chr, sorted(expected - actual)))))
        self.assertIn("SIL OPEN FONT LICENSE", (ROOT / "main/fonts/LICENSE.txt").read_text())


if __name__ == "__main__":
    unittest.main()
