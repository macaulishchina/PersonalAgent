"""Public supplementary messages, distinct from model-visible delivery evidence."""

import asyncio
import json
import uuid
from dataclasses import asdict

from assistant_runtime.api.plugins import current_call
from .records import TERMINAL


async def deliver(service, request, row):
    args, kind = request.arguments, request.action
    try:
        ident = str(uuid.UUID(args["message_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Execution input requires a stable UUID message_id") from None
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 32768:
        raise ValueError("A bounded nonempty execution input is required")
    content = {"kind": kind, "text": text, "question_id": args.get("question_id")}
    messages = service.records.messages
    prior, fingerprint = messages.existing(ident, row["id"], content)
    if prior:
        return messages.view(prior)
    question = service.questions.get(row["id"])
    if kind == "answer" and (
        row["state"] != "waiting_user" or not question or question["id"] != args.get("question_id")
    ):
        raise ValueError("Question is no longer pending")
    if kind == "supplement" and row["state"] not in TERMINAL and row["state"] != "running":
        raise ValueError("Only a running execution accepts supplementary input")
    call = current_call.get()
    message = messages.create(
        ident,
        row["id"],
        fingerprint,
        {
            **content,
            "turn_id": row["data"].get("turn_id"),
            "caller": asdict(call) if call else None,
        },
    )
    if row["state"] in TERMINAL or service.stopping:
        with service.records.db:
            message = messages.update(ident, "not_consumed", reason="execution_ended")
        return messages.view(message)
    data = row["data"]
    session, turn = data["session_id"], data["turn_id"]
    # A host-generated marker avoids guessing receipt identity from repeated text.
    # It is input metadata, not an instruction, credential or an authorization.
    nonce = message["data"]["nonce"]
    if kind == "answer":
        payload = {
            "answers": {q["id"]: {"answers": [text]} for q in question["data"].get("questions", [])},
            "_pa_input_receipt": nonce,
        }
        forwarded = json.dumps(payload, ensure_ascii=False)
        role, tool_call_id = "toolResult", question["data"].get("tool_call_id")
    else:
        forwarded = f"[PA input receipt: {nonce}]\n{text}"
        role, tool_call_id = "user", None
    try:
        service.client.track_input(session, turn, ident, role, forwarded, tool_call_id)
    except Exception:
        with service.records.db:
            message = messages.update(ident, "not_consumed", reason="input_tracking_failed")
        return messages.view(message)
    with service.records.db:
        messages.update(ident, dispatch_attempted=True)
    try:
        if kind == "answer":
            await service.client.respond(question["id"], payload)
            if service.questions.get(row["id"]) is question:
                service.questions.pop(row["id"], None)
                service.records.update(row["id"], "running", questions=[], question_id=None)
        else:
            await service.client.request(
                "run.steer",
                {
                    "session_id": session,
                    "expected_run_id": turn,
                    "input": [{"type": "text", "text": forwarded}],
                },
            )
    except BaseException as exc:
        with service.records.db:
            messages.update(ident, "unknown", reason="dispatch_unconfirmed")
        if isinstance(exc, asyncio.CancelledError):
            raise
    return messages.view(messages.get(ident))
