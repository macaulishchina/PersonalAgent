"""Wire-level contracts of PA-Terminal protocol 2 (ESP32-C3, no PSRAM)."""

import hashlib
import json
import struct

from assistant_runtime.api.speech import SpeechError

VERSION = 2
MAX_JSON_BYTES = 16 * 1024
INPUT_RATE = 16000
OUTPUT_RATE = 24000
PLAYBACK_WINDOW = 8192
PLAYBACK_MAX_WINDOW = 24 * 1024
PLAYBACK_CHUNK = 4096
IMAGE_CHUNK = 2048
IMAGE_STRIP_ROWS = 8
IMAGE_STRIP_WIDTH = 216
IMAGE_STRIP_HEIGHT = 194
IMAGE_STRIP_TAG = b"\xff\xff\xff\xff"
INPUT_AUDIO_ID_TAG = b"\xff\xff\xff\xff"
INPUT_AUDIO_ID_BYTES = 16

_COMMAND_FIELDS = {
    "input.begin": {"request_id", "mode", "audio_ids"},
    "input.finish": {"request_id", "frames", "confirmation_required"},
    "input.confirm": {"request_id"},
    "input.cancel": {"request_id"},
    "input.receipt": {"request_id"},
    "input.transcript": {"request_id", "offset"},
    "playback.stop": set(),
    "playback.progress": {"generation", "i2s_bytes"},
    "playback.credit": {"generation", "bytes"},
    "playback.completed": {"generation", "pcm"},
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
    "image.stream.open": {"request_id", "message_id", "image_id", "width", "height"},
    "image.stream.next": {"request_id", "row"},
    "image.stream.close": {"request_id"},
}

_PLAYBACK_PCM_FIELDS = {
    "window_bytes",
    "received_bytes",
    "received_crc32",
    "i2s_bytes",
    "i2s_crc32",
    "underruns",
}


def playback_pcm(data):
    if (
        not isinstance(data, dict)
        or data.keys() != _PLAYBACK_PCM_FIELDS
        or any(type(n) is not int or not 0 <= n <= 0xFFFFFFFF for n in data.values())
        or not PLAYBACK_WINDOW <= data["window_bytes"] <= PLAYBACK_MAX_WINDOW
    ):
        raise ValueError("Invalid playback PCM report")
    return data


def image_strip(request_id, row, rows, pixels):
    digest = hashlib.sha256(pixels).digest()[:8]
    return struct.pack("!4sIHH8s", IMAGE_STRIP_TAG, request_id, row, rows, digest) + pixels


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
