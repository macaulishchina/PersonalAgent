"""Private date archives; all IO runs on the existing receipt worker thread."""

from datetime import datetime, date
import json
import os
from zoneinfo import ZoneInfo


def append(directory, timezone, entry):
    folder = directory / "archive"
    folder.mkdir(mode=0o700, exist_ok=True)
    day = datetime.fromtimestamp(entry["at"], ZoneInfo(timezone)).date().isoformat()
    path = folder / (day + ".jsonl")
    raw = (json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(raw) > 65536:
        raise ValueError("Decision archive entry too large")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as out:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            out.write(b"\n")  # Preserve a torn tail without corrupting the next complete record.
        out.write(raw)
        out.flush()


def page(directory, day, offset=0, limit=50, **filters):
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Archive date must be YYYY-MM-DD")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid archive page")
    if set(filters) - {"kind", "decision_id", "operation_id", "status"}:
        raise ValueError("Unknown archive filter")
    path = directory / "archive" / (day + ".jsonl")
    if not path.exists():
        return {"items": [], "next_offset": None}
    items, scanned = [], 0
    with path.open("rb") as stream:
        stream.seek(offset)
        while scanned < 4 * 1024 * 1024 and len(items) < limit:
            raw = stream.readline(65537)
            if not raw:
                break
            scanned += len(raw)
            if len(raw) > 65536:
                raise ValueError("Invalid archive record")
            try:
                entry = json.loads(raw)
            except ValueError:
                continue  # A torn final record is not evidence of a completed decision.
            if isinstance(entry, dict) and all(entry.get(k) == v for k, v in filters.items()):
                items.append(entry)
        cursor = stream.tell()
        more = bool(stream.read(1))
    return {"items": items, "next_offset": cursor if more else None}
