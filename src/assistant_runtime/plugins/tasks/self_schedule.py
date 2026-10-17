"""Explicitly authorized schedule changes owned by the current execution."""

from datetime import datetime, timezone
import hashlib
import json
import time
import uuid

from .schedules import next_time, trigger


def schedule(value, zone):
    if not isinstance(value, dict):
        raise ValueError("Expected a timed schedule")
    kind = value.get("kind")
    fields = {
        "interval": {"kind", "seconds"},
        "date": {"kind", "at"},
        "cron": {"kind", "expression", "timezone"},
    }
    if not isinstance(kind, str) or kind not in fields or set(value) - fields[kind]:
        raise ValueError("Self scheduling supports only interval, date and cron triggers")
    required = fields[kind] - ({"timezone"} if kind == "cron" else set())
    if not required <= set(value):
        raise ValueError("Timed schedule is missing required fields")
    result = dict(value)
    if kind == "date":
        if not isinstance(result["at"], str) or not 1 <= len(result["at"]) <= 100:
            raise ValueError("Invalid task date")
        dt = datetime.fromisoformat(result["at"])
        if dt.tzinfo is None:
            raise ValueError("Task date must include timezone")
        result["at"] = dt.astimezone(timezone.utc).isoformat()
    elif kind == "cron":
        if not isinstance(result.get("expression"), str) or len(result["expression"]) > 100:
            raise ValueError("Invalid cron expression")
        result["expression"] = " ".join(result["expression"].split())
        result.setdefault("timezone", zone)
        if not isinstance(result["timezone"], str) or not 1 <= len(result["timezone"]) <= 100:
            raise ValueError("Invalid task timezone")
    try:
        trigger(result, zone, time.time())
    except (KeyError, TypeError, OverflowError):
        raise ValueError("Invalid timed schedule") from None
    return result


def policy(value, spec, zone):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"allowed_triggers", "condition"}
        or not isinstance(value["condition"], str)
        or not 1 <= len(value["condition"].strip()) <= 1000
        or not isinstance(value["allowed_triggers"], list)
        or not 1 <= len(value["allowed_triggers"]) <= 16
    ):
        raise ValueError("Self scheduling requires explicit allowed_triggers and an authorization condition")
    if (
        spec.get("authorization") != "user"
        or spec["trigger"]["kind"] not in {"interval", "date", "cron"}
        or spec.get("auto_archive")
        or spec.get("parent_id")
    ):
        raise ValueError("Self scheduling requires a user-authorized reusable timed plan")
    allowed = [schedule(item, zone) for item in value["allowed_triggers"]]
    if len({json.dumps(item, sort_keys=True) for item in allowed}) != len(allowed):
        raise ValueError("Duplicate self scheduling choices")
    return {"allowed_triggers": allowed, "condition": value["condition"].strip()}


