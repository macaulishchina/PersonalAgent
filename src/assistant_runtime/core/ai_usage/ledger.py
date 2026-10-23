"""Single-worker SQLite ledger. All methods execute off the event loop."""

from dataclasses import asdict
import json
import fcntl
import math
import sqlite3
import time
import uuid

from assistant_runtime.api.ai_usage import UsageError, UsageScope
from assistant_runtime.core.ai_usage.settings import SELECTORS


class Ledger:
    def __init__(self, path, settings):
        self.path, self.settings, self.db = path, settings, None
        self.owner = None

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = self.path.with_suffix(".lock").open("a+")
        self.path.with_suffix(".lock").chmod(0o600)
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            self.owner = None
            raise
        self.db = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY, created REAL NOT NULL,
            blocked TEXT, extra INTEGER NOT NULL DEFAULT 0, warned INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS plan_blocks(task_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY, scope TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            caller TEXT NOT NULL, task_id TEXT NOT NULL, service TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, created REAL NOT NULL,
            state TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
            cost REAL, reserved_input INTEGER, reserved_output INTEGER, reserved_cost REAL,
            compaction INTEGER NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS calls_work ON calls(work_id,created);
          CREATE INDEX IF NOT EXISTS calls_time ON calls(created);
          CREATE INDEX IF NOT EXISTS calls_provider ON calls(provider,model,created);
          CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            kind TEXT NOT NULL, data TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS adjustments(id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            created REAL NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS buckets(work_id TEXT NOT NULL, task_id TEXT NOT NULL,
            service TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,
            requests INTEGER NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,
            unknown_usage INTEGER NOT NULL,cost REAL NOT NULL,unknown_cost INTEGER NOT NULL,
            compactions INTEGER NOT NULL,first REAL NOT NULL,last REAL NOT NULL,
            PRIMARY KEY(work_id,task_id,service,provider,model));
          CREATE TABLE IF NOT EXISTS daily(day TEXT NOT NULL, service TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, requests INTEGER NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, unknown INTEGER NOT NULL,
            cost REAL NOT NULL, cost_unknown INTEGER NOT NULL,
            PRIMARY KEY(day,service,provider,model));
        """)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT value FROM metadata WHERE key='policy'").fetchone()
            if row and row[0] != self.settings.revision():
                self.db.execute(
                    "UPDATE works SET blocked=NULL WHERE blocked LIKE 'usage_limit:%' OR blocked LIKE 'usage_unaccountable:%'"
                )
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES('policy',?)", (self.settings.revision(),))
            for ident, data in self.db.execute(
                "SELECT id,data FROM calls WHERE state='submitted'"
            ).fetchall():
                record = json.loads(data)
                record.update(state="unknown", error="process_interrupted", ended=time.time())
                self.db.execute(
                    "UPDATE calls SET state='unknown',data=? WHERE id=?", (json.dumps(record), ident)
                )
        self.prune()

    def close(self):
        if self.db:
            self.db.close()
            self.db = None
        if self.owner:
            self.owner.close()
            self.owner = None

    def bind(self, ident, scope):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT OR IGNORE INTO works(id,created) VALUES(?,?)", (scope.work_id, time.time())
            )
            row = self.db.execute("SELECT scope FROM bindings WHERE id=?", (ident,)).fetchone()
            if row:
                previous = UsageScope(**json.loads(row[0]))
                if previous.work_id != scope.work_id:
                    raise UsageError("usage_identity_conflict", previous.work_id)
                return asdict(previous)
            self.db.execute("INSERT INTO bindings VALUES(?,?)", (ident, json.dumps(asdict(scope))))
        return asdict(scope)

    def binding(self, ident):
        row = self.db.execute("SELECT scope FROM bindings WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def price(self, provider, model):
        prices = [
            p
            for p in self.settings.prices.values()
            if p["provider"] == provider and p.get("model", "") in {"", model}
        ]
        exact = [p for p in prices if p.get("model") == model]
        return (exact or prices or [None])[0]

    @staticmethod
    def cost(price, inp, out):
        if not price:
            return None
        if any(
            price.get(k, 0) and v is None
            for k, v in (("input_cny_per_million", inp), ("output_cny_per_million", out))
        ):
            return None
        return (
            price.get("request_cny", 0)
            + (inp or 0) * price.get("input_cny_per_million", 0) / 1e6
            + (out or 0) * price.get("output_cny_per_million", 0) / 1e6
        )

    def matching(self, meta):
        yield {
            "id": "work",
            "scope": "work",
            "limits": {"max_requests": self.settings.max_requests, **self.settings.work_limits},
        }
        for rule in self.settings.rules:
            if all(meta.get(k) == v for k, v in rule.get("match", {}).items()):
                yield rule

    def rows(self, rule, meta, now):
        where, args = ["state!='not_sent'"], []
        if rule["scope"] == "work":
            where.append("work_id=?")
            args.append(meta["work_id"])
        elif rule["scope"] == "plan":
            if not meta["task_id"]:
                return []
            where.append("task_id=?")
            args.append(meta["task_id"])
        for key, value in rule.get("match", {}).items():
            where.append(key + "=?")
            args.append(value)
        if "window_seconds" in rule:
            where.append("created>?")
            args.append(now - rule["window_seconds"])
        rows = self.db.execute(
            "SELECT state,input_tokens,output_tokens,cost,reserved_input,reserved_output,reserved_cost,compaction,created,1 FROM calls WHERE "
            + " AND ".join(where),
            args,
        ).fetchall()
        if rule["scope"] == "work" and "window_seconds" not in rule:
            filters, values = ["work_id=?"], [meta["work_id"]]
            for key, value in rule.get("match", {}).items():
                filters.append(key + "=?")
                values.append(value)
            for n, inp, out, unknown, cost, cost_unknown, compact, last in self.db.execute(
                "SELECT requests,input_tokens,output_tokens,unknown_usage,cost,unknown_cost,compactions,last FROM buckets WHERE "
                + " AND ".join(filters),
                values,
            ):
                rows.append(
                    (
                        "archived",
                        inp if not unknown else None,
                        out if not unknown else None,
                        cost if not cost_unknown else None,
                        None,
                        None,
                        None,
                        compact,
                        last,
                        n,
                    )
                )
        return rows

    def notice(self, work, kind, data):
        extra = self.db.execute("SELECT extra FROM works WHERE id=?", (work,)).fetchone()
        ident = work + ":" + kind + ":" + str(extra[0] if extra else 0) + ":" + self.settings.revision()
        self.db.execute(
            "INSERT OR IGNORE INTO notices VALUES(?,?,?,?,0)", (ident, work, kind, json.dumps(data))
        )

    def admit(self, scope, service, provider, model, bounds, compaction, body, details):
        now = time.time()
        meta = {**asdict(scope), "service": service, "provider": provider, "model": model}
        self.bind(scope.work_id, UsageScope(scope.work_id, scope.work_id))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            work = self.db.execute(
                "SELECT created,blocked,extra,warned FROM works WHERE id=?", (scope.work_id,)
            ).fetchone()
            plan = self.plan_blocked(scope.task_id) if scope.task_id else None
            reason = work[1] or ("usage_plan_paused" if plan else None)
            reserve_input, reserve_output = bounds.get("input_tokens"), bounds.get("output_tokens")
            price = self.price(provider, model)
            reserve_cost = self.cost(price, reserve_input, reserve_output)
            for rule in self.matching(meta):
                if reason:
                    break
                rows = self.rows(rule, meta, now)
                caps = rule["limits"]

                def used(column, reserve):
                    values = [row[column] if row[column] is not None else row[reserve] for row in rows]
                    return None if any(v is None for v in values) else sum(values)

                for key, column, reserved, extra in (
                    ("max_input_tokens", 1, 4, reserve_input),
                    ("max_output_tokens", 2, 5, reserve_output),
                    ("max_cost_cny", 3, 6, reserve_cost),
                ):
                    if key in caps:
                        total = used(column, reserved)
                        if total is None or extra is None:
                            reason = "usage_unaccountable:" + rule["id"] + ":" + key
                        elif total + extra > caps[key]:
                            reason = "usage_limit:" + rule["id"] + ":" + key
                allowance = work[2] if rule["id"] == "work" else 0
                if "max_requests" in caps and sum(r[9] for r in rows) >= caps["max_requests"] + allowance:
                    reason = "usage_limit:" + rule["id"] + ":max_requests"
                if (
                    "max_compactions" in caps
                    and compaction
                    and sum(r[7] for r in rows) >= caps["max_compactions"]
                ):
                    reason = "usage_limit:" + rule["id"] + ":max_compactions"
                if "max_seconds" in caps and now - work[0] >= caps["max_seconds"]:
                    reason = "usage_limit:" + rule["id"] + ":max_seconds"
                if "concurrency" in caps and sum(r[0] == "submitted" for r in rows) >= caps["concurrency"]:
                    raise UsageError("usage_busy:" + rule["id"], scope.work_id)
                if "requests_per_second" in caps and sum(
                    r[8] > now - max(1, 1 / caps["requests_per_second"]) for r in rows
                ) >= max(1, int(caps["requests_per_second"])):
                    raise UsageError("usage_rate_limited:" + rule["id"], scope.work_id)
            if reason:
                self.db.execute("UPDATE works SET blocked=? WHERE id=?", (reason, scope.work_id))
                plans = {scope.task_id} - {""}
                plans.update(
                    r[0]
                    for r in self.db.execute(
                        "SELECT DISTINCT task_id FROM calls WHERE work_id=? AND task_id!=''", (scope.work_id,)
                    )
                )
                plans.update(
                    json.loads(r[0]).get("task_id", "")
                    for r in self.db.execute(
                        "SELECT scope FROM bindings WHERE json_extract(scope,'$.work_id')=?", (scope.work_id,)
                    )
                )
                for task in plans - {""}:
                    self.db.execute(
                        "INSERT OR IGNORE INTO plan_blocks VALUES(?,?,?)", (task, scope.work_id, reason)
                    )
                self.notice(scope.work_id, "blocked", {**meta, "reason": reason})
            else:
                ident = uuid.uuid4().hex
                record = {
                    "id": ident,
                    **meta,
                    "created": now,
                    "state": "submitted",
                    "config_revision": self.settings.revision(),
                    "price": price,
                    "details": details,
                    "compaction": compaction,
                }
                if body is not None:
                    record["request_body"] = body
                self.db.execute(
                    "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        scope.work_id,
                        scope.caller,
                        scope.task_id,
                        service,
                        provider,
                        model,
                        now,
                        "submitted",
                        None,
                        None,
                        None,
                        reserve_input,
                        reserve_output,
                        reserve_cost,
                        int(compaction),
                        json.dumps(record),
                    ),
                )
                count = self.snapshot(scope.work_id)["requests"]
                record["attempt"] = count
                self.db.execute("UPDATE calls SET data=? WHERE id=?", (json.dumps(record), ident))
                cap = self.settings.work_limits.get("max_requests", self.settings.max_requests) + work[2]
                if not work[3] and count >= math.ceil(cap * self.settings.warning_fraction):
                    self.db.execute("UPDATE works SET warned=1 WHERE id=?", (scope.work_id,))
                    self.notice(scope.work_id, "warning", {"requests": count, "limit": cap})
                record["_snapshot"] = self.snapshot(scope.work_id)
                return record
        raise UsageError(reason, scope.work_id)

    def settle(self, ident, result):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT state,data FROM calls WHERE id=?", (ident,)).fetchone()
            if not row:
                raise UsageError("usage_unknown_call")
            if row[0] != "submitted":
                return json.loads(row[1])
            record = json.loads(row[1])
            record.update(result, ended=time.time())
            record["elapsed_ms"] = round((record["ended"] - record["created"]) * 1000)
            usage = result.get("usage") or {}
            inp, out = usage.get("input_tokens"), usage.get("output_tokens")
            inp = inp if type(inp) is int and inp >= 0 else None
            out = out if type(out) is int and out >= 0 else None
            cost = self.cost(record.get("price"), inp, out)
            record.update(
                input_tokens=inp,
                output_tokens=out,
                usage_state="known" if inp is not None and out is not None else "unknown",
                cost_cny=cost,
                total_tokens=inp + out if inp is not None and out is not None else None,
                cached_input_tokens=usage.get("cached_input_tokens"),
                reasoning_tokens=usage.get("reasoning_tokens"),
                cost_kind="estimated" if cost is not None else "unknown",
            )
            self.db.execute(
                "UPDATE calls SET state=?,input_tokens=?,output_tokens=?,cost=?,data=? WHERE id=?",
                (result["state"], inp, out, cost, json.dumps(record), ident),
            )
            return record

    def snapshot(self, work):
        row = self.db.execute("SELECT created,blocked,extra,warned FROM works WHERE id=?", (work,)).fetchone()
        if not row:
            return {"work_id": work, "requests": 0, "limit": self.settings.max_requests, "blocked": None}
        calls = self.db.execute(
            "SELECT state,input_tokens,output_tokens,cost,compaction FROM calls WHERE work_id=? AND state!='not_sent'",
            (work,),
        ).fetchall()
        archived = self.db.execute(
            "SELECT coalesce(sum(requests),0),coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(unknown_usage),0),coalesce(sum(cost),0),coalesce(sum(unknown_cost),0),coalesce(sum(compactions),0) FROM buckets WHERE work_id=?",
            (work,),
        ).fetchone()
        return {
            "work_id": work,
            "created": row[0],
            "blocked": row[1],
            "limit": self.settings.work_limits.get("max_requests", self.settings.max_requests) + row[2],
            "warning": bool(row[3]),
            "requests": len(calls) + archived[0],
            "inflight": sum(r[0] == "submitted" for r in calls),
            "unknown_requests": sum(r[0] == "unknown" for r in calls),
            "unknown_compaction_requests": sum(r[0] == "unknown" and r[4] for r in calls),
            "unknown_execution_requests": self.db.execute(
                "SELECT count(*) FROM calls WHERE work_id=? AND service='execution' AND state='unknown' AND compaction=0",
                (work,),
            ).fetchone()[0],
            "input_tokens": sum(r[1] or 0 for r in calls) + archived[1],
            "output_tokens": sum(r[2] or 0 for r in calls) + archived[2],
            "unknown_usage": sum(r[1] is None or r[2] is None for r in calls) + archived[3],
            "estimated_cost_cny": sum(r[3] or 0 for r in calls) + archived[4],
            "unknown_cost": sum(r[3] is None for r in calls) + archived[5],
            "compactions": sum(r[4] for r in calls) + archived[6],
        }

    def query(self, args):
        allowed = {"work_id", "task_id", "service", "provider", "model", "before", "limit", "group_by"}
        if set(args) - allowed:
            raise ValueError("Unknown usage query filter")
        limit = args.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Usage query limit must be 1..200")
        where, params = [], []
        for key in SELECTORS:
            if key in args:
                if not isinstance(args[key], str):
                    raise ValueError("Invalid usage query filter")
                where.append(key + "=?")
                params.append(args[key])
        if "before" in args:
            if type(args["before"]) not in (float, int) or not math.isfinite(args["before"]):
                raise ValueError("Invalid usage query cursor")
            where.append("created<?")
            params.append(args["before"])
        clause = " WHERE " + " AND ".join(where) if where else ""
        group = args.get("group_by")
        if group == "day":
            if set(args) - {"group_by", "limit", "service", "provider", "model"}:
                raise ValueError("Daily statistics support service/provider/model filters")
            daily_where, daily_values = [], []
            for key in ("service", "provider", "model"):
                if key in args:
                    daily_where.append(key + "=?")
                    daily_values.append(args[key])
            clause = " WHERE " + " AND ".join(daily_where) if daily_where else ""
            rows = self.db.execute(
                "SELECT day,sum(requests),sum(input_tokens),sum(output_tokens),sum(unknown),sum(cost),sum(cost_unknown) FROM (SELECT date(created,'unixepoch') AS day, service,provider,model,1 AS requests,coalesce(input_tokens,0) AS input_tokens,coalesce(output_tokens,0) AS output_tokens,(input_tokens IS NULL OR output_tokens IS NULL) AS unknown,coalesce(cost,0) AS cost,(cost IS NULL) AS cost_unknown FROM calls WHERE state!='not_sent' UNION ALL SELECT * FROM daily)"
                + clause
                + " GROUP BY day ORDER BY day DESC LIMIT ?",
                (*daily_values, limit),
            ).fetchall()
            return {
                "items": [
                    dict(
                        zip(
                            (
                                "day",
                                "requests",
                                "input_tokens",
                                "output_tokens",
                                "unknown_usage",
                                "estimated_cost_cny",
                                "unknown_cost",
                            ),
                            r,
                        )
                    )
                    for r in rows
                ]
            }
        if group:
            if group not in SELECTORS:
                raise ValueError("Invalid usage grouping")
            rows = self.db.execute(
                "SELECT "
                + group
                + ",sum(requests),sum(input_tokens),sum(output_tokens),sum(cost),sum(unknown_usage),sum(unknown_cost) FROM ("
                "SELECT work_id,task_id,service,provider,model,created,1 AS requests,coalesce(input_tokens,0) AS input_tokens,coalesce(output_tokens,0) AS output_tokens,coalesce(cost,0) AS cost,(input_tokens IS NULL OR output_tokens IS NULL) AS unknown_usage,(cost IS NULL) AS unknown_cost FROM calls WHERE state!='not_sent' "
                "UNION ALL SELECT work_id,task_id,service,provider,model,last,requests,input_tokens,output_tokens,cost,unknown_usage,unknown_cost FROM buckets)"
                + clause
                + " GROUP BY "
                + group
                + " LIMIT ?",
                (*params, limit),
            ).fetchall()
            return {
                "items": [
                    dict(
                        zip(
                            (
                                group,
                                "requests",
                                "input_tokens",
                                "output_tokens",
                                "estimated_cost_cny",
                                "unknown_usage",
                                "unknown_cost",
                            ),
                            row,
                        )
                    )
                    for row in rows
                ]
            }
        rows = self.db.execute(
            "SELECT data FROM calls" + clause + " ORDER BY created DESC,id DESC LIMIT ?", (*params, limit + 1)
        ).fetchall()
        items = [json.loads(r[0]) for r in rows[:limit]]
        return {"items": items, "before": items[-1]["created"] if len(rows) > limit else None}

    def extend(self, work, amount, reason):
        if (
            type(amount) is not int
            or not 1 <= amount <= 1_000_000
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 500
        ):
            raise ValueError("Explicit extension requires positive requests and a reason")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute("SELECT 1 FROM works WHERE id=?", (work,)).fetchone():
                raise ValueError("Unknown work")
            self.db.execute("UPDATE works SET extra=extra+?,blocked=NULL WHERE id=?", (amount, work))
            self.db.execute(
                "INSERT INTO adjustments VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, work, time.time(), amount, reason),
            )
        return self.snapshot(work)

    def plans(self):
        return {
            row[0]: {"work_id": row[1], "reason": row[2]}
            for row in self.db.execute("SELECT task_id,work_id,reason FROM plan_blocks")
        }

    def plan_blocked(self, task):
        row = self.db.execute("SELECT work_id,reason FROM plan_blocks WHERE task_id=?", (task,)).fetchone()
        return {"work_id": row[0], "reason": row[1]} if row else None

    def resume_plan(self, task):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("DELETE FROM plan_blocks WHERE task_id=?", (task,))

    def notices(self):
        return [
            {"id": r[0], "work_id": r[1], "kind": r[2], **json.loads(r[3])}
            for r in self.db.execute("SELECT id,work_id,kind,data FROM notices WHERE delivered=0 LIMIT 100")
        ]

    def acknowledge(self, ident):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE notices SET delivered=1 WHERE id=?", (ident,))

    def prune(self):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # Never remove unsettled/unknown calls or blocked budgets. Aggregate first.
            cutoff = now - self.settings.retention_days * 86400
            rows = self.db.execute(
                "SELECT id,created,service,provider,model,input_tokens,output_tokens,cost,work_id,task_id,compaction FROM calls WHERE created<? AND state IN ('completed','rejected','not_sent') AND work_id NOT IN (SELECT id FROM works WHERE blocked IS NOT NULL) AND work_id NOT IN (SELECT work_id FROM calls WHERE state IN ('submitted','unknown'))",
                (cutoff,),
            ).fetchall()
            for ident, created, service, provider, model, inp, out, cost, work, task, compact in rows:
                state = self.db.execute("SELECT state FROM calls WHERE id=?", (ident,)).fetchone()[0]
                if state != "not_sent":
                    day = time.strftime("%Y-%m-%d", time.gmtime(created))
                    self.db.execute(
                        "INSERT INTO daily VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day,service,provider,model) DO UPDATE SET requests=requests+1,input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,unknown=unknown+excluded.unknown,cost=cost+excluded.cost,cost_unknown=cost_unknown+excluded.cost_unknown",
                        (
                            day,
                            service,
                            provider,
                            model,
                            1,
                            inp or 0,
                            out or 0,
                            int(inp is None or out is None),
                            cost or 0,
                            int(cost is None),
                        ),
                    )
                    self.db.execute(
                        "INSERT INTO buckets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id,task_id,service,provider,model) DO UPDATE SET requests=requests+1,input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,unknown_usage=unknown_usage+excluded.unknown_usage,cost=cost+excluded.cost,unknown_cost=unknown_cost+excluded.unknown_cost,compactions=compactions+excluded.compactions,last=max(last,excluded.last)",
                        (
                            work,
                            task,
                            service,
                            provider,
                            model,
                            1,
                            inp or 0,
                            out or 0,
                            int(inp is None or out is None),
                            cost or 0,
                            int(cost is None),
                            compact,
                            created,
                            created,
                        ),
                    )
                self.db.execute("DELETE FROM calls WHERE id=?", (ident,))
            for ident, data in self.db.execute(
                "SELECT id,data FROM calls WHERE created<?", (now - self.settings.body_days * 86400,)
            ).fetchall():
                record = json.loads(data)
                if "request_body" in record or "response_body" in record:
                    record.pop("request_body", None)
                    record.pop("response_body", None)
                    self.db.execute("UPDATE calls SET data=? WHERE id=?", (json.dumps(record), ident))
            self.db.execute(
                "DELETE FROM daily WHERE day<?",
                (time.strftime("%Y-%m-%d", time.gmtime(now - self.settings.aggregate_days * 86400)),),
            )
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
