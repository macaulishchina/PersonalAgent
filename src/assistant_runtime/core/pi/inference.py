"""One-shot native model transport, shared by auxiliary inference and memory."""

import time
from contextlib import aclosing

from assistant_runtime.api.models import ModelCallError
from assistant_runtime.core.pi.native_telemetry import telemetry
from assistant_runtime.core.pi.accounts import output_reservation


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
    worker, _, bound = await accounts.route(
        payload["model"], payload["reasoning"]["effort"], images=args["images"], schema=args["schema"]
    )
    return output_reservation(worker, payload["max_output_tokens"], bound)


async def complete(accounts, payload, session_id, *, meter=None):
    args = arguments(payload, session_id, accounts.settings)
    worker, route, _ = await accounts.route(
        payload["model"], payload["reasoning"]["effort"], images=args["images"], schema=args["schema"]
    )
    args.update(route)
    started, first, status, request_id, message = time.monotonic(), None, None, None, None
    timings = None
    args["inference"] = True
    if meter:
        args["retry"] = accounts.settings.retry
    async with aclosing(worker.events("stream", args, on_attempt=meter.gate if meter else None)) as events:
        async for item in events:
            event = item.get("event", {})
            kind = event.get("type")
            if meter and kind:
                meter.observe(event)
            if kind == "response":
                status, request_id = event["status"], event.get("request_id")
            elif kind == "text_delta" and first is None:
                first = time.monotonic() - started
            elif kind == "error":
                status = event.get("http_status", status)
                problem = event.get("failure") or event.get("error", {}).get("failure") or {}
                raise ModelCallError(
                    "model_http_" + str(status)
                    if status and status != 200
                    else problem.get("code", "model_unknown"),
                    problem,
                )
            elif kind == "done":
                message = event["message"]
                timings = event.get("transport_timings")
                request_id = request_id or message.get("responseId")

    def invalid_result(reason):
        return ModelCallError(
            "model_result_invalid",
            {
                "code": "model_result_invalid",
                "category": "result",
                "message": reason,
                "phase": "result_processing",
                "submission": "response_received",
                "generation_settled": bool(message),
                "retry_allowed": False,
                **({"request_id": request_id} if request_id else {}),
            },
        )

    if not message or message["stopReason"] != "stop":
        raise invalid_result("供应商已返回结果，但未确认完整文字回复；没有重新生成。")
    if any(part["type"] not in {"text", "thinking"} for part in message["content"]):
        raise invalid_result("辅助推理返回了不允许执行的工具调用；工具未执行，没有重新生成。")
    text = "".join(part["text"] for part in message["content"] if part["type"] == "text")
    if not text or len(text) > 128000:
        raise invalid_result("模型返回的文字为空或超过本地结果大小限制；没有重新生成。")
    return {
        "text": text,
        "usage": telemetry(message),
        "provider_request_id": request_id,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "first_text_ms": round(first * 1000) if first is not None else None,
        "transport_timings": timings,
        **(
            {"usage_call_id": meter.attempts[-1]["call_id"], "request_attempts": meter.attempts}
            if meter and meter.attempts
            else {}
        ),
    }
