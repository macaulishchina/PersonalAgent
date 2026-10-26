"""Durable input intent and observed consumption; never replay an uncertain send."""

import hashlib
import json
import time
import uuid


class MessageRecords:
    def __init__(self, db, event):
        self.db, self.event = db, event
        db.executescript("""
            CREATE TABLE IF NOT EXISTS agent_run_messages(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_run_messages_run ON agent_run_messages(run_id);
        """)

    def get(self, ident):
        raw = self.db.execute(
            "SELECT id,run_id,fingerprint,state,data,created,updated FROM agent_run_messages WHERE id=?",
            (ident,),
        ).fetchone()
        if raw is None:
            return None
        row = dict(
            zip(("message_id", "execution_id", "fingerprint", "state", "data", "created", "updated"), raw)
        )
        row["data"] = json.loads(row["data"])
        return row

    @staticmethod
    def view(row):
        data = row["data"]
        return {key: row[key] for key in ("message_id", "execution_id", "state", "created", "updated")} | {
            key: data.get(key)
            for key in (
                "kind",
                "question_id",
                "turn_id",
                "model_request_id",
                "consumed_at",
                "reason",
                "caller",
            )
        }

    def existing(self, ident, run, content):
        row = self.get(ident)
        fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        if row and (row["execution_id"] != run or row["fingerprint"] != fingerprint):
            raise ValueError("Message ID reused with a different execution or content")
        return row, fingerprint

    def create(self, ident, run, fingerprint, data):
        pending = self.db.execute(
            "SELECT count(*) FROM agent_run_messages WHERE run_id=? AND state='accepted'", (run,)
        ).fetchone()[0]
        if pending >= 64:
            raise RuntimeError("Execution input queue is full")
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO agent_run_messages VALUES(?,?,?,'accepted',?,?,?)",
                (ident, run, fingerprint, json.dumps({**data, "nonce": uuid.uuid4().hex}), now, now),
            )
            self.event(run, "message.accepted", {"message_id": ident})
        return self.get(ident)

    def update(self, ident, state=None, **changes):
        row = self.get(ident)
        if row["state"] != "accepted":
            return row
        state = state or row["state"]
        data = {**row["data"], **changes}
        self.db.execute(
            "UPDATE agent_run_messages SET state=?,data=?,updated=? WHERE id=?",
            (state, json.dumps(data), time.time(), ident),
        )
        if state != row["state"]:
            self.event(
                row["execution_id"],
                "message." + state,
                {
                    "message_id": ident,
                    "turn_id": data.get("turn_id"),
                    "model_request_id": data.get("model_request_id"),
                    "reason": data.get("reason"),
                },
            )
        return self.get(ident)

    def finish(self, run, reason):
        # Called inside the run's terminal transaction. No response from the engine
        # can prove non-consumption once dispatch has been attempted.
        for (ident,) in self.db.execute(
            "SELECT id FROM agent_run_messages WHERE run_id=? AND state='accepted'", (run,)
        ).fetchall():
            row = self.get(ident)
            self.update(
                ident,
                "unknown" if row["data"].get("dispatch_attempted") else "not_consumed",
                reason=reason,
            )

    def consumed(self, run, turn, request, identities):
        with self.db:
            for ident in identities:
                row = self.get(ident)
                if (
                    row
                    and row["execution_id"] == run
                    and row["state"] == "accepted"
                    and row["data"].get("turn_id") == turn
                    and row["data"].get("dispatch_attempted")
                ):
                    self.update(ident, "consumed", model_request_id=request, consumed_at=time.time())
