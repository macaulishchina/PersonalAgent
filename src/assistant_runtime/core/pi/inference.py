"""One-shot native model transport, shared by auxiliary inference and memory."""

import time
from contextlib import aclosing

from assistant_runtime.api.models import ModelCallError
from assistant_runtime.core.pi.native_transport import telemetry


def arguments(payload, session_id, settings):
    context = []
    images = False
    for item in payload["input"]:
        content = []
        for part in item["content"]:
            if part["type"] == "input_text":
                content.append({"type": "text", "text": part["text"]})
            elif part["type"] == "input_image":
                prefix, data = part["image_url"].split(",", 1)
                if not prefix.startswith("data:image/") or not prefix.endswith(";base64"):
                    raise ValueError("Native inference requires registered inline images")
                content.append({"type": "image", "mimeType": prefix[5:-7], "data": data})
                images = True
            else:
                raise ValueError("Unsupported native inference input")
        context.append({"role": "user", "content": content, "timestamp": 0})
    return {
        "context": {"messages": context},
        "images": images,
        "schema": payload.get("text", {}).get("format", {}).get("schema"),
        "max_tokens": payload["max_output_tokens"],
        "timeout_ms": settings.timeout_seconds * 1000,
        "session_id": session_id,
    }


async def output_bound(accounts, payload):
    args = arguments(payload, None, accounts.settings)
    _, _, bound = await accounts.route(
        payload["model"], payload["reasoning"]["effort"], images=args["images"], schema=args["schema"]
    )
    return bound or payload["max_output_tokens"]


async def complete(accounts, payload, session_id):
    args = arguments(payload, session_id, accounts.settings)
    worker, route, _ = await accounts.route(
        payload["model"], payload["reasoning"]["effort"], images=args["images"], schema=args["schema"]
    )
    args.update(route)
    started, first, status, request_id, message = time.monotonic(), None, None, None, None
    args["inference"] = True
    async with aclosing(worker.events("stream", args)) as events:
        async for item in events:
            event = item.get("event", {})
            kind = event.get("type")
            if kind == "response":
                status, request_id = event["status"], event.get("request_id")
            elif kind == "text_delta" and first is None:
                first = time.monotonic() - started
            elif kind == "error":
                status = event.get("http_status", status)
                if status and status != 200:
                    raise ModelCallError(
                        "model_http_" + str(status),
                        fallback_allowed=status in {401, 402, 403, 404, 408, 429, 500, 502, 503, 504},
                    )
                raise RuntimeError("Native model outcome unknown or authentication unavailable")
            elif kind == "done":
                message = event["message"]
                request_id = request_id or message.get("responseId")
    if not message or message["stopReason"] != "stop":
        raise RuntimeError("Native model did not confirm complete text")
    if any(part["type"] not in {"text", "thinking"} for part in message["content"]):
        raise ValueError("Auxiliary inference cannot request or execute tools")
    text = "".join(part["text"] for part in message["content"] if part["type"] == "text")
    if not text or len(text) > 128000:
        raise ValueError("Missing or oversized native model text")
    return {
        "text": text,
        "usage": telemetry(message),
        "provider_request_id": request_id,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "first_text_ms": round(first * 1000) if first is not None else None,
    }
