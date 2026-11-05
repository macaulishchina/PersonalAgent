"""A proven native clarification boundary, never a replay of unfinished tools."""

import copy
import hashlib
import json
import time
from pathlib import Path

from assistant_runtime.api.models import CapabilityRequestError
from .context import native_context


def pending(worker):
    checkpoint = worker.meta.get("input_checkpoint") or {}
    return checkpoint if checkpoint.get("state") == "pending" else None


async def capture(worker, event):
    """Persist the proof before publishing a recoverable question."""
    if not worker.meta.get("recover_user_input"):
        return
    data, meta = event["data"], worker.meta
    call = data.get("tool_call_id")
    checkpoint = {"id": event["id"], "state": "unavailable", "code": "waiting_boundary_incomplete"}
    admission = worker.parent.admission
    if set(meta.get("pending_operations", {})) == {call} and (
        not admission or admission.can_suspend(worker.execution_session_id, call)
    ):
        try:
            proof = await native_context(
                worker.parent.pi_binary, action="waiting_checkpoint", path=meta["path"], call_id=call
            )
            if proof.get("available") and proof.get("questions") == data.get("questions"):
                checkpoint = {
                    "id": event["id"],
                    "state": "pending",
                    "code": "waiting_checkpoint_ready",
                    "turn_id": worker.active_turn,
                    "call_id": call,
                    "created": time.time(),
                    "questions": data["questions"],
                    "leaf": proof["leaf"],
                    "digest": proof["digest"],
                    "size": proof["size"],
                    "model": meta["model"],
                    "binding": meta["model_binding"],
                    "effort": meta["reasoning_effort"],
                    "budget_id": meta["budget_id"],
                }
        except Exception:
            checkpoint["code"] = "waiting_checkpoint_storage_error"
    meta["input_checkpoint"] = checkpoint
    worker.parent._save_meta(worker.execution_session_id)
    data["recovery"] = recovery(checkpoint)


def recovery(checkpoint, *, restored=False):
    ready = checkpoint.get("state") == "pending"
    return {
        "state": "waiting_user" if ready else "unknown",
        "code": checkpoint.get("code", "waiting_checkpoint_unavailable"),
        "question_id": checkpoint.get("id"),
        "restored": restored,
        "actions": ["answer", "cancel_run"] if ready else ["reconcile_run"],
        "safe_to_retry": False,
    }


def validate(client, worker, question_id=None, *, check_model=True):
    checkpoint, meta = pending(worker), worker.meta
    if not checkpoint or (question_id is not None and checkpoint["id"] != question_id):
        raise CapabilityRequestError("question_expired", "Question has no recoverable pending checkpoint")
    if (
        meta.get("active_run") != checkpoint["turn_id"]
        or set(meta.get("pending_operations", {})) != {checkpoint["call_id"]}
        or meta.get("budget_id") != checkpoint["budget_id"]
        or any(i.get("tool_outcome", {}).get("outcome") == "unknown" for i in meta.get("current_items", []))
    ):
        raise CapabilityRequestError(
            "waiting_boundary_incomplete", "Waiting execution has unresolved operations"
        )
    try:
        with Path(meta["path"]).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != checkpoint["digest"] or Path(meta["path"]).stat().st_size != checkpoint["size"]:
            raise CapabilityRequestError(
                "waiting_checkpoint_corrupt", "Waiting history changed or is corrupt"
            )
    except FileNotFoundError as exc:
        raise CapabilityRequestError(
            "waiting_checkpoint_unavailable", "Waiting history is unavailable"
        ) from exc
    except OSError as exc:
        raise CapabilityRequestError(
            "waiting_checkpoint_storage_error", "Waiting history cannot be read"
        ) from exc
    if check_model:
        if client.admission and not client.admission.can_suspend(
            worker.execution_session_id, checkpoint["call_id"]
        ):
            raise CapabilityRequestError(
                "waiting_boundary_incomplete", "Other execution operations are unresolved"
            )
        try:
            client._selection(checkpoint["model"], checkpoint["effort"])
            if client._binding(checkpoint["model"]) != checkpoint["binding"] or (
                meta["model"],
                meta["reasoning_effort"],
            ) != (checkpoint["model"], checkpoint["effort"]):
                raise ValueError("binding changed")
        except ValueError as exc:
            raise CapabilityRequestError(
                "waiting_model_unavailable", "The waiting task's model binding is unavailable"
            ) from exc
    if not Path(meta["cwd"]).is_dir():
        raise CapabilityRequestError(
            "waiting_resource_unavailable", "The waiting task's work area is unavailable"
        )
    return checkpoint


def restore(client, session, turn):
    worker = client._worker(session)
    if not pending(worker):
        checkpoint = worker.meta.get("input_checkpoint") or {}
        code = (
            "waiting_answer_unconfirmed"
            if checkpoint.get("state") == "answering"
            else checkpoint.get("code", "waiting_checkpoint_unavailable")
        )
        raise CapabilityRequestError(code, "Waiting execution has no complete pending checkpoint")
    checkpoint = validate(client, worker, check_model=False)
    if checkpoint["turn_id"] != turn:
        raise CapabilityRequestError("question_expired", "Question belongs to another execution turn")
    event = {
        "id": checkpoint["id"],
        "kind": "input.requested",
        "data": {
            "session_id": session,
            "run_id": turn,
            "questions": checkpoint["questions"],
            "tool_call_id": checkpoint["call_id"],
            "recovery": recovery(checkpoint, restored=True),
        },
    }
    client.ui_requests[event["id"]] = (worker, None, "input.requested")
    return copy.deepcopy(event)


async def resume(client, worker, checkpoint, result):
    """Called only after durable answer intent and execution admission."""
    meta = worker.meta
    text = json.dumps(result, ensure_ascii=False)
    appended = await native_context(
        client.pi_binary,
        action="waiting_answer",
        path=meta["path"],
        call_id=checkpoint["call_id"],
        leaf=checkpoint["leaf"],
        digest=checkpoint["digest"],
        text=text,
    )
    if not appended.get("appended"):
        raise CapabilityRequestError(
            "waiting_boundary_incomplete", "The native wait can no longer accept an answer"
        )
    meta["pending_operations"].pop(checkpoint["call_id"])
    if client.admission:
        client.admission.operation(worker.execution_session_id, "tool", checkpoint["call_id"], "completed")
    worker.active_turn = checkpoint["turn_id"]
    worker.turn_items = copy.deepcopy(meta.get("current_items", []))
    client._write_configuration(meta)
    client._save_meta(worker.execution_session_id)
    await worker.start()
    state = await worker.command({"type": "get_state"})
    from .rpc import THINKING_MAP

    if (state.get("model") or {}).get("id") != checkpoint["model"] or state.get(
        "thinkingLevel"
    ) != THINKING_MAP[checkpoint["effort"]]:
        raise CapabilityRequestError("waiting_model_unavailable", "Pi selected a different model or effort")
    await worker.command(
        {
            "type": "prompt",
            "message": "Continue from the recorded answer to request_user_input. Do not repeat completed operations.",
        },
        timeout=client.settings.outer_timeout_seconds,
    )
