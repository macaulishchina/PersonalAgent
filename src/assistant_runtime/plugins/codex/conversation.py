"""Translate provider-specific events into the shared conversation vocabulary."""

import json


def tool_name(item):
    """Use precise actions when projecting historical overloaded MCP calls."""
    name = item.get("tool", "")
    actions = {
        "device": {
            "room": "select_audio_output",
            "volume": "set_audio_volume",
            "stop": "stop_playback",
            "sleep": "end_voice_conversation",
            "progress": "get_task_status",
            "cancel": "cancel_task",
            "list": "list_devices",
            "inspect": "inspect_device",
            "snapshot": "capture_image",
        },
        "memory": {
            "search": "search_memory",
            "read": "read_memory",
            "remember": "remember_memory",
            "forget": "forget_memory",
        },
    }
    if name not in actions:
        return name
    arguments = item.get("arguments", {})
    if isinstance(arguments, str) and len(arguments) <= 16000:
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return name
    if not isinstance(arguments, dict):
        return name
    action = arguments.get("action")
    if not isinstance(action, str):
        return name
    if name == "memory" and action == "remember":
        values = arguments.get("arguments")
        if isinstance(values, dict) and values.get("id"):
            return "update_memory"
    return actions[name].get(action, name)


def image_content(result, *, task_result=False):
    """Normalize native and JSON-wrapped MCP media; preserve durable asset refs."""
    images, attachments = [], []

    def visit(value, depth=0):
        if depth > 6 or not isinstance(value, dict):
            return
        if isinstance(value.get("attachments"), list):
            attachments.extend(value["attachments"][:4])
        elif task_result and isinstance(value.get("runs"), list) and value["runs"]:
            # Restore already-recorded get_task replies from their run-owned refs,
            # without recapturing or treating prose/paths as media.
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
        # Native structured content duplicates the text payload; prefer text refs.
        if not attachments:
            visit(value.get("structuredContent"), depth + 1)

    visit(result)
    return images[:4], attachments[:4]


def task_event(method, params):
    turn = params.get("turnId") or params.get("turn", {}).get("id")
    item = params.get("item", {})
    ident = item.get("id") or params.get("itemId")
    common = {"turn_id": turn, "message_id": ident}
    if method == "turn/started":
        return {**common, "kind": "turn_started"}
    if method == "turn/completed":
        return {**common, "kind": "turn_completed", "status": params["turn"].get("status", "unknown")}
    if method == "item/agentMessage/delta" and ident:
        return {**common, "kind": "message_delta", "delta": params.get("delta", "")}
    if method not in ("item/started", "item/completed") or not ident:
        return None
    kind = item.get("type")
    labels = {
        "commandExecution": "执行命令",
        "fileChange": "修改文件",
        "mcpToolCall": "调用工具",
        "webSearch": "检索资料",
    }
    if kind == "agentMessage":
        text, detail = item.get("text", ""), False
    elif kind in labels:
        value = (
            item.get("aggregatedOutput")
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
        if method == "item/started"
        else ("failed" if item.get("status") in ("failed", "declined") else "complete")
    )
    event = {**common, "kind": "message", "text": text, "detail": detail, "status": status}
    if kind == "mcpToolCall" and method == "item/completed":
        images, attachments = image_content(
            item.get("result"), task_result=item.get("server") == "tasks" and item.get("tool") == "get_task"
        )
        if images:
            event["images"] = images
        if attachments:
            event["attachments"] = attachments
    return event


def voice_event(data):
    kind = data.get("type")
    if kind not in ("turn.created", "turn.delta", "turn.done"):
        return None
    turn = data.get("turn", {})
    return {
        "kind": "message_delta" if kind == "turn.delta" else "message",
        "message_id": turn.get("id") or data.get("turn_id"),
        "role": turn.get("role"),
        "text": turn.get("transcript", ""),
        "delta": data.get("delta", ""),
        "status": "complete" if kind == "turn.done" else "streaming",
    }
