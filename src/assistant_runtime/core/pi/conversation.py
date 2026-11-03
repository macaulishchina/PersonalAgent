"""Translate provider-specific events into the shared conversation vocabulary."""

import json

from assistant_runtime.core.pi.tool_display import tool_display


def tool_name(item):
    return item.get("tool", "")


def tool_media_receipt(result):
    details = result.get("details") if isinstance(result, dict) else None
    receipt = details.get("personal_agent.tool_media") if isinstance(details, dict) else None
    if (
        isinstance(receipt, dict)
        and receipt.get("state") in {"ready", "partial", "failed"}
        and isinstance(receipt.get("attachments"), list)
    ):
        return receipt
    return None


def image_content(result, *, task_result=False):
    """Normalize native and JSON-wrapped MCP media; preserve durable asset refs."""
    images, attachments = [], []
    receipt = tool_media_receipt(result)
    if receipt:
        return [], receipt["attachments"][:4]

    def visit(value, depth=0):
        if depth > 6 or not isinstance(value, dict):
            return
        if isinstance(value.get("attachments"), list):
            attachments.extend(value["attachments"][:4])
        elif task_result and isinstance(value.get("runs"), list) and value["runs"]:
            run = value["runs"][0]
            data = run.get("data") if isinstance(run, dict) else None
            if isinstance(data, dict) and isinstance(data.get("attachments"), list):
                attachments.extend(data["attachments"][:4])
        if value.get("type") == "image":
            images.append(
                {
                    "data_base64": value.get("data"),
                    "mime_type": value.get("mimeType") or value.get("mime_type"),
                }
            )
        elif value.get("type") == "text":
            text = value.get("text", "")
            if isinstance(text, str) and len(text) <= 16 * 1024 * 1024 and text.lstrip().startswith("{"):
                try:
                    visit(json.loads(text), depth + 1)
                except (ValueError, RecursionError):
                    pass
        if isinstance(value.get("content"), list):
            for block in value["content"][:32]:
                visit(block, depth + 1)
        if not attachments:
            visit(value.get("structuredContent"), depth + 1)

    visit(result)
    return images[:4], attachments[:4]


def task_event(method, params):
    turn = params.get("run_id") or params.get("run", {}).get("id")
    item = params.get("item", {})
    ident = item.get("id") or params.get("activity_id")
    common = {"turn_id": turn, "message_id": ident}
    if method == "run.started":
        return {**common, "kind": "turn_started"}
    if method == "model.retry":
        return {**common, "kind": "model_retry", "attempt": params.get("attempt")}
    if method == "run.completed":
        return {
            "result_ref": params.get("result_ref"),
            "result_preview": params.get("result"),
            "result_delivery_error": params.get("result_delivery_error"),
            "result_messages": params.get("result_messages", []),
            **common,
            "kind": "turn_completed",
            "status": params["run"].get("status", "unknown"),
            "error_status": (params["run"].get("error") or {}).get("status"),
            "error_code": (params["run"].get("error") or {}).get("code"),
            "failure": params["run"].get("failure"),
        }
    if method == "message.delta" and ident:
        return {**common, "kind": "message_delta", "delta": params.get("delta", "")}
    if method not in ("activity.started", "activity.completed") or not ident:
        return None
    kind = item.get("type")
    if kind == "input" and method == "activity.completed":
        content = item.get("content", [])
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text"}
        )
        return {**common, "kind": "execution_input", "text": text[:1000]}
    labels = {
        "command": "执行命令",
        "file": "修改文件",
        "mcp": "调用工具",
        "search": "检索资料",
    }
    if kind == "message":
        text, detail = item.get("text", ""), False
    elif kind in labels:
        value = (
            item.get("output")
            or item.get("command")
            or tool_name(item)
            or item.get("changes")
            or item.get("error")
            or ""
        )
        text, detail = labels[kind] + "\n" + str(value)[:12000], True
    else:
        return None
    status = (
        "streaming"
        if method == "activity.started"
        else ("failed" if item.get("status") in ("failed", "declined") else "complete")
    )
    event = {**common, "kind": "message", "text": text, "detail": detail, "status": status}
    if kind == "message":
        event["phase"] = item.get("phase") or "final"
    if kind == "mcp" and method == "activity.completed":
        raw_result = item.get("result")
        delivery = raw_result.get("details", {}) if isinstance(raw_result, dict) else {}
        if isinstance(delivery, dict):
            event["result_ref"] = (delivery.get("personal_agent.result") or {}).get("reference")
        receipt = tool_media_receipt(item.get("result"))
        if receipt:
            event["media_delivery"] = receipt
        display = tool_display(item)
        if display:
            event["tool_summary"] = display
            if display["outcome"] == "failed":
                event["status"] = "failed"
        images, attachments = image_content(
            item.get("result"), task_result=item.get("server") == "tasks" and item.get("tool") == "get_task"
        )
        if images:
            event["images"] = images
        if attachments:
            event["attachments"] = attachments
    return event
