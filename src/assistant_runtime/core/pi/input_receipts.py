"""Host receipt correlation at the actual Pi model-input boundary."""

import hashlib


def content_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def track(client, session, turn, ident, role, text, tool_call_id=None):
    worker = client._worker(session)
    if worker.active_turn != turn:
        raise ValueError("Input belongs to another execution turn")
    pending = worker.meta.setdefault("public_inputs", {})
    if len(pending) >= 64:
        raise RuntimeError("Execution input tracking queue is full")
    pending[ident] = {
        "turn_id": turn,
        "role": role,
        "digest": content_digest(text),
        "tool_call_id": tool_call_id,
    }
    client._save_meta(session)


def consumed(client, worker, context, model_request):
    if not getattr(client, "admission", None) or worker.compacting:
        return
    pending = worker.meta.get("public_inputs", {})
    found = []
    for ident, tracked in pending.items():
        if tracked["turn_id"] != worker.active_turn:
            continue
        # A tool result without its native call ID is insufficient evidence.
        if tracked["role"] == "toolResult" and not tracked.get("tool_call_id"):
            continue
        for message in context["messages"]:
            if not isinstance(message, dict) or message.get("role") != tracked["role"]:
                continue
            if tracked.get("tool_call_id") and message.get("toolCallId") != tracked["tool_call_id"]:
                continue
            content = message.get("content", [])
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                continue
            if content_digest(text) == tracked["digest"]:
                found.append(ident)
                break
    if found:
        client.admission.inputs_consumed(
            worker.execution_session_id, worker.active_turn, model_request, found
        )
        for ident in found:
            pending.pop(ident, None)
        client._save_meta(worker.execution_session_id)
