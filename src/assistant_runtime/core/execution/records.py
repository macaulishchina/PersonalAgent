"""Durable single executions and bounded, cursor-addressable evidence."""

import json
import time

ACTIVE = frozenset({"queued", "dispatching", "running", "waiting_user", "cancelling"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown", "interrupted"})


class RunRecords:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_runs(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_run_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_run_event_cursor ON agent_run_events(run_id,sequence);
        """)

    def create(self, ident, owner, fingerprint, data):
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO agent_runs VALUES(?,?,?,?,?,?,?)",
                (ident, owner, fingerprint, "queued", json.dumps(data), now, now),
            )
            self.event(ident, "queued", {})
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute(
            "SELECT id,owner,fingerprint,state,data,created,updated FROM agent_runs WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown execution")
        result = dict(zip(("id", "owner", "fingerprint", "state", "data", "created", "updated"), row))
        result["data"] = json.loads(result["data"])
        return result

    def find(self, ident):
        return (
            self.get(ident)
            if self.db.execute("SELECT 1 FROM agent_runs WHERE id=?", (ident,)).fetchone()
            else None
        )

    def update(self, ident, state=None, **changes):
        row = self.get(ident)
        state = state or row["state"]
        if row["state"] in TERMINAL:
            return row  # Late provider events cannot reopen or overwrite a settled result.
        if state not in ACTIVE | TERMINAL:
            raise ValueError("Invalid execution state")
        data = {**row["data"], **changes}
        if state in TERMINAL:
            data.update(questions=[], question_id=None)
        with self.db:
            self.db.execute(
                "UPDATE agent_runs SET state=?,data=?,updated=? WHERE id=?",
                (state, json.dumps(data), time.time(), ident),
            )
            if state != row["state"]:
                self.event(ident, state, {})
        return self.get(ident)

    def event(self, ident, kind, data):
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode()) > 16384:
            encoded = json.dumps({"truncated": True})
        self.db.execute(
            "INSERT INTO agent_run_events(run_id,kind,data,created) VALUES(?,?,?,?)",
            (ident, kind, encoded, time.time()),
        )
        self.db.execute(
            "DELETE FROM agent_run_events WHERE run_id=? AND sequence NOT IN "
            "(SELECT sequence FROM agent_run_events WHERE run_id=? ORDER BY sequence DESC LIMIT 512)",
            (ident, ident),
        )

    def events(self, ident, cursor=0, limit=100):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid execution event cursor or limit")
        first = self.db.execute(
            "SELECT min(sequence) FROM agent_run_events WHERE run_id=?", (ident,)
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT sequence,kind,data,created FROM agent_run_events WHERE run_id=? AND sequence>? "
            "ORDER BY sequence LIMIT ?",
            (ident, cursor, limit),
        ).fetchall()
        return {
            "items": [
                {"cursor": r[0], "kind": r[1], "data": json.loads(r[2]), "created": r[3]} for r in rows
            ],
            "cursor": rows[-1][0] if rows else cursor,
            "earliest_cursor": first,
        }

    def active(self):
        return [
            self.get(row[0])
            for row in self.db.execute(
                "SELECT id FROM agent_runs WHERE state IN ('queued','dispatching','running','waiting_user','cancelling')"
            ).fetchall()
        ]

    def recover(self):
        for row in self.active():
            self.update(
                row["id"],
                "interrupted" if row["state"] == "queued" else "unknown",
                error={"code": "service_interrupted", "message": "Execution was not replayed"},
            )
