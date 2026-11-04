"""Definitions, immutable run snapshots and delivery revisions in host SQLite."""

import base64
import json
import math
import re
import time
import uuid

ACTIVE = {"queued", "dispatching", "running", "waiting_user", "paused", "unknown"}
TERMINAL = {"completed", "failed", "canceled", "missed"}


def one_shot(spec):
    return spec["trigger"]["kind"] in {"immediate", "date"} and not spec.get("long_term", False)


def category(spec):
    return "temporary" if one_shot(spec) and spec["trigger"]["kind"] == "immediate" else "planned"


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
        CREATE INDEX IF NOT EXISTS job_definitions_page ON job_definitions(created DESC,id DESC);
        CREATE INDEX IF NOT EXISTS job_definitions_state_page ON job_definitions(state,created DESC,id DESC);
        CREATE INDEX IF NOT EXISTS job_runs_page ON job_runs(task_id,created DESC,id DESC);
        CREATE INDEX IF NOT EXISTS job_runs_active_page ON job_runs(task_id,state,created DESC,id DESC);
        CREATE TABLE IF NOT EXISTS job_finalizations (
          run_id TEXT PRIMARY KEY, completion_round INTEGER NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, error TEXT, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS job_finalizations_due ON job_finalizations(state,next_at);
        CREATE TABLE IF NOT EXISTS job_event_cursor (task_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_contexts (task_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_run_history (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS job_run_history_cursor ON job_run_history(run_id,seq);
        """)

    def record(self, ident, kind, data):
        """Owner evidence, separate from the mutable progress presentation."""
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode()) > 8192:
            raise ValueError("Task lifecycle evidence exceeds 8 KiB")
        with self.db:
            self.db.execute(
                "INSERT INTO job_run_history(run_id,at,kind,data) VALUES(?,?,?,?)",
                (ident, time.time(), kind, encoded),
            )

    def history(self, ident, cursor=0, limit=50):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid task history cursor or limit")
        rows = self.db.execute(
            "SELECT seq,at,kind,data FROM job_run_history WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (ident, cursor, limit + 1),
        ).fetchall()
        return {
            "items": [dict(seq=r[0], at=r[1], kind=r[2], data=json.loads(r[3])) for r in rows[:limit]],
            "cursor": rows[min(limit, len(rows)) - 1][0] if rows else cursor,
            "has_more": len(rows) > limit,
        }

    def definition(self, ident):
        row = self.db.execute(
            "SELECT id,state,spec,next_at,created,updated,revision FROM job_definitions WHERE id=?", (ident,)
        ).fetchone()
        if not row:
            raise ValueError("Task not found")
        return self.definition_row(row)

    def definition_row(self, row):
        definition = dict(
            zip(
                ("id", "state", "spec", "next_at", "created", "updated", "revision"),
                (row[0], row[1], json.loads(row[2]), *row[3:]),
            )
        )
        definition["category"] = category(definition["spec"])
        definition["spec"].setdefault("auto_archive", one_shot(definition["spec"]))
        context = self.db.execute(
            "SELECT data FROM task_contexts WHERE task_id=?", (definition["id"],)
        ).fetchone()
        if context:
            captured = json.loads(context[0])
            definition["context_info"] = {
                key: captured[key] for key in ("snapshot_ref", "source_session", "checkpoint")
            }
            if captured["additional_input"]:
                definition["context_info"]["additional_input"] = {
                    key: captured["additional_input"][key] for key in ("id", "sha256", "bytes")
                }
        return definition

    def definitions_by_ids(self, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        return {
            r[0]: self.definition_row(r)
            for r in self.db.execute(
                f"SELECT id,state,spec,next_at,created,updated,revision FROM job_definitions WHERE id IN ({marks})",
                ids,
            )
        }

    def definitions(self):
        return [
            self.definition(r[0])
            for r in self.db.execute("SELECT id FROM job_definitions ORDER BY created DESC")
        ]

    @staticmethod
    def page_options(args, scope):
        limit = args.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Page limit must be between 1 and 50")
        before = args.get("before")
        if before is None:
            return limit, None
        try:
            if not isinstance(before, str) or len(before) > 2048:
                raise ValueError()
            saved_scope, created, ident = json.loads(base64.urlsafe_b64decode(before.encode()))
            if (
                saved_scope != scope
                or type(created) not in (int, float)
                or not 0 <= created <= 1e12
                or not math.isfinite(created)
            ):
                raise ValueError()
            if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid page cursor for this query") from exc
        return limit, (created, ident)

    @staticmethod
    def page_result(rows, limit, scope):
        more = len(rows) > limit
        rows = rows[:limit]
        before = None
        if more:
            ident, created = rows[-1]
            before = base64.urlsafe_b64encode(json.dumps([scope, created, ident]).encode()).decode()
        return [row[0] for row in rows], before

    def definition_page(self, args):
        view = args.get("view", "all")
        if view not in {"all", "planned", "temporary"}:
            raise ValueError("Unknown task view")
        query, state = args.get("query", ""), args.get("state")
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("Task query must be at most 500 characters")
        if state is not None and state not in {"enabled", "paused", "archived", "terminated"}:
            raise ValueError("Unknown task state")
        include_archived = args.get("include_archived", False)
        active_only = args.get("active_only", False)
        if type(include_archived) is not bool or type(active_only) is not bool:
            raise ValueError("Task filters must be boolean")
        if (include_archived and view == "all") or (active_only and view != "planned"):
            raise ValueError("Filter does not apply to this task category")
        scope = ["tasks", view, query, state, include_archived, active_only]
        limit, cursor = self.page_options(args, scope)
        where, values = [], []
        temporary = "(json_extract(spec,'$.trigger.kind')='immediate' AND NOT coalesce(json_extract(spec,'$.long_term'),0))"
        unfinished = "EXISTS (SELECT 1 FROM job_runs WHERE task_id=job_definitions.id AND state IN ('queued','dispatching','running','waiting_user','paused','unknown'))"
        if view == "temporary":
            where.append(temporary)
            if not include_archived:
                where.append("state IN ('enabled','paused')")
                where.append(
                    f"(next_at IS NOT NULL OR {unfinished} OR NOT EXISTS (SELECT 1 FROM job_runs WHERE task_id=job_definitions.id))"
                )
        elif view == "planned":
            where.append(f"NOT {temporary}")
            if not include_archived:
                where.append("state!='archived'")
            if active_only:
                where.append("state='enabled'")
                where.append(
                    f"(next_at IS NOT NULL OR json_extract(spec,'$.trigger.kind')='event' OR {unfinished})"
                )
        if state:
            where.append("state=?")
            values.append(state)
        if query:
            where.append(
                "instr(lower(json_extract(spec,'$.title') || ' ' || json_extract(spec,'$.prompt')),lower(?))>0"
            )
            values.append(query)
        known = self.known_ids(args)
        removed = []
        if known:
            filters = " AND ".join(where) or "1"
            marks = ",".join("?" for _ in known)
            matching = {
                row[0]
                for row in self.db.execute(
                    f"SELECT id FROM job_definitions WHERE ({filters}) AND id IN ({marks})", (*values, *known)
                )
            }
            removed = [ident for ident in known if ident not in matching]
        if cursor:
            where.append("(created,id)<(?,?)")
            values.extend(cursor)
        clause = " AND ".join(where) or "1"
        rows = self.db.execute(
            f"SELECT id,created FROM job_definitions WHERE {clause} ORDER BY created DESC,id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        ids, before = self.page_result(rows, limit, scope)
        # Keep older visible work current without fetching its intervening pages.
        update_ids = [ident for ident in known if ident not in removed and ident not in ids]
        all_definitions = self.definitions_by_ids([*ids, *update_ids])
        updates = [all_definitions[ident] for ident in update_ids]
        return [all_definitions[ident] for ident in ids], before, removed, updates

    @staticmethod
    def known_ids(args):
        known = args.get("known", [])
        if (
            not isinstance(known, list)
            or len(known) > 50
            or any(not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident) for ident in known)
        ):
            raise ValueError("Expected at most 50 known IDs")
        return known

    def run_page(self, ident, args):
        scope = ["runs", ident]
        limit, cursor = self.page_options(args, scope)
        clause, values = "", [ident]
        if cursor:
            clause = " AND (created,id)<(?,?)"
            values.extend(cursor)
        rows = self.db.execute(
            f"SELECT id,created FROM job_runs WHERE task_id=?{clause} ORDER BY created DESC,id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        ids, before = self.page_result(rows, limit, scope)
        # Keep an old unfinished execution visible even after many skipped occurrences.
        active = self.db.execute(
            "SELECT id FROM job_runs WHERE task_id=? AND state IN ('queued','dispatching','running','waiting_user','paused','unknown') ORDER BY created DESC,id DESC LIMIT ?",
            (ident, limit),
        ).fetchall()
        known = self.known_ids(args)
        updates = []
        if known:
            marks = ",".join("?" for _ in known)
            updates = [
                row[0]
                for row in self.db.execute(
                    f"SELECT id FROM job_runs WHERE task_id=? AND id IN ({marks})", (ident, *known)
                )
            ]
        active_ids = [row[0] for row in active]
        found = self.runs_by_ids([*ids, *active_ids, *updates])
        return [found[i] for i in ids], [found[i] for i in active_ids], before, [found[i] for i in updates]

    def list_runs(self, definitions):
        ids = list(dict.fromkeys(d["id"] for d in definitions))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT id,task_id,state,created,progress FROM ("
            f"SELECT id,task_id,state,created,substr(coalesce(json_extract(data,'$.progress'),''),1,240) AS progress,"
            f"row_number() OVER (PARTITION BY task_id ORDER BY created DESC,id DESC) AS latest,"
            f"row_number() OVER (PARTITION BY task_id,CASE WHEN state IN ('queued','dispatching','running','waiting_user','paused','unknown') THEN 1 ELSE 0 END ORDER BY created DESC,id DESC) AS active "
            f"FROM job_runs WHERE task_id IN ({marks})) WHERE latest=1 OR "
            f"(active=1 AND state IN ('queued','dispatching','running','waiting_user','paused','unknown'))",
            ids,
        ).fetchall()
        by_task = {}
        for row in rows:
            by_task.setdefault(row[1], []).append(
                {
                    "id": row[0],
                    "task_id": row[1],
                    "state": row[2],
                    "created": row[3],
                    "data": {"progress": row[4]},
                }
            )
        return [
            run
            for ident in ids
            for run in sorted(by_task.get(ident, []), key=lambda r: (r["created"], r["id"]), reverse=True)
        ]

    def runs_by_ids(self, ids):
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT id,task_id,occurrence,state,data,created,updated,revision,delivered FROM job_runs WHERE id IN ({marks})",
            ids,
        ).fetchall()
        return {
            r[0]: dict(
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
                    (*r[:4], json.loads(r[4]), *r[5:]),
                )
            )
            for r in rows
        }

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

    def seed_finalizations(self):
        # Only this plugin's confirmed-ended model executions; never enumerate
        # unrelated provider conversations or re-run an execution to close it.
        with self.db:
            self.db.execute(
                """
              INSERT INTO job_finalizations(run_id,completion_round,state,updated)
              SELECT id,coalesce(json_extract(data,'$.completion_round'),0),'pending',?
              FROM job_runs WHERE state IN ('completed','failed','canceled','missed')
              AND json_extract(data,'$.execution_session_id') IS NOT NULL
              ON CONFLICT(run_id) DO UPDATE SET completion_round=excluded.completion_round,
                state='pending',attempts=0,next_at=0,error=NULL,updated=excluded.updated
              WHERE job_finalizations.completion_round!=excluded.completion_round
            """,
                (time.time(),),
            )

    def finalization(self, ident):
        row = self.db.execute(
            "SELECT completion_round,state,attempts,next_at,error FROM job_finalizations WHERE run_id=?",
            (ident,),
        ).fetchone()
        return dict(zip(("completion_round", "state", "attempts", "next_at", "error"), row)) if row else None

    def finalizations(self):
        rows = self.db.execute(
            """
          SELECT f.run_id FROM job_finalizations f JOIN job_runs r ON r.id=f.run_id
          WHERE f.state='pending' AND f.next_at<=?
          AND r.state IN ('completed','failed','canceled','missed')
          AND f.completion_round=coalesce(json_extract(r.data,'$.completion_round'),0)
          ORDER BY f.next_at,f.run_id LIMIT 10
        """,
            (time.time(),),
        ).fetchall()
        return [row[0] for row in rows]

    def finalize_receipt(self, run, error=None):
        # Housekeeping must not alter the run's revision, result notification,
        # measured duration, execution status or explicit continuation round.
        round_ = run["data"].get("completion_round", 0)
        with self.db:
            if error is None:
                self.db.execute(
                    "UPDATE job_finalizations SET state='done',error=NULL,updated=? WHERE run_id=? AND completion_round=?",
                    (time.time(), run["id"], round_),
                )
            else:
                row = self.db.execute(
                    "SELECT attempts FROM job_finalizations WHERE run_id=? AND completion_round=?",
                    (run["id"], round_),
                ).fetchone()
                if row:
                    self.db.execute(
                        "UPDATE job_finalizations SET attempts=attempts+1,error=?,next_at=?,updated=? WHERE run_id=? AND completion_round=?",
                        (
                            str(error)[:240],
                            time.time() + min(300, 5 * 2 ** min(row[0], 6)),
                            time.time(),
                            run["id"],
                            round_,
                        ),
                    )

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
        now = time.time()
        target = state or r["state"]
        if (
            target == "dispatching"
            and r["state"] != "dispatching"
            and data["spec"]["execution"].get("kind") == "agent"
        ):
            execution = data["spec"]["execution"]
            history = data.get("model_selections", [])
            data["model_selections"] = [
                *history,
                {
                    "segment": history[-1]["segment"] + 1 if history else 1,
                    "at": now,
                    "model": execution["model"],
                    "reasoning_effort": execution["reasoning_effort"],
                    "selection": execution.get(
                        "selection",
                        {
                            "version": 0,
                            "source": "legacy_saved",
                            "reason": execution.get("reason", "既有任务保存的具体选型"),
                        },
                    ),
                },
            ][-16:]
        timed = {"dispatching", "running", "waiting_user"}
        # Durable wall-clock segments: queue, explicit pauses and uncertain states
        # do not consume execution time. Never derive duration from updated (delivery
        # receipts also update it), or fabricate start times for old executions.
        if data.get("timing_since") is not None:
            data["duration_seconds"] = data.get("duration_seconds", 0) + max(0, now - data["timing_since"])
        if target in timed:
            if r["state"] == "queued" and data.get("started_at") is None:
                data["started_at"] = now
                data["duration_seconds"] = 0.0
            data["timing_since"] = now if data.get("started_at") is not None else None
        else:
            data["timing_since"] = None
        if target in TERMINAL and r["state"] not in TERMINAL:
            data["finished_at"] = now
            if r["state"] == "queued" and data.get("started_at") is None:
                data["duration_seconds"] = 0.0
        elif target not in TERMINAL:
            data.pop("finished_at", None)
        with self.db:
            if "progress" in changes and changes["progress"] != r["data"].get("progress"):
                self.db.execute(
                    "INSERT INTO job_run_history(run_id,at,kind,data) VALUES(?,?,?,?)",
                    (
                        ident,
                        now,
                        "progress",
                        json.dumps(
                            {
                                "before": str(r["data"].get("progress", ""))[:2000],
                                "after": str(changes["progress"])[:2000],
                                "bounded": True,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            if target != r["state"]:
                self.db.execute(
                    "INSERT INTO job_run_history(run_id,at,kind,data) VALUES(?,?,?,?)",
                    (ident, now, "state", json.dumps({"before": r["state"], "after": target})),
                )
            self.db.execute(
                "UPDATE job_runs SET state=?,data=?,updated=?,revision=revision+1 WHERE id=?",
                (state or r["state"], json.dumps(data, ensure_ascii=False), time.time(), ident),
            )
            if target in TERMINAL and data.get("execution_session_id"):
                self.db.execute(
                    """
                  INSERT INTO job_finalizations(run_id,completion_round,state,updated) VALUES(?,?,'pending',?)
                  ON CONFLICT(run_id) DO UPDATE SET completion_round=excluded.completion_round,
                    state='pending',attempts=0,next_at=0,error=NULL,updated=excluded.updated
                  WHERE job_finalizations.completion_round!=excluded.completion_round
                """,
                    (ident, data.get("completion_round", 0), now),
                )
        return self.run(ident)

    def update_definition(self, ident, *, on_update=None, expected_revision=None, **changes):
        d = self.definition(ident)
        expected = d["revision"] if expected_revision is None else expected_revision
        if d["revision"] != expected:
            raise ValueError("Task changed; read latest revision")
        d.update(changes)
        with self.db:
            cursor = self.db.execute(
                "UPDATE job_definitions SET state=?,spec=?,next_at=?,updated=?,revision=revision+1 WHERE id=? AND revision=?",
                (
                    d["state"],
                    json.dumps(d["spec"], ensure_ascii=False),
                    d["next_at"],
                    time.time(),
                    ident,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task changed; read latest revision")
            if on_update:
                on_update(self.definition(ident))
        return self.definition(ident)
