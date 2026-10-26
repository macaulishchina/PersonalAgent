"""Additional immutable round/scope ceilings inside the existing usage ledger."""

import json
from .settings import limits
from assistant_runtime.api.ai_usage import UsageError

KEYS = {"max_requests", "max_input_tokens", "max_output_tokens", "max_cost_cny", "max_compactions"}


def validate(value):
    limits(value)
    if set(value) - KEYS:
        raise ValueError("Controlled budget supports request, token, cost and compaction ceilings")


def install(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS controlled_budgets(
        work_id TEXT NOT NULL, caller TEXT NOT NULL, limits TEXT NOT NULL,
        PRIMARY KEY(work_id,caller));
      CREATE TABLE IF NOT EXISTS round_buckets(
        work_id TEXT NOT NULL, caller TEXT NOT NULL, requests INTEGER NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, cost REAL, compactions INTEGER NOT NULL,
        PRIMARY KEY(work_id,caller));
    """)


def configure(ledger, work, caller, caps):
    validate(caps)
    text = json.dumps(caps, sort_keys=True)
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        row = ledger.db.execute(
            "SELECT limits FROM controlled_budgets WHERE work_id=? AND caller=?", (work, caller)
        ).fetchone()
        if row and row[0] != text:
            raise ValueError(
                "Existing controlled budget is immutable; a new execution cannot refresh a shared allowance"
            )
        ledger.db.execute("INSERT OR IGNORE INTO controlled_budgets VALUES(?,?,?)", (work, caller, text))
    return caps


def enforce(ledger, meta, reserved, compaction, now):
    for caller in ("", meta["caller"]):
        row = ledger.db.execute(
            "SELECT limits FROM controlled_budgets WHERE work_id=? AND caller=?", (meta["work_id"], caller)
        ).fetchone()
        if not row:
            continue
        caps = json.loads(row[0])
        if caller:
            rows = ledger.db.execute(
                "SELECT state,input_tokens,output_tokens,cost,reserved_input,reserved_output,reserved_cost,compaction,created,1 FROM calls WHERE work_id=? AND caller=? AND state!='not_sent'",
                (meta["work_id"], caller),
            ).fetchall()
            archived = ledger.db.execute(
                "SELECT input_tokens,output_tokens,cost,compactions,requests FROM round_buckets WHERE work_id=? AND caller=?",
                (meta["work_id"], caller),
            ).fetchone()
            if archived:
                rows.append(("archived", *archived[:3], None, None, None, archived[3], 0, archived[4]))
        else:
            rows = ledger.rows({"scope": "work"}, meta, now)
        label = "round" if caller else "scope"
        for key, column, reserve, extra in (
            ("max_input_tokens", 1, 4, reserved[0]),
            ("max_output_tokens", 2, 5, reserved[1]),
            ("max_cost_cny", 3, 6, reserved[2]),
        ):
            if key not in caps:
                continue
            values = [r[column] if r[column] is not None else r[reserve] for r in rows]
            if extra is None or any(v is None for v in values):
                raise UsageError("usage_unaccountable:" + label + ":" + key, meta["work_id"])
            if sum(values) + extra > caps[key]:
                raise UsageError("usage_limit:" + label + ":" + key, meta["work_id"])
        if "max_requests" in caps and sum(r[9] for r in rows) >= caps["max_requests"]:
            raise UsageError("usage_limit:" + label + ":max_requests", meta["work_id"])
        if compaction and "max_compactions" in caps and sum(r[7] for r in rows) >= caps["max_compactions"]:
            raise UsageError("usage_limit:" + label + ":max_compactions", meta["work_id"])


def archive(db, ident):
    row = db.execute(
        "SELECT work_id,caller,input_tokens,output_tokens,cost,compaction FROM calls WHERE id=? AND state!='not_sent'",
        (ident,),
    ).fetchone()
    if row is None:
        return
    db.execute(
        "INSERT INTO round_buckets VALUES(?,?,1,?,?,?,?) ON CONFLICT(work_id,caller) DO UPDATE SET requests=requests+1,input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,cost=cost+excluded.cost,compactions=compactions+excluded.compactions",
        row,
    )
