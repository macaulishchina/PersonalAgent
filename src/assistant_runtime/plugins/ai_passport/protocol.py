"""Wire-level contracts of PA-Terminal protocol 2 (ESP32-C3, no PSRAM)."""

import json

from assistant_runtime.api.speech import SpeechError

VERSION = 2
MAX_JSON_BYTES = 16 * 1024
INPUT_RATE = 16000
OUTPUT_RATE = 24000
PLAYBACK_WINDOW = 8192
PLAYBACK_CHUNK = 4096
IMAGE_CHUNK = 2048

_COMMAND_FIELDS = {
    "input.begin": {"request_id", "mode"},
    "input.finish": {"request_id", "frames", "confirmation_required"},
    "input.confirm": {"request_id"},
    "input.cancel": {"request_id"},
    "input.receipt": {"request_id"},
    "input.transcript": {"request_id", "offset"},
    "playback.stop": set(),
    "playback.credit": {"generation", "bytes"},
    "playback.completed": {"generation"},
    "message.page": {"message_id", "offset", "prefetch"},
    "history.index": {"before", "after", "limit"},
    "execution.cancel": {"request_id"},
    "notifications.list": {"before", "request_id"},
    "notification.receipt": {"id", "revision", "action"},
    "notification.page": {"id", "revision", "offset"},
    "configuration.reload": set(),
    "configuration.applied": {"versions", "actual"},
    "image.open": {"message_id", "image_id", "width", "height"},
    "image.chunk": {"version", "offset"},
    "image.close": set(),
}


def command_type(data):
    kind = data.get("type")
    if (
        not isinstance(kind, str)
        or kind not in _COMMAND_FIELDS
        or data.keys() - _COMMAND_FIELDS[kind] - {"type"}
    ):
        raise ValueError("Invalid terminal command")
    return kind


def encode_event(event_type, data):
    if event_type == "input.receipt" and "result" in data:
        result = data["result"]
        data = {
            **data,
            "result": {
                k: result[k] for k in ("state", "request_id") if isinstance(result, dict) and k in result
            },
        }
    # Measure exactly what aiohttp sends; its default JSON dumper escapes CJK.
    text = json.dumps({"type": event_type, **data}, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise SpeechError("terminal_message_limit")
    return text
