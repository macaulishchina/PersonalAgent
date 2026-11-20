"""Bounded, speech-only projection of text already assigned to the terminal."""

import re
import unicodedata

CHARACTER_LIMIT = 200
_SENTENCE_END = re.compile(r"[。！？!?；;\n]+|\.(?=\s|$)")
_PAUSE = re.compile(r"[，、：,:]|\s")
_LINK = re.compile(r"!?\[([^\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_URL = re.compile(r"https?://[^\s<>。！？！，、；]+")
_MARKUP = re.compile(r"(?m)^\s*(?:#{1,6}|>|[-+*])\s+|(?:`+|\*+|~~|__|\|)")


def readable_text(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _MARKUP.sub(" ", text)
    text = "".join(ch if _readable_character(ch) else " " for ch in text)
    speech = " ".join(text.split())
    return speech if any(ch.isalnum() for ch in speech) else ""


def _readable_character(ch: str) -> bool:
    if ch.isspace() or ch in "℃℉№©®™":
        return True
    category = unicodedata.category(ch)
    return not category.startswith("C") and category not in {"So", "Sk"} and not 0xFE00 <= ord(ch) <= 0xFE0F


def source_prefix(text: str, limit: int, *, complete: bool, end_of_span: bool) -> int:
    """Return a safe raw offset, including formatting omitted from the speech projection."""
    if complete and end_of_span:
        return len(text)
    boundaries = [match.end() for match in _SENTENCE_END.finditer(text)]
    if boundaries:
        return boundaries[-1]
    if complete and len(text) == limit:
        pauses = [match.end() for match in _PAUSE.finditer(text) if match.start() >= limit // 2]
        return pauses[-1] if pauses else len(text)
    return 0
