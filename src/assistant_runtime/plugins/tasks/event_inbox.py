"""Durable event receipts and bounded task inboxes; no device or model execution."""

import asyncio
import time

from ...kernel.contracts import EventRequest, DecisionSample, DecisionQuery
from ...kernel.decisions import decide, fallback, report_decision
from ...kernel.event_reporting import identifier


def subscription(spec):
    trigger = spec.get("trigger", {})
    event = trigger.get("event", {})
    return event if trigger.get("kind") == "event" and event.get("source") == "records" else None


def validate_subscription(value):
    allowed = {
        "source",
        "source_id",
        "types",
        "subject",
        "merge_seconds",
        "cooldown_seconds",
        "max_age_seconds",
        "capacity",
    }
    if set(value) - allowed:
        raise ValueError("Unknown durable event subscription field")
    for key in ("source_id", "subject"):
        if key in value:
            identifier(value[key])
    kinds = value.get("types")
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= 16:
        raise ValueError("Subscription requires 1..16 exact event types")
    for kind in kinds:
        identifier(kind)
    result = {
        "merge_seconds": 3,
        "cooldown_seconds": 30,
        "max_age_seconds": 300,
        "capacity": 128,
        **value,
        "types": list(dict.fromkeys(kinds)),
    }
    for key, low, high in (
        ("merge_seconds", 0, 60),
        ("cooldown_seconds", 0, 86400),
        ("max_age_seconds", 1, 86400),
        ("capacity", 1, 256),
    ):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError("Invalid subscription " + key)
    if result["merge_seconds"] >= result["max_age_seconds"]:
        raise ValueError("Merge window must be shorter than event lifetime")
    return result


class EventLogRegression(RuntimeError):
    pass


