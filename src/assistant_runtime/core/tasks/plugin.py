"""Durable task management. Scheduling callbacks never execute model work inline."""

from assistant_runtime.api.lifecycle import admits_work

from .declaration import DECLARATION

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid
import weakref

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from assistant_runtime.api.contracts import TaskRequest, DeviceQuery, NotificationRequest
from assistant_runtime.core.tasks.settings import TaskSettings
from assistant_runtime.core.tasks.repository import TaskRepository, ACTIVE, TERMINAL, one_shot
from assistant_runtime.core.tasks.schedules import next_time, trigger
from assistant_runtime.core.tasks.event_inbox import EventInbox, subscription, validate_subscription
from assistant_runtime.core.tasks.trace import TaskTrace
from assistant_runtime.core.tasks.selection import ModelPolicy
from assistant_runtime.core.tasks.model_advice import select_execution
from assistant_runtime.core.tasks.model_review import ModelReview, assessment, evidence
from assistant_runtime.core.tasks.upgrades import (
    ModelUpgrades,
    pending as pending_upgrade,
    replace as replace_upgrade,
)
from assistant_runtime.core.tasks.self_schedule import (
    SelfSchedule,
    policy as schedule_policy,
    completion_policy,
)
from assistant_runtime.core.tasks.feedback import TaskFeedback


RUN_LABELS = {
    "queued": "等待启动",
    "dispatching": "正在启动",
    "running": "执行中",
    "waiting_user": "等待你处理",
    "unknown": "执行状态待核实",
    "paused": "本次已暂停",
    "completed": "本次已完成",
    "failed": "本次执行失败",
    "canceled": "本次已取消",
    "missed": "本次已跳过",
}
RUN_SUMMARIES = {
    "completed": "本次执行已结束，未提供文字摘要。",
    "failed": "本次执行失败。",
    "canceled": "本次执行已取消。",
    "missed": "本次计划未启动，已跳过。",
}


class TasksPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, host):
        self.host = host
        required = ("execution", host.config.bindings["brain"], "notifications", "workfiles")
        self.manifest = replace(
            DECLARATION.manifest,
            requires=required,
            dependency_versions=tuple(
                (name, limits)
                for name, limits in DECLARATION.manifest.dependency_versions
                if name in required + DECLARATION.manifest.optional
            ),
        )
        self.settings = TaskSettings.parse(host.config.tasks)
        self.model_policy = ModelPolicy.parse(self.settings.model_policy)
        self.repo = TaskRepository(host.store.db)
        self.feedback = TaskFeedback(self)
        self.delivering_results = set()
        self.event_inbox = EventInbox(self)
        self.trace = TaskTrace(self.repo)
        self.upgrades = ModelUpgrades(self)
        self.self_schedule = SelfSchedule(self)
        self.model_review = ModelReview(self)
        self.upgrading = set()
        self.scheduler = None
        self.launching = set()
        self.draining = False
        self.drain_plans = set()
        self.started = False
        self.notification_error = None
        self.run_locks = weakref.WeakValueDictionary()

    def model_defaults(self):
        """Configured main binding, captured at selection; never a parent's incidental model."""
        return {
            "model": self.host.config.main_model,
            "reasoning_effort": self.host.config.main_reasoning_effort,
        }

    async def start(self, context):
        self.context = context
        self.executor = self.host.task_executor(self.provider_update)
        self.host.backend.jobs = self.executor
        context.own(self.close)
        context.register(TaskRequest, self.request)
        self.scheduler = AsyncIOScheduler(timezone=self.settings.timezone)
        self.scheduler.start(paused=True)
        for run in self.repo.pending():
            if run["state"] in {"dispatching", "running", "waiting_user"}:
                upgrade = pending_upgrade(run)
                if upgrade and upgrade["phase"] == "dispatching":
                    self.repo.update_run(
                        run["id"],
                        "unknown",
                        model_upgrades=replace_upgrade(run, phase="unknown", error="restart_during_handoff"),
                        progress="模型交接时服务中断，请核对后明确继续；未重放。",
                    )
                    continue
                if upgrade and upgrade["phase"] == "ready":
                    continue  # No new turn was sent; worker performs read-only preflight first.
                try:
                    state, text = await self.executor.reconcile(run)
                except Exception:
                    state, text = "unknown", "无法核对上次执行结果，未自动重做"
                self.provider_update(run["id"], state, progress=text, turn_active=False)
        interruption = self.host.config.state_dir / "configuration/unclean.json"
        if interruption.exists():
            # No dispatch occurred for these rows. Do not replay them after an orphan cleanup.
            self.maintenance_pause()
            for ident in list(self.drain_plans):
                self.due(ident)
            for row in self.repo.pending():
                if row["state"] == "queued":
                    self.repo.update_run(row["id"], "canceled", progress="管理进程异常中断，未自动执行。")
            self.draining = False
            self.drain_plans.clear()
            interruption.unlink(missing_ok=True)
        self.repo.seed_finalizations()
        self.archive_finished()
        for definition in self.repo.definitions():
            self.schedule(definition)
        self.scheduler.resume()
        self.started = True
        self.model_review.recover()
        context.spawn(self.model_review.worker(), "model-review")
        context.spawn(self.worker(), "dispatch-and-deliver")
        context.spawn(self.finalize_finished(), "finalize-executions")
        context.spawn(self.watch_events(), "event-rules")
        event_queue = context.runtime.subscribe("task-event-inbox", 32)

        async def unsubscribe():
            context.runtime.unsubscribe("task-event-inbox")

        context.own(unsubscribe)
        context.spawn(self.event_inbox.listen(event_queue), "event-hints")
        context.spawn(self.event_inbox.run(), "event-inbox")

    async def usage_blocked(self, notice):
        owners = []
        for run in self.repo.active_runs():
            scope = await self.host.ai_usage.service.binding(run["id"])
            if not scope or scope.work_id != notice["work_id"]:
                continue
            self.repo.update_run(run["id"], ai_usage=await self.host.ai_usage.service.snapshot(scope.work_id))
            owners.append({"run": run, "feedback": self.feedback.describe(run)})
            definition = self.repo.definition(run["task_id"])
            if definition["state"] == "enabled":
                with self.repo.db:
                    self.repo.db.execute(
                        "UPDATE job_definitions SET state='paused',revision=revision+1,updated=? WHERE id=?",
                        (time.time(), definition["id"]),
                    )
                self.schedule(self.repo.definition(definition["id"]))
        # Pre-dispatch/event judgments may belong to a plan before a run exists.
        # The durable latch is authoritative even without an active executor.
        for task_id, blocked in self.host.ai_usage.service.blocked_plans.items():
            if blocked["work_id"] != notice["work_id"]:
                continue
            try:
                definition = self.repo.definition(task_id)
            except ValueError:
                continue
            if definition["state"] == "enabled":
                with self.repo.db:
                    self.repo.db.execute(
                        "UPDATE job_definitions SET state='paused',revision=revision+1,updated=? WHERE id=?",
                        (time.time(), task_id),
                    )
                self.schedule(self.repo.definition(task_id))
        return owners

    def schedule(self, d):
        ident = d["id"]
        if self.scheduler.get_job(ident):
            self.scheduler.remove_job(ident)
        if d["state"] == "enabled" and d["next_at"] is not None:
            self.scheduler.add_job(
                self.scheduled_due,
                "date",
                run_date=datetime.fromtimestamp(max(time.time(), d["next_at"]), timezone.utc),
                args=[ident],
                id=ident,
                replace_existing=True,
                misfire_grace_time=None,
            )

    def archive_finished(self, task_ids=None):
        if task_ids is not None and not task_ids:
            return
        definitions = {d["id"]: d for d in self.repo.definitions()}
        candidates = set(definitions) if task_ids is None else set(task_ids)
        # Finishing a child can also finish an otherwise completed parent.
        for ident in list(candidates):
            seen = set()
            while ident in definitions and ident not in seen:
                seen.add(ident)
                ident = definitions[ident]["spec"].get("parent_id")
                if ident:
                    candidates.add(ident)
        children = {}
        for d in definitions.values():
            children.setdefault(d["spec"].get("parent_id"), []).append(d["id"])

        def unfinished(ident):
            if self.repo.active_runs(ident):
                return True
            d = definitions[ident]
            if d["state"] not in {"archived", "terminated"}:
                latest = self.repo.runs(ident, limit=1)
                recurring = d["spec"]["trigger"]["kind"] in {"interval", "cron", "event"}
                if d["next_at"] is not None or recurring or not latest:
                    return True
            return any(unfinished(child) for child in children.get(ident, []))

        for ident in candidates:
            d = definitions.get(ident)
            if not d or d["state"] not in {"enabled", "paused"} or not d["spec"]["auto_archive"]:
                continue
            if not one_shot(d["spec"]) or unfinished(ident):
                continue
            latest = self.repo.runs(ident, limit=1)
            if not latest or latest[0]["state"] not in TERMINAL:
                continue
            # Automatic archival only changes organization. Unlike explicit archive,
            # it never interrupts work, deletes results or creates another notification.
            definitions[ident] = self.repo.update_definition(ident, state="archived", next_at=None)
            self.schedule(definitions[ident])

    async def scheduled_due(self, ident):
        self.due(ident)

    @property
    def accepting_schedule(self):
        return not self.draining and not getattr(getattr(self.host, "intake", None), "paused", False)

    def due(self, ident):
        if not self.accepting_schedule and ident not in self.drain_plans:
            return
        self.drain_plans.discard(ident)
        d = self.repo.definition(ident)
        if d["state"] != "enabled" or d["next_at"] is None:
            return
        now = time.time()
        usage = getattr(self.host, "ai_usage", None)
        if usage and d["spec"]["execution"]["kind"] == "agent":
            if usage.service.blocked_plans.get(d["id"]):
                with self.repo.db:
                    self.repo.db.execute(
                        "UPDATE job_definitions SET state='paused',revision=revision+1,updated=? WHERE id=?",
                        (time.time(), d["id"]),
                    )
                self.schedule(self.repo.definition(d["id"]))
                return
        at = d["next_at"]
        spec = d["spec"]
        occurrence = at
        nxt = next_time(spec["trigger"], self.settings.timezone, d["created"], at)
        # Coalesce old interval occurrences arithmetically; cron uses upstream calendar math.
        if spec["trigger"]["kind"] == "interval" and now > at:
            occurrence = at + int((now - at) // spec["trigger"]["seconds"]) * spec["trigger"]["seconds"]
            nxt = next_time(spec["trigger"], self.settings.timezone, d["created"], occurrence)
        elif nxt is not None and nxt <= now:
            calendar = trigger(spec["trigger"], self.settings.timezone, d["created"])
            start = datetime.fromtimestamp(max(at, now - spec["misfire_seconds"]), timezone.utc)
            candidate = calendar.get_next_fire_time(None, start)
            while candidate and candidate.timestamp() <= now:
                occurrence = candidate.timestamp()
                candidate = calendar.get_next_fire_time(
                    candidate, datetime.fromtimestamp(occurrence + 0.001, timezone.utc)
                )
            nxt = candidate.timestamp() if candidate else None
        active = self.repo.active_runs(ident)
        reason = "overlap" if active else "late" if now - occurrence > spec["misfire_seconds"] else None
        state = "missed" if reason else "queued"
        skipped = (
            {
                "skip_reason": reason,
                "blocked_by": [run["id"] for run in active],
                "progress": "触发时同一任务已有未结束的执行，为避免重复，本轮未启动。"
                if reason == "overlap"
                else "已超过允许补跑的时限，本轮未启动。",
            }
            if reason
            else None
        )
        with self.repo.db:
            self.repo.enqueue(d, f"time:{occurrence:.6f}", state, additions=skipped)
            self.repo.db.execute("UPDATE job_definitions SET next_at=? WHERE id=?", (nxt, ident))
        self.schedule(self.repo.definition(ident))

    def provider_update(self, ident, state, **data):
        run = self.repo.run(ident)
        trace = data.pop("tool_trace", None)
        if trace:
            self.trace.append(ident, trace)
        if run["state"] in TERMINAL | {"paused"}:
            identifiers = {
                k: v for k, v in data.items() if k in {"execution_session_id", "turn_id", "execution_id"}
            }
            if identifiers:
                self.repo.update_run(ident, **identifiers)
            return False
        if "attachments" in data:
            # Pin only registered media while the producing run still owns it.
            # Quiet/no-notification tasks must not lose their images to upload expiry.
            data["attachments"] = self.host.workfiles.media.resolve(data["attachments"])
            self.host.workfiles.inputs.grant(data["attachments"], ident)
        state, data = self.upgrades.boundary(run, state, data)
        if "provider_selection" in data and run["data"].get("model_selections"):
            history = [dict(item) for item in run["data"]["model_selections"]]
            history[-1]["provider"] = data["provider_selection"]
            data["model_selections"] = history
        goal = data.get("goal", run["data"].get("goal", {}))
        if state == "completed" and run["state"] == "waiting_user":
            state = "waiting_user"
        if state == "completed" and (run["data"]["spec"].get("long_term") or goal):
            rounds = run["data"].get("goal_rounds", 0) + 1
            data["goal_rounds"] = rounds
            if goal.get("status") == "complete":
                state = "completed"
            elif (
                not run["data"]["spec"].get("long_term")
                or goal.get("status") in {"blocked", "paused", "budgetLimited", "usageLimited"}
                or rounds >= self.settings.max_goal_rounds
            ):
                state = "waiting_user"
                data["progress"] = "目标尚未完成；自动执行已停止，请核对进展后明确继续。"
            else:
                # Pi owns one agent run; tasks owns bounded goal continuation.
                # The SAME run, budget and grants survive. Never restart a whole plan.
                state = "queued"
                data["continuation"] = (
                    run["data"]["spec"]["prompt"]
                    + "\n继续尚未完成的已授权目标。先核对实际操作回执，不重做已完成动作；"
                    "结果不明先查证。上一轮摘要只作线索：\n"
                    + str(data.get("result", run["data"].get("result", "")))[:2000]
                )
        if data.get("goal") and state not in {"failed", "canceled", "unknown", "paused"}:
            status = goal.get("status")
            if status == "complete" and not data.get("turn_active", run["data"].get("turn_active")):
                state = "completed"
            elif status in {"blocked", "paused", "budgetLimited", "usageLimited"}:
                state = "waiting_user"
        if (data.get("ai_usage") or {}).get("blocked") and state != "completed":
            state = "paused" if state not in {"unknown", "canceled"} else state
            data["progress"] = "AI 调用预算已暂停；已完成的操作保持实际结果，请核对后明确继续。"
            definition = self.repo.definition(run["task_id"])
            with self.repo.db:
                self.repo.db.execute(
                    "UPDATE job_definitions SET state='paused',revision=revision+1,updated=? WHERE id=?",
                    (time.time(), definition["id"]),
                )
            if self.scheduler:
                self.schedule(self.repo.definition(definition["id"]))
        self.repo.update_run(ident, state, **data)
        return True

    async def validate_spec(self, value, caller=None, *, preserved_execution=None):
        allowed = {
            "title",
            "prompt",
            "trigger",
            "long_term",
            "workspace",
            "resources",
            "execution",
            "notify_voice",
            "notify_completion",
            "parent_id",
            "authorization",
            "misfire_seconds",
            "auto_archive",
            "self_schedule",
            "self_completion",
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Unknown task specification fields")
        s = {
            "trigger": {"kind": "immediate"},
            "long_term": False,
            "workspace": self.host.config.workspace,
            "resources": [],
            "execution": {},
            "notify_voice": False,
            "notify_completion": True,
            "authorization": "user",
            "misfire_seconds": self.settings.misfire_seconds,
            **value,
        }
        for key, maxlen in (("title", 200), ("prompt", 4000)):
            if not isinstance(s.get(key), str) or not 0 < len(s[key].strip()) <= maxlen:
                raise ValueError(f"Invalid task {key}")
        for key in ("long_term", "notify_voice", "notify_completion"):
            if type(s[key]) is not bool:
                raise ValueError(key + " must be boolean")
        if type(s["misfire_seconds"]) is not int or not 0 <= s["misfire_seconds"] <= 86400:
            raise ValueError("Invalid misfire window")
        if (
            not isinstance(s["resources"], list)
            or len(s["resources"]) > 16
            or any(not isinstance(x, str) or not 0 < len(x) <= 200 for x in s["resources"])
        ):
            raise ValueError("Invalid resource locks")
        workspace = Path(s["workspace"]).expanduser().resolve()
        if not workspace.is_dir() or not workspace.is_relative_to(Path(self.host.config.workspace).resolve()):
            raise ValueError("Task workspace must be inside configured workspace")
        s["workspace"] = str(workspace)
        tr = dict(s["trigger"]) if isinstance(s["trigger"], dict) else s["trigger"]
        s["trigger"] = tr
        if not isinstance(tr, dict) or set(tr) - {"kind", "at", "seconds", "expression", "timezone", "event"}:
            raise ValueError("Invalid task trigger")
        tr.setdefault("kind", "immediate")
        trigger(tr, self.settings.timezone, time.time())
        if tr["kind"] == "event":
            self.validate_event(tr.get("event", {}))
        if subscription(s):
            tr["event"] = validate_subscription(tr["event"])
            if "notify_completion" not in value:
                s["notify_completion"] = False
        s.setdefault("auto_archive", one_shot(s))
        if type(s["auto_archive"]) is not bool or (s["auto_archive"] and not one_shot(s)):
            raise ValueError("auto_archive requires a one-shot, non-Goal task")
        if s.get("self_completion") is not None:
            if caller:
                raise ValueError("Background executions cannot grant self completion to children")
            s["self_completion"] = completion_policy(s["self_completion"], s)
        if s.get("self_schedule") is not None:
            if caller:
                raise ValueError("Background executions cannot grant self scheduling to children")
            s["self_schedule"] = schedule_policy(s["self_schedule"], s, self.settings.timezone)
        if caller:
            parent = self.repo.run(caller)
            p = parent["data"]["spec"]
            if parent["state"] not in {"running", "waiting_user"}:
                raise ValueError("Parent execution is not active")
            if (
                tr["kind"] != "immediate"
                or s["notify_voice"]
                or not workspace.is_relative_to(Path(p["workspace"]))
                or not set(s["resources"]) <= set(p["resources"])
            ):
                raise ValueError("Child task exceeds parent execution scope")
            children = [d for d in self.repo.definitions() if d["spec"].get("parent_id") == parent["task_id"]]
            if len(children) >= 16 or p.get("depth", 0) >= 3:
                raise ValueError("Subtask limit reached")
            s["parent_id"], s["depth"] = parent["task_id"], p.get("depth", 0) + 1
            s["authorization"] = p["authorization"]
        else:
            s.pop("parent_id", None)
        requested = s["execution"]
        if preserved_execution is not None:
            s["execution"] = json.loads(json.dumps(preserved_execution))
            return s
        if not isinstance(requested, dict) or set(requested) - {
            "model",
            "reasoning_effort",
            "reason",
            "kind",
            "device_id",
            "recommendation",
            "selection",
            "assessment",
            "model_source",
            "effort_source",
        }:
            raise ValueError("Invalid task execution")
        if requested.get("kind", "agent") not in {"agent", "reminder", "device_query"}:
            raise ValueError("Unknown execution kind")
        if requested.get("kind") == "device_query":
            if requested.get("device_id") not in self.host.config.devices:
                raise ValueError("Task query device is not configured")
            s["execution"] = {"kind": "device_query", "device_id": requested["device_id"]}
        elif requested.get("kind") == "reminder":
            s["execution"] = {"kind": "reminder"}
        else:
            declared = assessment(requested.get("assessment"))
            prior = evidence(self, declared, caller, s["workspace"])
            s["execution"] = await select_execution(self, requested, s["title"] + "\n" + s["prompt"], prior)
            s["execution"]["assessment"] = declared
            s["execution"]["selection"]["prior_execution"] = prior
            if caller and self.repo.run(caller)["state"] not in {"running", "waiting_user"}:
                raise ValueError("Parent execution ended during task model selection")
        return s

    def validate_event(self, e):
        if isinstance(e, dict) and e.get("source") == "records":
            validate_subscription(e)
            return
        if not isinstance(e, dict) or set(e) - {
            "source",
            "id",
            "metric",
            "operator",
            "value",
            "cooldown_seconds",
            "duration_seconds",
            "hysteresis",
        }:
            raise ValueError("Invalid event rule")
        if e.get("source") not in {"device", "task"} or not isinstance(e.get("id"), str):
            raise ValueError("Event requires device/task source and stable ID")
        if e["source"] == "task":
            self.repo.definition(e["id"])
            if e.get("value") not in {"completed", "failed"}:
                raise ValueError("Task event must select completed or failed")
        else:
            if e["id"] not in self.host.config.devices:
                raise ValueError("Event device is not configured")
            if e.get("operator", "eq") not in {"eq", "gt", "lt"}:
                raise ValueError("Unknown event operator")
            if e.get("operator") in {"gt", "lt"} and type(e.get("value")) not in (int, float):
                raise ValueError("Threshold must be numeric")
        for key in ("cooldown_seconds", "duration_seconds", "hysteresis"):
            if type(e.get(key, 0)) not in (int, float) or not 0 <= e.get(key, 0) <= 86400:
                raise ValueError("Invalid event timing/hysteresis")

    @admits_work(lambda request: request.action in {"create", "run", "continue", "upgrade_model"})
    async def request(self, request):
        from assistant_runtime.api.ai_usage import current_usage

        token = None
        usage = getattr(self.host, "ai_usage", None)
        if (
            usage
            and request.action == "create"
            and request.arguments.get("spec", {}).get("execution", {}).get("kind", "agent") == "agent"
        ):
            token = current_usage.set(await usage.scope(request.caller_run))
        try:
            return await self._locked_request(request)
        finally:
            if token is not None:
                current_usage.reset(token)

    async def _locked_request(self, request):
        if request.action in {"reschedule_self", "complete_self"}:
            if not request.caller_run:
                raise ValueError("Self scheduling requires the bound background execution identity")
            lock = self.run_locks.setdefault(request.caller_run, asyncio.Lock())
            async with lock:
                return await self._request(request)
        if request.action in {
            "continue",
            "cancel_run",
            "pause_run",
            "supplement",
            "upgrade_model",
            "repair_resources",
        }:
            lock = self.run_locks.setdefault(request.arguments["run_id"], asyncio.Lock())
            async with lock:
                return await self._request(request)
        return await self._request(request)

    async def _request(self, request):
        a, args, caller = request.action, request.arguments, request.caller_run
        if caller and a not in {
            "create",
            "list",
            "get",
            "models",
            "artifacts",
            "event_inbox",
            "trace",
            "upgrade_model",
            "reschedule_self",
            "complete_self",
        }:
            raise ValueError(
                "Background executions cannot use general task management; use update_own_task_schedule for an explicitly authorized schedule change"
            )
        if a == "transfer_feedback":
            return await self.feedback.transfer(args, caller)
        if a == "repair_resources":
            from assistant_runtime.core.tasks.resources import repair_resources

            return repair_resources(self, args)
        if a in {"reschedule_self", "complete_self"}:
            return self.self_schedule.request(args, caller, complete=a == "complete_self")
        if a == "upgrade_model":
            return await self.upgrades.request(args, caller)
        if a in {"event_inbox", "trace"}:
            if a == "event_inbox":
                self.repo.definition(args["task_id"])
                return self.event_inbox.describe(args["task_id"])
            return {**self.trace.read(args), **self.self_schedule.history(args["run_id"])}
        if a == "models":
            models = await self.executor.models()
            return {
                "models": models,
                "configuration": self.settings.execution,
                "model_policy": self.model_policy.describe(
                    models, self.model_defaults(), self.settings.execution
                ),
            }
        if a == "list":
            definitions, before, removed, updates = self.repo.definition_page(args)
            return {
                "tasks": definitions,
                "updates": updates,
                "runs": self.repo.list_runs(definitions + updates),
                "before": before,
                "removed": removed,
            }
        if a == "execution_info":
            ids = args.get("run_ids")
            if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
                raise ValueError("Expected at most 100 execution IDs")
            result = {}
            for ident in set(ids):
                try:
                    run = self.repo.run(ident)
                except ValueError:
                    continue
                result[ident] = {
                    "task_id": run["task_id"],
                    "state": run["state"],
                    "resources": run["data"]["spec"].get("resources", []),
                    "execution_version": {
                        "execution_session_id": run["data"].get("execution_session_id"),
                        "turn_id": run["data"].get("turn_id"),
                        "model_segments": len(run["data"].get("model_upgrades", [])),
                    },
                    "events_management": run["state"] in {"running", "waiting_user"}
                    and "events_management" in run["data"]["spec"].get("resources", []),
                    "mcp_management": run["state"] in {"running", "waiting_user"}
                    and "mcp_management" in run["data"]["spec"].get("resources", []),
                    "duration_seconds": run["data"].get("duration_seconds")
                    if run["data"].get("finished_at") is not None
                    else None,
                    "execution_id": run["data"].get("execution_id"),
                    "attachments": run["data"].get("attachments", [])[:4],
                }
            return result
        if a == "get":
            ident = args["task_id"]
            definition = self.repo.definition(ident)
            runs, active, before, updates = self.repo.run_page(ident, args)
            for run in [*runs, *active, *updates]:
                run["feedback"] = self.feedback.describe(run)
                execution = getattr(self.host.backend, "executions", None)
                if execution and run["data"].get("execution_id"):
                    run["execution"] = execution.view(execution.records.get(run["data"]["execution_id"]))
                usage = getattr(self.host, "ai_usage", None)
                if usage and usage.service.ready:
                    scope = await usage.service.binding(run["id"])
                    if scope:
                        run["data"]["ai_usage"] = await usage.service.snapshot(scope.work_id)
                references = run["data"].get("attachments")
                if references:
                    run["data"]["attachments"] = self.host.workfiles.media.resolve(references)
            return {
                "task": definition,
                "runs": runs,
                "active_runs": active,
                "before": before,
                "updates": updates,
                # Only the selected page's newest run supplies the tool reply's
                # media. Never substitute an older run's picture for an empty one.
                "attachments": runs[0]["data"].get("attachments", []) if runs else [],
                "attachment_run_id": runs[0]["id"] if runs else None,
                "event_inbox": self.event_inbox.describe(ident) if subscription(definition["spec"]) else None,
            }
        if a == "create":
            request_id = str(args["request_id"])
            uuid.UUID(request_id)
            fingerprint = hashlib.sha256(
                json.dumps(
                    args["spec"]
                    if args.get("feedback_owner", "background") == "background"
                    else [args["spec"], args["feedback_owner"]],
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            old = self.repo.db.execute(
                "SELECT id,fingerprint FROM job_definitions WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if old[1] != fingerprint:
                    raise ValueError("Task request ID reused with different contents")
                return self.repo.definition(old[0])
            feedback_context = await self.feedback.capture(args.get("feedback_owner", "background"), caller)
            spec = await self.validate_spec(args["spec"], caller)
            if feedback_context and (
                spec["trigger"]["kind"] != "immediate" or spec["long_term"] or not spec["notify_completion"]
            ):
                raise ValueError(
                    "Conversation feedback on creation requires immediate non-Goal work with completion enabled"
                )
            event_cursor = await self.event_inbox.tail() if subscription(spec) else 0
            now, ident = time.time(), uuid.uuid4().hex
            at = next_time(spec["trigger"], self.settings.timezone, now)
            with self.repo.db:
                self.repo.db.execute(
                    "INSERT OR IGNORE INTO job_definitions(id,request_id,fingerprint,state,spec,next_at,created,updated) VALUES(?,?,?,'enabled',?,?,?,?)",
                    (ident, request_id, fingerprint, json.dumps(spec, ensure_ascii=False), at, now, now),
                )
                if (
                    self.repo.db.execute(
                        "SELECT id FROM job_definitions WHERE request_id=?", (request_id,)
                    ).fetchone()[0]
                    == ident
                ):
                    self.feedback.bind(ident, feedback_context)
                if (
                    subscription(spec)
                    and self.repo.db.execute(
                        "SELECT id FROM job_definitions WHERE request_id=?", (request_id,)
                    ).fetchone()[0]
                    == ident
                ):
                    self.event_inbox.reset(self.repo.definition(ident), event_cursor, "created")
            saved = self.repo.db.execute(
                "SELECT id,fingerprint FROM job_definitions WHERE request_id=?", (request_id,)
            ).fetchone()
            if saved[1] != fingerprint:
                raise ValueError("Task request ID reused with different contents")
            d = self.repo.definition(saved[0])
            if spec["trigger"]["kind"] == "immediate" and spec["execution"]["kind"] == "agent":
                usage = getattr(self.host, "ai_usage", None)
                if usage:
                    scope = await usage.scope(caller)
                    await usage.service.bind("plan:" + d["id"], scope)
            if self.draining and spec["trigger"]["kind"] == "immediate":
                self.drain_plans.add(d["id"])
            self.schedule(d)
            return d
        if a == "workfiles":
            return {
                **await self.host.workfiles.collect(bool(args.get("apply", False))),
                **await self.host.workfiles.usage(),
            }
        if a == "artifact_path":
            if caller is not None:
                raise ValueError("Artifact delivery is an entrance capability")
            from pathlib import Path

            name = args["name"]
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                raise ValueError("Invalid artifact name")
            run = self.repo.run(args["run_id"])
            owner = self.owner(run)
            if name not in {item["name"] for item in self.host.workfiles.artifacts(owner)}:
                raise FileNotFoundError(name)
            return {"path": str(self.host.workfiles.area(owner) / "artifacts" / name)}
        if a == "artifacts":
            run = self.repo.run(args["run_id"])
            return {"artifacts": self.host.workfiles.artifacts(self.owner(run))}
        if a in {"supplement", "cancel_run", "pause_run", "continue"}:
            run = self.repo.run(args["run_id"])
            if a == "supplement":
                if run["state"] not in {"running", "waiting_user"}:
                    raise ValueError("Execution is not running; use continue for a stopped task")
                await self.executor.supplement(run, self.input_text(args))
            elif a == "continue":
                if run["state"] not in {"paused", "unknown", "failed", "canceled", "waiting_user"}:
                    raise ValueError("Execution cannot be continued in this state")
                if run["state"] == "waiting_user":
                    raise ValueError("Answer the waiting task with supplement")
                if self.repo.definition(run["task_id"])["state"] in {"archived", "terminated"}:
                    raise ValueError("Task has ended; create a new task to continue its work")
                self.host.workfiles.retain(self.owner(run))
                self.repo.update_run(
                    run["id"],
                    "queued",
                    continuation=self.input_text(args),
                    goal={},
                    completion_delivered=False,
                    completion_round=run["data"].get("completion_round", 0) + 1,
                    artifacts=[],
                    questions=[],
                    turn_active=False,
                )
            else:
                if run["state"] in TERMINAL:
                    return run
                target = "paused" if a == "pause_run" else "canceled"
                changes = (
                    {"model_upgrades": replace_upgrade(run, phase="blocked", error="user_stopped_execution")}
                    if pending_upgrade(run)
                    else {}
                )
                self.repo.update_run(run["id"], target, **changes)
                try:
                    await self.executor.stop(run, pause=a == "pause_run")
                except Exception:
                    self.repo.update_run(run["id"], "unknown", progress="停止请求未确认，请核对执行状态")
                    raise
            return self.repo.run(run["id"])
        d = self.repo.definition(args["task_id"])
        if d["state"] in {"archived", "terminated"} and a in {"update", "pause", "resume", "run"}:
            raise ValueError("Archived/terminated tasks cannot be reactivated; create an explicit new task")
        if a == "update":
            if args.get("revision") != d["revision"]:
                raise ValueError("Task changed; read latest revision")
            if "execution" in args["changes"]:
                replacement = args["changes"]["execution"]
                if not isinstance(replacement, dict):
                    raise ValueError("Invalid task execution")
                replacement = dict(replacement)
                old_execution = d["spec"]["execution"]
                for key, source in (("model", "model_source"), ("reasoning_effort", "effort_source")):
                    if old_execution.get(source) == "user" and replacement.get(source) != "user":
                        if replacement.get(key) not in (None, "auto", old_execution[key]):
                            raise ValueError(
                                "Changing a user choice requires explicit user-directed replacement"
                            )
                        replacement.update({key: old_execution[key], source: "user"})
                args = {**args, "changes": {**args["changes"], "execution": replacement}}
            combined = {**d["spec"], **args["changes"]}
            combined.pop("depth", None)
            if {"trigger", "long_term"}.intersection(args["changes"]) and "auto_archive" not in args[
                "changes"
            ]:
                combined.pop("auto_archive", None)
            spec = await self.validate_spec(
                combined,
                preserved_execution=d["spec"]["execution"] if "execution" not in args["changes"] else None,
            )
            if "execution" not in args["changes"] and all(
                spec["execution"].get(k) == d["spec"]["execution"].get(k)
                for k in ("model", "reasoning_effort")
            ):
                spec["execution"] = d["spec"]["execution"]
            event_cursor = await self.event_inbox.tail() if subscription(spec) else 0
            if self.repo.definition(d["id"])["revision"] != d["revision"]:
                raise ValueError("Task changed; read latest revision")
            # Editing a child must preserve its lineage for recursive termination.
            for key in ("parent_id", "depth"):
                if key in d["spec"]:
                    spec[key] = d["spec"][key]
            d = self.repo.update_definition(
                d["id"],
                spec=spec,
                next_at=next_time(spec["trigger"], self.settings.timezone, time.time())
                if "trigger" in args["changes"]
                else d["next_at"],
                on_update=lambda saved: self.event_inbox.reset(saved, event_cursor),
            )
        elif a in {"pause", "resume", "archive", "terminate"}:
            if a == "resume" and getattr(self.host, "ai_usage", None):
                await self.host.ai_usage.service.call("resume_plan", d["id"])
                self.host.ai_usage.service.blocked_plans.pop(d["id"], None)
            state = {
                "pause": "paused",
                "resume": "enabled",
                "archive": "archived",
                "terminate": "terminated",
            }[a]
            event_cursor = await self.event_inbox.tail() if a == "resume" and subscription(d["spec"]) else 0
            if self.repo.definition(d["id"])["revision"] != d["revision"]:
                raise ValueError("Task changed; read latest revision")
            changes = {"state": state}
            if a == "resume":
                changes["next_at"] = next_time(d["spec"]["trigger"], self.settings.timezone, time.time())
            d = self.repo.update_definition(
                d["id"],
                **changes,
                on_update=lambda saved: self.event_inbox.reset(saved, event_cursor, "subscription_" + a),
            )
            if a in {"terminate", "archive"}:
                for run in self.repo.active_runs(d["id"]):
                    if run["state"] in ACTIVE:
                        await self.request(TaskRequest("cancel_run", {"run_id": run["id"]}))
                for child in self.repo.definitions():
                    if child["spec"].get("parent_id") == d["id"]:
                        await self.request(TaskRequest(a, {"task_id": child["id"]}))
        elif a == "run":
            if d["state"] in {"archived", "terminated"}:
                raise ValueError("Task is no longer enabled")
            uuid.UUID(args["request_id"])
            existing = self.repo.db.execute(
                "SELECT id FROM job_runs WHERE task_id=? AND occurrence=?",
                (d["id"], "manual:" + args["request_id"]),
            ).fetchone()
            if existing:
                return self.repo.run(existing[0])
            if self.repo.active_runs(d["id"]):
                raise ValueError("Task already has unfinished execution")
            with self.repo.db:
                return self.repo.enqueue(d, "manual:" + args["request_id"])
        else:
            raise ValueError("Unknown task operation")
        self.schedule(d)
        return d

    @staticmethod
    def input_text(args):
        text = args.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= 16000:
            raise ValueError("Provide nonempty task instructions (max 16000 characters)")
        return text

    @staticmethod
    def owner(run):
        return f"tasks/{run['task_id']}/{run['id']}"

    async def launch(self, run):
        from assistant_runtime.api.ai_usage import current_usage

        ident = run["id"]
        usage_token = None
        try:
            if self.repo.run(ident)["state"] != "queued":
                return
            usage = getattr(self.host, "ai_usage", None)
            if usage and run["data"]["spec"]["execution"]["kind"] == "agent":
                parent = await usage.service.binding("plan:" + run["task_id"])
                await usage.bind_task(run, parent)
                scope = await usage.service.binding(ident)
                usage_token = current_usage.set(scope)
                snapshot = await usage.service.snapshot(scope.work_id)
                self.repo.update_run(ident, ai_usage=snapshot)
                if snapshot.get("blocked"):
                    await self.usage_blocked({"work_id": scope.work_id})
                    self.repo.update_run(
                        ident, "paused", progress="AI 调用预算已暂停，请明确调整额度后继续。"
                    )
                    return
            if not await self.event_inbox.prepare(run):
                return
            run = self.repo.run(ident)
            directory = self.host.workfiles.area(self.owner(run))
            if run["data"]["spec"]["execution"]["kind"] == "reminder":
                self.repo.update_run(ident, "dispatching")
                self.repo.update_run(ident, "completed", progress=run["data"]["spec"]["prompt"])
            elif run["data"]["spec"]["execution"]["kind"] == "device_query":
                self.repo.update_run(ident, "dispatching")
                result = await self.host.runtime.invoke(
                    DeviceQuery("inspect", run["data"]["spec"]["execution"]["device_id"])
                )
                self.repo.update_run(ident, "completed", progress=json.dumps(result, ensure_ascii=False))
            else:
                spec = json.loads(json.dumps(run["data"]["spec"]))
                if any(
                    value != "auto" and value != spec["execution"].get(key)
                    for key, value in self.settings.execution.items()
                ):
                    requested = {
                        k: v for k, v in spec["execution"].items() if k not in {"selection", "recommendation"}
                    }
                    requested["reason"] = "执行前应用实例固定配置"
                    assessment_data = spec["execution"].get("assessment", {})
                    prior = spec["execution"].get("selection", {}).get("prior_execution")
                    spec["execution"] = await self.model_policy.resolve(
                        self.executor, requested, self.settings.execution, self.model_defaults()
                    )
                    spec["execution"]["assessment"] = assessment_data
                    spec["execution"]["selection"]["prior_execution"] = prior
                await self.executor.select(spec["execution"]["model"], spec["execution"]["reasoning_effort"])
                if self.repo.run(ident)["state"] != "queued":
                    return
                run = self.repo.update_run(ident, "dispatching", spec=spec)
                self.model_review.enqueue(run)
                await self.executor.launch(run, directory)
        except Exception as exc:
            # A provider request may have executed before its reply was lost.
            current = self.repo.run(ident)
            if current["state"] not in {"canceled", "paused"}:
                state = "failed" if run["data"]["spec"]["execution"]["kind"] != "agent" else "unknown"
                self.repo.update_run(ident, state, progress=str(exc)[:600])
        finally:
            if usage_token is not None:
                current_usage.reset(usage_token)
            self.launching.discard(ident)

    async def worker(self):
        from assistant_runtime.api.lifecycle import configuration_boundary

        await self.context.ready.wait()
        while True:
            await configuration_boundary(self.context)
            runs = self.repo.pending()
            for r in runs:
                upgrade = pending_upgrade(r)
                if (
                    r["state"] == "running"
                    and upgrade
                    and upgrade["phase"] == "ready"
                    and r["id"] not in self.upgrading
                ):
                    self.upgrading.add(r["id"])
                    self.context.spawn(self.upgrade_run(r["id"]), "model-handoff:" + r["id"])
            busy = [
                r
                for r in runs
                if r["state"] in {"running", "dispatching", "waiting_user"} or r["id"] in self.launching
            ]
            used = {
                x
                for r in runs
                if r in busy or r["state"] == "unknown"
                for x in r["data"]["spec"]["resources"]
            }
            for r in reversed(runs):
                if (
                    r["state"] == "queued"
                    and r["id"] not in self.launching
                    and r["data"].get("event_retry_at", 0) <= time.time()
                    and len(busy) < self.settings.concurrency
                    and not used.intersection(r["data"]["spec"]["resources"])
                ):
                    self.launching.add(r["id"])
                    busy.append(r)
                    used.update(r["data"]["spec"]["resources"])
                    self.context.spawn(self.launch(r), "execute:" + r["id"])
            for r in runs:
                if r["revision"] <= r["delivered"]:
                    continue
                if r["state"] in {"completed", "failed", "canceled", "unknown"}:
                    from assistant_runtime.api.decisions import report_decision

                    selection = r["data"]["spec"].get("execution", {}).get("selection", {})
                    decision = selection.get("model_review", {}).get("decision") or selection.get(
                        "decision_support", {}
                    )
                    await report_decision(
                        self.host.runtime,
                        decision,
                        "completed"
                        if r["state"] == "completed"
                        else "failed"
                        if r["state"] == "failed"
                        else "unknown",
                        "run_" + r["state"],
                        r["id"],
                    )
                if r["state"] in TERMINAL:
                    self.host.workfiles.finish(self.owner(r))
                    if not r["data"].get("completion_delivered"):
                        if (
                            r["id"] not in self.delivering_results
                            and len(self.delivering_results) < self.settings.concurrency
                        ):
                            self.delivering_results.add(r["id"])
                            self.context.spawn(self.deliver_result(r), "task-result:" + r["id"])
                        continue
                elif r["state"] in {"waiting_user", "unknown"} and not r["data"].get("ai_usage", {}).get(
                    "blocked"
                ):
                    # Budget attention is delivered once by ai_usage's durable outbox.
                    # User participation is distinct from routine completion preferences.
                    # Same structured request survives retries and progress-only revisions.
                    questions = r["data"].get("questions", [])
                    identity = hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest()[:24]
                    body = "\n".join(q["question"] for q in questions) or (
                        RUN_LABELS[r["state"]] + "。请打开任务查看详情。"
                    )
                    try:
                        await self.host.runtime.invoke(
                            NotificationRequest(
                                "publish",
                                {
                                    "event_key": f"task-attention:{r['id']}:{r['data'].get('completion_round', 0)}:{r['state']}:{identity}",
                                    "kind": "attention",
                                    "priority": "important",
                                    "title": r["data"]["spec"]["title"] + " · " + RUN_LABELS[r["state"]],
                                    "body": body[:8000],
                                },
                                r["id"],
                            )
                        )
                        self.notification_error = None
                    except Exception as exc:
                        self.notification_error = str(exc)[:240]
                        continue
                with self.repo.db:
                    self.repo.db.execute(
                        "UPDATE job_runs SET delivered=? WHERE id=?", (r["revision"], r["id"])
                    )
            self.archive_finished({r["task_id"] for r in runs if r["state"] in TERMINAL})
            await asyncio.sleep(0.5)

    @staticmethod
    def result_payload(r):
        return {
            "kind": "result",
            "event_key": f"task-result:{r['id']}:{r['data'].get('completion_round', 0)}",
            "title": r["data"]["spec"]["title"]
            + " · "
            + ("本次执行结束" if r["state"] == "completed" else RUN_LABELS[r["state"]]),
            "body": (r["data"].get("progress") or RUN_SUMMARIES[r["state"]])[:8000],
            "voice": r["data"]["spec"]["notify_voice"],
        }

    async def deliver_result(self, r):
        try:
            feedback_receipt = {"route": "disabled"}
            if r["data"]["spec"].get("notify_completion", True):
                feedback_receipt = await self.feedback.deliver(
                    r,
                    self.result_payload(r),
                )
                if feedback_receipt is None:
                    return
            # An asynchronous delivery cannot acknowledge a newer execution round.
            latest = self.repo.run(r["id"])
            if latest["state"] != r["state"] or latest["data"].get("completion_round", 0) != r["data"].get(
                "completion_round", 0
            ):
                return
            self.repo.update_run(
                r["id"],
                completion_delivered=True,
                completion_feedback=feedback_receipt,
                artifacts=self.host.workfiles.artifacts(self.owner(r)),
            )
            self.notification_error = None
        except Exception as exc:
            self.notification_error = str(exc)[:240]
            # Don't acknowledge the run revision until its outbox receipt is durable.
            # Stable event identity makes retry after a crash safe.
            return
        finally:
            self.delivering_results.discard(r["id"])

    async def upgrade_run(self, ident):
        try:
            await self.upgrades.apply(ident)
        finally:
            self.upgrading.discard(ident)

    async def finalize_once(self, ident):
        lock = self.run_locks.setdefault(ident, asyncio.Lock())
        async with lock:
            run = self.repo.run(ident)
            receipt = self.repo.finalization(ident)
            if (
                run["state"] not in TERMINAL
                or not receipt
                or receipt["state"] != "pending"
                or receipt["completion_round"] != run["data"].get("completion_round", 0)
                or receipt["next_at"] > time.time()
            ):
                return
            try:
                await self.executor.finalize(run)
            except Exception as exc:
                self.repo.finalize_receipt(run, exc)
            else:
                self.repo.finalize_receipt(run)

    async def finalize_finished(self):
        while True:
            for ident in self.repo.finalizations():
                await self.finalize_once(ident)
            await asyncio.sleep(0.5)

    async def watch_events(self):
        while True:
            if not self.accepting_schedule:
                await asyncio.sleep(0.2)
                continue
            for d in self.repo.definitions():
                if d["state"] != "enabled" or d["spec"]["trigger"]["kind"] != "event":
                    continue
                e = d["spec"]["trigger"]["event"]
                if e["source"] == "records":
                    continue
                row = self.repo.db.execute(
                    "SELECT data FROM job_event_cursor WHERE task_id=?", (d["id"],)
                ).fetchone()
                cursor = json.loads(row[0]) if row else {"active": False, "since": None, "last": 0, "run": ""}
                match, key = False, ""
                if e["source"] == "task":
                    targets = [
                        r
                        for r in self.repo.runs(e["id"])
                        if r["created"] >= d["created"]
                        and r["state"] == e["value"]
                        and not self.repo.db.execute(
                            "SELECT 1 FROM job_runs WHERE task_id=? AND occurrence=?",
                            (d["id"], "event:" + r["id"]),
                        ).fetchone()
                    ]
                    if targets:
                        key = targets[-1]["id"]
                        match = True
                        cursor["active"] = False
                else:
                    try:
                        device = self.host.devices.snapshot(e["id"])
                    except ValueError:
                        continue
                    if e.get("metric"):
                        reading = device.get("readings", {}).get(e["metric"], {})
                        value = reading.get("value") if reading.get("fresh") else None
                    else:
                        value = device.get("state") if device.get("fresh") else None
                    if value is not None:
                        threshold, op = e.get("value"), e.get("operator", "eq")
                        h = e.get("hysteresis", 0) if cursor["active"] else 0
                        match = (
                            value == threshold
                            if op == "eq"
                            else value > threshold - h
                            if op == "gt"
                            else value < threshold + h
                        )
                now = time.time()
                if match:
                    cursor["since"] = cursor["since"] or now
                    if (
                        not cursor["active"]
                        and now - cursor["since"] >= e.get("duration_seconds", 0)
                        and now - cursor["last"]
                        >= e.get("cooldown_seconds", 0 if e["source"] == "task" else 60)
                    ):
                        if not self.repo.active_runs(d["id"]):
                            with self.repo.db:
                                self.repo.enqueue(d, "event:" + (key or uuid.uuid4().hex))
                                cursor.update(active=True, last=now, run=key)
                                self.repo.db.execute(
                                    "INSERT OR REPLACE INTO job_event_cursor VALUES (?,?)",
                                    (d["id"], json.dumps(cursor)),
                                )
                else:
                    cursor.update(active=False, since=None)
                with self.repo.db:
                    self.repo.db.execute(
                        "INSERT OR REPLACE INTO job_event_cursor VALUES (?,?)", (d["id"], json.dumps(cursor))
                    )
            await asyncio.sleep(1)

    def maintenance_pause(self):
        self.draining = True
        self.drain_plans = {
            d["id"]
            for d in self.repo.definitions()
            if d["state"] == "enabled"
            and d["next_at"] is not None
            and d["spec"]["trigger"]["kind"] == "immediate"
        }

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationPending
        from assistant_runtime.core.configuration.updates import attributes

        from dataclasses import replace

        settings = TaskSettings.parse(config.tasks)
        capacity_only = replace(self.settings, concurrency=settings.concurrency) == settings

        def ready():
            return capacity_only or not (
                self.launching
                or self.delivering_results
                or any(
                    run["state"] in {"dispatching", "running", "waiting_user"} for run in self.repo.pending()
                )
            )

        if not ready():
            raise ConfigurationPending()
        return attributes(
            [(self, "settings", settings), (self, "model_policy", ModelPolicy.parse(settings.model_policy))],
            ready=ready,
        )

    def maintenance_resume(self):
        self.draining = False
        self.drain_plans.clear()
        for definition in self.repo.definitions():
            self.schedule(definition)

    async def maintenance_cancel(self):
        # Materialize accepted immediate occurrences so cancellation has a durable receipt;
        # otherwise their untouched schedule could launch them after the restart.
        for ident in list(self.drain_plans):
            self.due(ident)
        for run in self.repo.pending():
            if run["state"] not in {"unknown", "paused"}:
                await self.request(TaskRequest("cancel_run", {"run_id": run["id"]}))

    def maintenance_pending(self):
        pending = [{"id": row["id"], "state": row["state"]} for row in self.repo.pending()]
        pending.extend({"id": ident, "state": "scheduled_immediate"} for ident in self.drain_plans)
        pending.extend({"id": ident, "state": "finalizing"} for ident in self.repo.finalizations())
        pending.extend(
            {"id": row[0], "state": "model_review"}
            for row in self.repo.db.execute(
                "SELECT id FROM job_runs WHERE json_extract(data,'$.model_review.state') IN ('queued','running')"
            ).fetchall()
        )
        return pending

    async def close(self):
        self.started = False
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        self.host.backend.jobs = None

    def diagnostics(self):
        pending = self.repo.db.execute(
            "SELECT count(*) FROM job_finalizations WHERE state='pending'"
        ).fetchone()[0]
        failure = self.repo.db.execute(
            "SELECT error FROM job_finalizations WHERE state='pending' AND error IS NOT NULL ORDER BY updated DESC LIMIT 1"
        ).fetchone()
        return {
            "ready": self.started,
            "concurrency": self.settings.concurrency,
            "execution": self.settings.execution,
            "notification_error": self.notification_error,
            "finalizations_pending": pending,
            "finalization_error": failure[0] if failure else None,
        }
