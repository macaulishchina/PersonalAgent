"""Bounded tool metadata for execution tracing; never copy tool arguments/results."""

import json
import time


class TaskTrace:
    def __init__(self, repo):
        self.repo, self.db = repo, repo.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS job_tool_trace (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          call_id TEXT NOT NULL, phase TEXT NOT NULL, at REAL NOT NULL, metadata TEXT NOT NULL,
          UNIQUE(run_id,call_id,phase));
        CREATE INDEX IF NOT EXISTS job_tool_trace_run ON job_tool_trace(run_id,seq);
        CREATE TABLE IF NOT EXISTS job_trace_limits (run_id TEXT PRIMARY KEY, truncated INTEGER NOT NULL);
        """)

    def append(self, run, value):
        allowed = {"call_id", "phase", "kind", "server", "tool", "status", "operation_id", "device_id"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid execution trace metadata")
        if any(not isinstance(v, str) or len(v) > 300 for v in value.values()):
            raise ValueError("Invalid execution trace metadata value")
        if value.get("phase") not in {"started", "completed"} or not value.get("call_id"):
            raise ValueError("Trace requires call identity and phase")
        with self.db:
            if self.db.execute(
                "SELECT 1 FROM job_tool_trace WHERE run_id=? AND call_id=? AND phase=?",
                (run, value["call_id"], value["phase"]),
            ).fetchone():
                return
            if (
                self.db.execute("SELECT count(*) FROM job_tool_trace WHERE run_id=?", (run,)).fetchone()[0]
                >= 500
            ):
                self.db.execute(
                    "INSERT INTO job_trace_limits VALUES (?,1) ON CONFLICT(run_id) DO UPDATE SET truncated=1",
                    (run,),
                )
                return
            self.db.execute(
                "INSERT INTO job_tool_trace(run_id,call_id,phase,at,metadata) VALUES (?,?,?,?,?)",
                (run, value["call_id"], value["phase"], time.time(), json.dumps(value)),
            )

    def read(self, args):
        run = self.repo.run(args["run_id"])
        after, limit = args.get("after", 0), args.get("limit", 50)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Trace requires nonnegative cursor and limit 1..100")
        rows = self.db.execute(
            "SELECT seq,at,metadata FROM job_tool_trace WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (run["id"], after, limit + 1),
        ).fetchall()
        truncated = self.db.execute(
            "SELECT truncated FROM job_trace_limits WHERE run_id=?", (run["id"],)
        ).fetchone()
        return {
            "run_id": run["id"],
            "task_id": run["task_id"],
            "state": run["state"],
            "trigger_context": run["data"].get("trigger_context"),
            "thread_id": run["data"].get("thread_id"),
            "turn_id": run["data"].get("turn_id"),
            "items": [{"seq": row[0], "at": row[1], **json.loads(row[2])} for row in rows[:limit]],
            "next": rows[limit - 1][0] if len(rows) > limit else None,
            "truncated": bool(truncated),
            "outcome_scope": "provider_reported",
        }