class EventInbox:
    def __init__(self, plugin):
        self.plugin, self.repo = plugin, plugin.repo
        self.db = self.repo.db
        self.signal = asyncio.Event()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS job_subscriptions (
          task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, cursor INTEGER NOT NULL,
          last_dispatch REAL NOT NULL DEFAULT 0, error TEXT, retry_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS job_inbox (
          task_id TEXT NOT NULL, event_id TEXT NOT NULL, cursor INTEGER NOT NULL,
          event_time REAL NOT NULL, received REAL NOT NULL, expires REAL NOT NULL,
          PRIMARY KEY(task_id,event_id));
        CREATE INDEX IF NOT EXISTS job_inbox_due ON job_inbox(task_id,cursor);
        CREATE TABLE IF NOT EXISTS job_event_decisions (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          event_id TEXT, reason TEXT NOT NULL, at REAL NOT NULL, run_id TEXT);
        CREATE INDEX IF NOT EXISTS job_event_decisions_task ON job_event_decisions(task_id,seq);
        CREATE TABLE IF NOT EXISTS job_event_counts (
          task_id TEXT NOT NULL, reason TEXT NOT NULL, count INTEGER NOT NULL,
          PRIMARY KEY(task_id,reason));
        """)

    async def tail(self):
        result = await self.plugin.host.runtime.invoke(EventRequest("status"))
        return result["cursor"]

    def decision(self, task, event, reason, now, run=None):
        self.db.execute(
            "INSERT INTO job_event_counts VALUES (?,?,1) ON CONFLICT(task_id,reason) DO UPDATE SET count=count+1",
            (task, reason),
        )
        self.db.execute(
            "INSERT INTO job_event_decisions(task_id,event_id,reason,at,run_id) VALUES (?,?,?,?,?)",
            (task, event, reason, now, run),
        )
        self.db.execute(
            "DELETE FROM job_event_decisions WHERE task_id=? AND seq NOT IN (SELECT seq FROM job_event_decisions WHERE task_id=? ORDER BY seq DESC LIMIT 100)",
            (task, task),
        )

    def discard(self, task, reason):
        now = time.time()
        for (event,) in self.db.execute("SELECT event_id FROM job_inbox WHERE task_id=?", (task,)).fetchall():
            self.decision(task, event, reason, now)
        self.db.execute("DELETE FROM job_inbox WHERE task_id=?", (task,))

    def reset(self, definition, cursor, reason="definition_changed"):
        """Called in the same transaction as definition create/edit/pause/resume."""
        ident = definition["id"]
        self.discard(ident, reason)
        if subscription(definition["spec"]) and definition["state"] == "enabled":
            if (
                not self.db.execute("SELECT 1 FROM job_subscriptions WHERE task_id=?", (ident,)).fetchone()
                and self.db.execute("SELECT count(*) FROM job_subscriptions").fetchone()[0] >= 64
            ):
                raise ValueError("At most 64 enabled durable event subscriptions")
            self.db.execute(
                "INSERT INTO job_subscriptions(task_id,revision,cursor) VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision,cursor=excluded.cursor,error=NULL,retry_at=0",
                (ident, definition["revision"], cursor),
            )
        else:
            self.db.execute("DELETE FROM job_subscriptions WHERE task_id=?", (ident,))
        self.signal.set()

    def current(self, task, revision):
        definition = self.repo.definition(task)
        return definition["state"] == "enabled" and definition["revision"] == revision

    async def call(self, action, **arguments):
        return await self.plugin.host.runtime.invoke(EventRequest(action, arguments))

    async def cycle(self):
        rows = self.db.execute(
            "SELECT task_id,revision,cursor FROM job_subscriptions WHERE retry_at<=? ORDER BY task_id LIMIT 64",
            (time.time(),),
        ).fetchall()
        more = False
        for task, revision, cursor in rows:
            if not self.current(task, revision):
                continue
            try:
                page = await self.call("changes", after=cursor, limit=50)
                if page["head"] < cursor:
                    raise EventLogRegression(
                        "Event log moved backwards; inspect the recorder before explicit resume"
                    )
                # Only append is a trigger. Latest correction/deletion also invalidates
                # a previously admitted observation; never retain raw event payloads.
                details = {}
                for change in page["items"]:
                    if change["action"] == "append":
                        details[change["id"]] = await self.call("get", id=change["id"], limit=1)
                if not self.current(task, revision):
                    continue
                with self.db:
                    samples = self.ingest(self.repo.definition(task), page, details)
                    self.db.execute(
                        "UPDATE job_subscriptions SET cursor=?,error=NULL WHERE task_id=? AND revision=?",
                        (page["next"], task, revision),
                    )
                    self.dispatch(self.repo.definition(task))
                from ...kernel.ai_usage import UsageScope
                import uuid

                usage_scope = UsageScope("event-check:" + uuid.uuid4().hex, "event-check", task)
                for sample in samples:
                    self.plugin.context.publish("decision.observed", sample=sample, usage_scope=usage_scope)
                more |= page["has_more"]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.db:
                    self.db.execute(
                        "UPDATE job_subscriptions SET error=?,retry_at=? WHERE task_id=? AND revision=?",
                        (
                            str(exc)
                            if isinstance(exc, EventLogRegression)
                            else type(exc).__name__ + ": event consumption unavailable",
                            time.time() + 5,
                            task,
                            revision,
                        ),
                    )
        return more

    def ingest(self, definition, page, details):
        task, now = definition["id"], time.time()
        samples = []
        config = subscription(definition["spec"])
        # Release expired capacity before considering newly arriving events.
        for (ident,) in self.db.execute(
            "SELECT event_id FROM job_inbox WHERE task_id=? AND expires<=?", (task, now)
        ).fetchall():
            self.decision(task, ident, "expired", now)
            self.db.execute("DELETE FROM job_inbox WHERE task_id=? AND event_id=?", (task, ident))
        count = self.db.execute("SELECT count(*) FROM job_inbox WHERE task_id=?", (task,)).fetchone()[0]
        for change in page["items"]:
            ident = change["id"]
            if change["action"] != "append":
                removed = self.db.execute(
                    "DELETE FROM job_inbox WHERE task_id=? AND event_id=?", (task, ident)
                ).rowcount
                if removed:
                    count -= removed
                    self.decision(task, ident, "invalidated", now)
                continue
            entry = details[ident]
            record = entry.get("record")
            if not record or entry["revision"] != 1 or record["evidence"] != "observation":
                continue
            if (
                record["type"] not in config["types"]
                or (config.get("source_id") and entry["source"] != config["source_id"])
                or (config.get("subject") and record["subject"] != config["subject"])
            ):
                continue
            at = entry["event_time"]
            if at > now + 5 or at + config["max_age_seconds"] <= now:
                self.decision(task, ident, "future_timestamp" if at > now + 5 else "expired", now)
                continue
            if count >= config["capacity"]:
                self.decision(task, ident, "overflow", now)
                continue
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO job_inbox VALUES (?,?,?,?,?,?)",
                (task, ident, change["cursor"], at, now, at + config["max_age_seconds"]),
            ).rowcount
            if inserted:
                count += 1
                self.decision(task, ident, "received", now)
                samples.append(
                    DecisionSample(
                        "event_relevance",
                        f"task:{task}:{definition['revision']}:event:{ident}",
                        {"objective": definition["spec"]["prompt"], "event": record},
                    ),
                )
        return samples

    def dispatch(self, definition):
        task, now = definition["id"], time.time()
        if self.repo.active_runs(task):
            return
        row = self.db.execute(
            "SELECT min(received),count(*) FROM job_inbox WHERE task_id=?", (task,)
        ).fetchone()
        if not row[1]:
            return
        config = subscription(definition["spec"])
        last = self.db.execute(
            "SELECT last_dispatch FROM job_subscriptions WHERE task_id=?", (task,)
        ).fetchone()[0]
        if now < row[0] + config["merge_seconds"] or now < last + config["cooldown_seconds"]:
            return
        entries = self.db.execute(
            "SELECT event_id,cursor,event_time,expires FROM job_inbox WHERE task_id=? ORDER BY cursor LIMIT 32",
            (task,),
        ).fetchall()
        context = {
            "kind": "durable_events",
            "subscription": config,
            "count": len(entries),
            "first_observed_at": min(r[2] for r in entries),
            "last_observed_at": max(r[2] for r in entries),
            "events": [{"id": r[0], "cursor": r[1], "event_time": r[2], "expires_at": r[3]} for r in entries],
        }
        previous = self.repo.runs(task, limit=1)
        if previous:
            context["previous_run"] = {"id": previous[0]["id"], "state": previous[0]["state"]}
        run = self.repo.enqueue(
            definition,
            f"records:{definition['revision']}:{entries[0][1]}:{entries[-1][1]}",
            additions={"trigger_context": context},
        )
        for entry in entries:
            self.db.execute("DELETE FROM job_inbox WHERE task_id=? AND event_id=?", (task, entry[0]))
            self.decision(task, entry[0], "assigned", now, run["id"])
        self.db.execute("UPDATE job_subscriptions SET last_dispatch=? WHERE task_id=?", (now, task))

    def report(self, advice, status, reason, run_id):
        if advice.get("decision_id"):
            self.plugin.context.spawn(
                report_decision(self.plugin.host.runtime, advice, status, reason, run_id),
                "event-decision-outcome",
            )

    async def prepare(self, run):
        """Revalidate queued references before model/device dispatch, never replay a run."""
        context = run["data"].get("trigger_context")
        if not context or run["data"].get("continuation"):
            return True
        try:
            valid = []
            records = []
            for ref in context["events"]:
                if ref["expires_at"] <= time.time():
                    continue
                item = await self.call("get", id=ref["id"], limit=1)
                if item.get("record") and item["revision"] == 1 and ref["expires_at"] > time.time():
                    valid.append(ref)
                    records.append(item["record"])
        except Exception:
            current = self.repo.run(run["id"])
            if current["state"] == "queued":
                self.repo.update_run(
                    run["id"], event_retry_at=time.time() + 5, progress="等待事件服务恢复，尚未启动执行。"
                )
            return False
        advice = None
        if valid and self.plugin.host.config.decisions.get("external_data_consent", False):
            advice = await decide(
                self.plugin.host.runtime,
                DecisionQuery(
                    "event_relevance",
                    "event-run:" + run["id"],
                    {"objective": run["data"]["spec"]["prompt"], "event": {"batch": records}},
                    summary=run["data"]["spec"]["title"][:600] + " · " + str(len(records)) + " events",
                ),
            )
            refreshed = []
            try:
                for ref in valid:
                    item = await self.call("get", id=ref["id"], limit=1)
                    if item.get("record") and item["revision"] == 1 and ref["expires_at"] > time.time():
                        refreshed.append(ref)
            except Exception:
                current = self.repo.run(run["id"])
                if current["state"] == "queued":
                    self.repo.update_run(
                        run["id"],
                        event_retry_at=time.time() + 5,
                        progress="等待事件服务恢复，尚未启动执行。",
                    )
                return False
            if refreshed != valid:
                self.report(advice, "rejected", "events_changed", run["id"])
                advice = fallback("events_changed")
            valid = refreshed
        current = self.repo.run(run["id"])
        if current["state"] != "queued":
            if advice:
                self.report(advice, "rejected", "run_changed", run["id"])
            return False
        if not valid:
            if advice:
                self.report(advice, "rejected", "events_expired", run["id"])
            self.repo.update_run(
                run["id"],
                "missed",
                skip_reason="events_no_longer_valid",
                progress="触发事件已过期、更正或删除，本次未启动。",
            )
            return False
        if advice:
            skip = advice["status"] == "ok" and advice["choice"] == "irrelevant"
            self.report(
                advice,
                "adopted" if skip else "rejected",
                "irrelevant_batch" if skip else "original_event_dispatch",
                run["id"],
            )
        if advice and advice["status"] == "ok" and advice["choice"] == "irrelevant":
            self.repo.update_run(
                run["id"],
                "missed",
                skip_reason="semantic_irrelevant",
                decision_support=advice,
                progress="触发事件与任务授权目标无关，本次未启动；原始事件已保留。",
            )
            return False
        self.repo.update_run(
            run["id"],
            trigger_context={
                **context,
                "valid_events": valid,
                "invalidated_before_dispatch": len(context["events"]) - len(valid),
            },
            event_retry_at=0,
            decision_support=advice,
        )
        return True

    def describe(self, task):
        row = self.db.execute(
            "SELECT revision,cursor,last_dispatch,error FROM job_subscriptions WHERE task_id=?", (task,)
        ).fetchone()
        items = self.db.execute(
            "SELECT event_id,cursor,event_time,expires FROM job_inbox WHERE task_id=? ORDER BY cursor LIMIT 256",
            (task,),
        ).fetchall()
        return {
            "subscription": dict(zip(("revision", "cursor", "last_dispatch", "error"), row)) if row else None,
            "pending_count": len(items),
            "events": [dict(zip(("id", "cursor", "event_time", "expires_at"), r)) for r in items],
            "counts": dict(
                self.db.execute("SELECT reason,count FROM job_event_counts WHERE task_id=?", (task,))
            ),
            "recent_decisions": [
                dict(zip(("seq", "event_id", "reason", "at", "run_id"), r))
                for r in self.db.execute(
                    "SELECT seq,event_id,reason,at,run_id FROM job_event_decisions WHERE task_id=? ORDER BY seq DESC LIMIT 20",
                    (task,),
                )
            ],
        }

    async def listen(self, queue):
        while True:
            event = await queue.get()
            if event.kind == "events.committed":
                self.signal.set()

    async def run(self):
        while True:
            self.signal.clear()
            more = await self.cycle()
            if more:
                await asyncio.sleep(0.05)
            else:
                try:
                    await asyncio.wait_for(self.signal.wait(), 1)
                except TimeoutError:
                    pass
