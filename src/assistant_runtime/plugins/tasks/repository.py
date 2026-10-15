"""Definitions, immutable run snapshots and delivery revisions in host SQLite."""

import json
import time
import uuid

ACTIVE = {"queued", "dispatching", "running", "waiting_user", "paused", "unknown"}
TERMINAL = {"completed", "failed", "canceled", "missed"}


class TaskRepository:
    def __init__(self, db):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS job_definitions (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
          fingerprint TEXT NOT NULL, state TEXT NOT NULL, spec TEXT NOT NULL, next_at REAL,
          created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS job_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, occurrence TEXT NOT NULL,
          state TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, delivered INTEGER NOT NULL DEFAULT 0,
          UNIQUE(task_id, occurrence));
        CREATE INDEX IF NOT EXISTS job_runs_state ON job_runs(state);
        CREATE TABLE IF NOT EXISTS job_event_cursor (task_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

    def definition(self, ident):
        row = self.db.execute(
            "SELECT id,state,spec,next_at,created,updated,revision FROM job_definitions WHERE id=?", (ident,)
        ).fetchone()
        if not row:
            raise ValueError("Task not found")
        return dict(
            zip(
                ("id", "state", "spec", "next_at", "created", "updated", "revision"),
                (row[0], row[1], json.loads(row[2]), *row[3:]),
            )
        )

    def definitions(self):
        return [
            self.definition(r[0])
            for r in self.db.execute("SELECT id FROM job_definitions ORDER BY created DESC")
        ]

    def run(self, ident):
        row = self.db.execute(
            "SELECT id,task_id,occurrence,state,data,created,updated,revision,delivered FROM job_runs WHERE id=?",
            (ident,),
        ).fetchone()
        if not row:
            raise ValueError("Execution not found")
        return dict(
            zip(
                (
                    "id",
                    "task_id",
                    "occurrence",
                    "state",
                    "data",
                    "created",
                    "updated",
                    "revision",
                    "delivered",
                ),
                (*row[:4], json.loads(row[4]), *row[5:]),
            )
        )

    def runs(self, task_id=None, limit=100):
        rows = self.db.execute(
            "SELECT id FROM job_runs WHERE (? IS NULL OR task_id=?) ORDER BY created DESC LIMIT ?",
            (task_id, task_id, limit),
        )
        return [self.run(r[0]) for r in rows]

    def active_runs(self, task_id=None):
        placeholders = ",".join("?" for _ in ACTIVE)
        rows = self.db.execute(
            f"SELECT id FROM job_runs WHERE (? IS NULL OR task_id=?) AND state IN ({placeholders}) ORDER BY created DESC",
            (task_id, task_id, *sorted(ACTIVE)),
        )
        return [self.run(row[0]) for row in rows]

    def pending(self):
        rows = self.db.execute(
            "SELECT id FROM job_runs WHERE state IN ('queued','dispatching','running','waiting_user','unknown','paused') OR delivered<revision ORDER BY created"
        )
        return [self.run(row[0]) for row in rows]

    def enqueue(self, definition, occurrence, state="queued", additions=None):
        ident = uuid.uuid4().hex
        data = {
            "spec": definition["spec"],
            "definition_revision": definition["revision"],
            "progress": "",
            **(additions or {}),
        }
        self.db.execute(
            "INSERT OR IGNORE INTO job_runs(id,task_id,occurrence,state,data,created,updated) VALUES(?,?,?,?,?,?,?)",
            (
                ident,
                definition["id"],
                occurrence,
                state,
                json.dumps(data, ensure_ascii=False),
                time.time(),
                time.time(),
            ),
        )
        row = self.db.execute(
            "SELECT id FROM job_runs WHERE task_id=? AND occurrence=?", (definition["id"], occurrence)
        ).fetchone()
        return self.run(row[0])

    def update_run(self, ident, state=None, **changes):
        r = self.run(ident)
        data = {**r["data"], **changes}
        with self.db:
            self.db.execute(
                "UPDATE job_runs SET state=?,data=?,updated=?,revision=revision+1 WHERE id=?",
                (state or r["state"], json.dumps(data, ensure_ascii=False), time.time(), ident),
            )
        return self.run(ident)

    def update_definition(self, ident, **changes):
        d = self.definition(ident)
        d.update(changes)
        with self.db:
            self.db.execute(
                "UPDATE job_definitions SET state=?,spec=?,next_at=?,updated=?,revision=revision+1 WHERE id=?",
                (d["state"], json.dumps(d["spec"], ensure_ascii=False), d["next_at"], time.time(), ident),
            )
        return self.definition(ident)
