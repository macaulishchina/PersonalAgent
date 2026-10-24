"""Read-only task provenance for sibling core services; tasks owns this schema."""


def notification_origin(db, caller):
    if caller:
        row = db.execute("SELECT task_id,data FROM job_runs WHERE id=?", (caller,)).fetchone()
        if not row:
            raise ValueError("Unknown notification task execution")
        import json

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
