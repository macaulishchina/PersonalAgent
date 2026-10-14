"""Translate provider-specific events into the shared conversation vocabulary."""


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
            or item.get("tool")
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
    return {**common, "kind": "message", "text": text, "detail": detail, "status": status}


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