class SelfSchedule:
    def __init__(self, plugin):
        self.plugin, self.repo = plugin, plugin.repo
        self.repo.db.executescript("""
          CREATE TABLE IF NOT EXISTS job_schedule_changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
            UNIQUE(run_id,request_id));
          CREATE INDEX IF NOT EXISTS job_schedule_changes_run ON job_schedule_changes(run_id,seq);
        """)

    def record(self, run, request_id, fingerprint, receipt):
        self.repo.db.execute(
            "INSERT INTO job_schedule_changes(run_id,request_id,fingerprint,data) VALUES(?,?,?,?)",
            (run, request_id, fingerprint, json.dumps(receipt, ensure_ascii=False)),
        )

    def history(self, run):
        rows = self.repo.db.execute(
            "SELECT data FROM job_schedule_changes WHERE run_id=? ORDER BY seq DESC LIMIT 21", (run,)
        ).fetchall()
        return {
            "schedule_changes": [json.loads(row[0]) for row in reversed(rows[:20])],
            "schedule_changes_truncated": len(rows) > 20,
        }

    def request(self, args, caller):
        if not caller:
            raise ValueError("Self scheduling requires the bound background execution identity")
        if not isinstance(args, dict) or set(args) != {
            "request_id",
            "revision",
            "trigger",
            "reason",
            "evidence",
        }:
            raise ValueError("Self scheduling accepts only UUID, revision, trigger, reason and evidence")
        try:
            if str(uuid.UUID(args["request_id"])) != args["request_id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Self scheduling requires a canonical UUID request_id") from None
        if type(args["revision"]) is not int or args["revision"] < 1:
            raise ValueError("Read the latest task revision before changing its schedule")
        for key, maximum in (("reason", 400), ("evidence", 2000)):
            if not isinstance(args[key], str) or not 1 <= len(args[key].strip()) <= maximum:
                raise ValueError("Self scheduling requires bounded reason and evidence")
        target = schedule(args["trigger"], self.plugin.settings.timezone)
        fingerprint = hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        run = self.repo.run(caller)
        prior = self.repo.db.execute(
            "SELECT fingerprint,data FROM job_schedule_changes WHERE run_id=? AND request_id=?",
            (caller, args["request_id"]),
        ).fetchone()
        if prior:
            if prior[0] != fingerprint:
                raise ValueError("Schedule request ID reused with different contents")
            current = self.repo.definition(run["task_id"])
            self.plugin.schedule(current)
            self.plugin.publish_definition(current)
            return json.loads(prior[1])  # Confirmed receipt, never a second mutation.
        if run["state"] != "running" or not run["data"].get("turn_active") or not run["data"].get("turn_id"):
            raise ValueError("Self scheduling requires a running provider turn")
        definition = self.repo.definition(run["task_id"])
        if definition["state"] != "enabled":
            raise ValueError("Paused or ended plans cannot change their own schedule")
        previous = self.repo.db.execute(
            "SELECT data FROM job_schedule_changes WHERE run_id=? ORDER BY seq DESC LIMIT 1", (caller,)
        ).fetchone()
        expected = json.loads(previous[0])["revision"] if previous else run["data"]["definition_revision"]
        if args["revision"] != definition["revision"]:
            raise ValueError("Task changed; read latest revision")
        if definition["revision"] != expected:
            raise ValueError("User changed this plan; a new execution must use the latest authorization")
        current = definition["spec"]
        for spec in (run["data"]["spec"], current):
            grant = policy(spec.get("self_schedule"), spec, self.plugin.settings.timezone)
            if grant is None or target not in grant["allowed_triggers"]:
                raise ValueError("Requested schedule is outside this execution's explicit authorization")
        if target == schedule(current["trigger"], self.plugin.settings.timezone):
            raise ValueError("Schedule already matches; keep its existing next occurrence")
        now = time.time()
        following = next_time(
            target,
            self.plugin.settings.timezone,
            now,
            after=None if target["kind"] == "date" else now,
        )
        if following is None or following <= now:
            raise ValueError("Self scheduling must select a future occurrence")
        receipt = {
            "request_id": args["request_id"],
            "run_id": caller,
            "task_id": definition["id"],
            "previous_revision": definition["revision"],
            "revision": definition["revision"] + 1,
            "before": current["trigger"],
            "after": target,
            "next_at": following,
            "at": now,
            "reason": args["reason"],
            "evidence": args["evidence"],
            "authorization_condition": current["self_schedule"]["condition"],
            "evidence_scope": "executor_reported",
            "status": "applied",
        }
        # No await between authorization checks and this transaction. Receipt and
        # definition commit together; the running snapshot is never rewritten.
        saved = self.repo.update_definition(
            definition["id"],
            expected_revision=definition["revision"],
            spec={**current, "trigger": target},
            next_at=following,
            on_update=lambda _: self.record(caller, args["request_id"], fingerprint, receipt),
        )
        self.plugin.schedule(saved)
        self.plugin.publish_definition(saved)
        return receipt
