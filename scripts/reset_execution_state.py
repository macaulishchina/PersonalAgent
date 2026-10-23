"""Preview or archive/reset execution state. Preserve memory evidence and business stores."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3


@contextmanager
def stopped_instance(root):
    lock = root / "state/service.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Stop the assistant and verify in-flight operations before applying") from None
        yield


def execution_tables(db):
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [
        n
        for n in names
        if n.startswith(("job_", "chat_", "task_"))
        or n in {"tasks", "sessions", "feedback_requests", "feedback_intents"}
    ]


def reset(root, *, apply=False, confirmed_stopped=False):
    root = Path(root).resolve()
    path = root / "state/assistant.sqlite3"
    if not path.is_file():
        return {"state": "empty", "tables": {}}
    db = sqlite3.connect(f"file:{path}?mode={'rw' if apply else 'ro'}", uri=True)
    try:
        tables = execution_tables(db)
        counts = {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in tables}
        previous = db.execute("SELECT value FROM settings WHERE key='execution.generation'").fetchone()
        if previous and json.loads(previous[0]) == "pi-v1":
            return {"state": "already_reset", "tables": counts}
        active = 0
        if "job_runs" in tables:
            active = db.execute(
                "SELECT COUNT(*) FROM job_runs WHERE state IN ('dispatching','running','waiting_user','unknown')"
            ).fetchone()[0]
        if not apply:
            return {
                "state": "preview",
                "tables": counts,
                "unconfirmed_executions": active,
                "preserved": "transcripts, memory, media, events, credentials and physical files",
            }
        if not confirmed_stopped:
            raise ValueError(
                "Applying requires --confirmed-stopped after checking provider/device operations"
            )
        with stopped_instance(root):
            backup = (
                root
                / "state/backups"
                / ("before-pi-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            )
            backup.mkdir(parents=True, mode=0o700)
            target = sqlite3.connect(backup / "assistant.sqlite3")
            try:
                db.backup(target)
            finally:
                target.close()
            (backup / "assistant.sqlite3").chmod(0o600)
            if (root / "config").is_dir():
                shutil.copytree(root / "config", backup / "config")
                for file in (backup / "config").rglob("*"):
                    file.chmod(0o700 if file.is_dir() else 0o600)
            with db:
                # Retire only task-linked notices; retain unrelated notifications.
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "notifications" in names:
                    rows = db.execute("SELECT id,payload FROM notifications").fetchall()
                    for ident, payload in rows:
                        value = json.loads(payload)
                        relation = value.get("related") or {}
                        origin = value.get("origin") or {}
                        if (
                            value.get("task_id")
                            or relation.get("task_id")
                            or origin.get("task_id")
                            or origin.get("run_id")
                        ):
                            if "notification_deliveries" in names:
                                db.execute(
                                    "DELETE FROM notification_deliveries WHERE notification_id=?", (ident,)
                                )
                            db.execute("DELETE FROM notifications WHERE id=?", (ident,))
                for table in tables:
                    db.execute(f'DELETE FROM "{table}"')
                for key in ("thread_id", "session_id", "chat.legacy_imported", "pi.execution_session_id"):
                    db.execute("DELETE FROM settings WHERE key=?", (key,))
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES('execution.generation', ?)",
                    (json.dumps("pi-v1"),),
                )
            return {"state": "reset", "backup": str(backup), "tables": counts}
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--confirmed-stopped", action="store_true")
    args = p.parse_args()
    print(
        json.dumps(
            reset(args.root, apply=args.apply, confirmed_stopped=args.confirmed_stopped),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
