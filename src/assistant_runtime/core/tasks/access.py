"""Read-only task provenance for sibling core services; tasks owns this schema."""

import json
import re


def notification_origin(db, caller):
    if caller:
        row = db.execute("SELECT task_id,data FROM job_runs WHERE id=?", (caller,)).fetchone()
        if not row:
            raise ValueError("Unknown notification task execution")
        run_data = json.loads(row[1])
        origin = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='job_conversation_origins'").fetchone():
            origin = db.execute(
                "SELECT session_id FROM job_conversation_origins WHERE task_id=?", (row[0],)
            ).fetchone()
        return "task:" + row[0], {
            **({"conversation_id": origin[0]} if origin else {}),
            "task_id": row[0],
            "run_id": caller,
            "task_title": run_data["spec"]["title"],
            "reports_completion": run_data["spec"].get("notify_completion", True)
            and run_data.get("finished_at") is None
            and not run_data.get("completion_delivered", False),
            **(
                {"duration_seconds": run_data["duration_seconds"]}
                if run_data.get("finished_at") is not None and run_data.get("duration_seconds") is not None
                else {}
            ),
        }
    return "assistant", {}


def completion_identity(key):
    match = re.fullmatch(r"task-result:([a-f0-9]{32}):([0-9]+)", key or "")
    return match.groups() if match else None


def project_conversation_results(db, messages):
    """Show completed conversational handoffs as answers, using the saved run snapshot."""
    from .repository import category

    candidates = {}
    for message in messages:
        if message.get("role") != "assistant" or not message.get("task_id"):
            continue
        if message.get("kind") == "task":
            identity = completion_identity(message.get("turn_id"))
            if not identity:
                continue
            run_id, round_id = identity
        elif message.get("kind") == "notification":
            group = message.get("report_group", {})
            run_id, round_id = group.get("run_id"), group.get("completion_round")
            if not run_id or round_id is None:
                continue
        else:
            continue
        candidates.setdefault(run_id, []).append((round_id, message))
    if not candidates:
        return
    placeholders = ",".join("?" for _ in candidates)
    rows = db.execute(
        f"SELECT id,task_id,occurrence,data FROM job_runs WHERE state='completed' AND id IN ({placeholders})",
        tuple(candidates),
    )
    for run_id, task_id, occurrence, raw in rows:
        run = json.loads(raw)
        spec = run.get("spec") or {}
        if (
            occurrence.startswith("manual:")
            or not spec.get("trigger", {}).get("kind")
            or category(spec) != "temporary"
            or spec.get("parent_id")
            or spec.get("execution", {}).get("kind") != "agent"
            or (run.get("business_result") or {}).get("state") not in (None, "completed")
        ):
            continue
        for round_id, message in candidates[run_id]:
            if message["task_id"] == task_id and round_id == str(run.get("completion_round", 0)):
                message["presentation"] = {**(message.get("presentation") or {}), "task_reply": True}
