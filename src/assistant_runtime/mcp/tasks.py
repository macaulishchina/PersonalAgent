"""Independent task MCP; thin typed capability mapping, no scheduler/provider code."""

from fastmcp import FastMCP
from ..kernel.contracts import TaskRequest
from .memory import READ, WRITE


def create_server(client, caller_run=None):
    server = FastMCP(
        "tasks",
        instructions="Manage durable background work without blocking the conversation. Create only user-authorized work. Get models before selecting execution.model and reasoning_effort; explain selection in execution.reason. The host may enforce fixed settings. Use stable UUID request_id for idempotent creation. Triggers: immediate; date with timezone-aware ISO at; interval with seconds; cron with expression/timezone; event with source(device/task), id, metric, operator(eq/gt/lt), value, duration_seconds, cooldown_seconds, hysteresis; or event with source=records, types (1..16 exact names), optional source_id/subject, merge_seconds (default 3), cooldown_seconds (30), max_age_seconds (300), capacity (128). Durable records subscriptions consume only new observations; no historical replay on create/resume/update. Baselines/corrections/deletions are not triggers. Query get_task_event_inbox and get_task_execution_trace to explain trigger decisions and execution. Execution kind: codex or reminder (no model). long_term uses a persistent Goal. Query run_id before steering/canceling; pause_task changes future scheduling only. Child tasks inherit scope. Never claim actions completed from memory.",
    )

    async def call(action, **arguments):
        return await client.invoke(TaskRequest(action, arguments, caller_run))

    @server.tool(annotations=READ)
    async def list_task_models() -> dict:
        """Available configured execution models/efforts, enforced settings and unified profile policy.

        Choose the lightest suitable profile using uncertainty, known procedures and acceptance
        checks, not task length or the parent model. Pass execution={profile:fast|balanced|deep|ultra,
        reason:...}; concrete model/effort overrides are allowed with a reason and validated.
        Profile and reasoning_effort are independent: select either or both using this catalog.
        Declare model_source="assistant" for your own model/profile recommendation and
        effort_source="assistant" for your own explicit effort. Use "user" only when the
        user explicitly chose that field. Never label your inference as a user choice.
        With review_mode=active, Jev may correct assistant recommendations using legal pairs;
        user choices, instance-fixed fields and unlabelled explicit choices remain protected.
        A missing/auto field is selectable; a profile default is not an effort lock.
        off/shadow retain the existing missing-field support without overriding explicit choices.
        Reserve ultra for exceptionally complex reasoning or original scientific research with
        concrete justification that deep is insufficient; ordinary research search/summaries
        do not qualify. Use it sparingly.
        Missing/auto fields use instance profiles, never inherit the foreground/parent model.
        Include execution.assessment={acceptance:concrete checks, procedure:known steps and limits,
        uncertainties:[unresolved facts], user_preference:exact user model/latency preference if any,
        prior_run_id:related execution ID if retrying}. Do not claim familiar workflow when prior
        evidence conflicts. The host reads prior outcomes itself; completion is not goal success.
        All supplied assessment fields and model choices are assistant statements, not authority.
        review_mode selects off, shadow (asynchronous observation) or active (one bounded
        pre-execution decision). Failure/timeout preserves your valid original recommendation.
        Inspect execution.selection.model_review and the run execution trace. No running-model
        changes or action replays; unrelated task edits do not trigger reselection.

        """
        return await call("models")

    @server.tool(annotations=WRITE)
    async def create_task(
        request_id: str,
        title: str,
        prompt: str,
        trigger: dict | None = None,
        execution: dict | None = None,
        long_term: bool = False,
        workspace: str | None = None,
        resources: list[str] | None = None,
        notify_voice: bool = False,
        notify_completion: bool | None = None,
        auto_archive: bool | None = None,
        self_schedule: dict | None = None,
        self_completion: dict | None = None,
    ) -> dict:
        """Persist authorized work. One-shot non-Goal tasks default to auto_archive=true: after success, failure, cancellation or a skipped occurrence they move to history, retaining results. Set false only when the user wants to keep the task reusable. Unfinished/waiting/uncertain tasks stay visible. Immediate one-shot work is categorized as temporary; scheduled and Goal work stays in plans. A multi-step task runs independently; do not also execute it in foreground. Use kind=reminder for simple notifications. Include authorized scope/completion criteria in prompt. All children must stay within the parent's scope. notify_completion controls automatic end-of-run notifications (including failures); notify_voice only adds speech when completion notifications are enabled. Durable event subscriptions default to notify_completion=false; other tasks default true. For monitoring, set notify_completion=false and publish notifications only for meaningful new events; unchanged checks stay silent. Only an explicitly user-authorized reusable timed plan may set self_schedule={allowed_triggers:[exact interval/date/cron choices], condition:when the user permits change}. For authorized automatic ending, self_completion={condition:concrete stopping criteria} permits complete_own_task after verification. No grant is inferred from a prompt; children cannot receive either permission."""
        spec = {
            "title": title,
            "prompt": prompt,
            "trigger": trigger or {"kind": "immediate"},
            "execution": execution or {},
            "long_term": long_term,
            "resources": resources or [],
            "notify_voice": notify_voice,
        }
        if notify_completion is not None:
            spec["notify_completion"] = notify_completion
        if auto_archive is not None:
            spec["auto_archive"] = auto_archive
        if self_completion is not None:
            spec["self_completion"] = self_completion
        if self_schedule is not None:
            spec["self_schedule"] = self_schedule
        if workspace:
            spec["workspace"] = workspace
        return await call("create", request_id=request_id, spec=spec)

    @server.tool(annotations=READ)
    async def list_tasks(
        query: str = "",
        state: str | None = None,
        view: str = "all",
        include_archived: bool = False,
        active_only: bool = False,
        before: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Search a bounded page of definitions and latest/unfinished execution summaries. view: all/temporary/planned. Temporary defaults to unfinished non-archived work; include_archived=true includes all temporary results. Planned defaults to non-archived plans, including paused and terminated; include_archived=true also includes archived plans. active_only=true selects enabled plans with future triggers, event listening or unfinished executions. Paused/archived/terminated plans and ended one-shots are inactive. Pass the returned before cursor for older tasks (limit 1–50); get_task provides complete results."""
        return await call(
            "list",
            query=query,
            state=state,
            view=view,
            include_archived=include_archived,
            active_only=active_only,
            before=before,
            limit=limit,
        )

    @server.tool(annotations=READ)
    async def get_task(task_id: str, before: str | None = None, limit: int = 20) -> dict:
        """Get a page of runs (limit 1–50; use returned before for older runs), plus a separate bounded active_runs snapshot, definition revision, execution IDs, model choices, progress, questions and results. Run data includes started_at, finished_at and duration_seconds when measured. Duration excludes queue and explicit pauses, includes waiting for answers. Top-level attachments are registered media from attachment_run_id (the page's newest run), displayed by the conversation UI. An empty attachment list is not a delivered picture; do not claim one was sent from prose alone. Older run media stays associated with that run."""
        return await call("get", task_id=task_id, before=before, limit=limit)

    @server.tool(annotations=READ)
    async def get_task_event_inbox(task_id: str) -> dict:
        """Bounded pending event references, subscription cursor/health, decision counters and recent skip/assignment reasons. No raw event payload copy."""
        return await call("event_inbox", task_id=task_id)

    @server.tool(annotations=READ)
    async def get_task_execution_trace(run_id: str, after: int = 0, limit: int = 50) -> dict:
        """Trigger event references, run/provider identity, latest 20 schedule-change receipts and paginated tool metadata (max 100/page, 500/run). Tool completion and executor-reported schedule evidence are not proof of a physical outcome. Tool arguments, commands and output are not copied into trace metadata."""
        return await call("trace", run_id=run_id, after=after, limit=limit)

    @server.tool(annotations=WRITE)
    async def update_task(task_id: str, revision: int, changes: dict) -> dict:
        """Foreground management only; background callers must use update_own_task_schedule for preauthorized scheduling. Change future executions with optimistic revision checking; use changes={"notify_completion": false} to disable completion notifications, true to enable. notify_voice independently controls spoken delivery when enabled. Explicit user authorization may grant/revoke self_schedule. Active executions keep their creation-time settings; use supplement for active work."""
        return await call("update", task_id=task_id, revision=revision, changes=changes)

    @server.tool(annotations=WRITE)
    async def update_own_task_schedule(
        request_id: str,
        revision: int,
        trigger: dict,
        reason: str,
        evidence: str,
    ) -> dict:
        """Change only this execution's future schedule, within explicit self_schedule authorization.

        Read your own get_task for its current revision and self_schedule grant. Only use a
        preauthorized trigger after its condition is met; provide concrete evidence, never
        treat a tool response as proof of the goal. The host derives identity from the bound
        background run; no task/run target is accepted. No prompt, model, resources, policy,
        notification or current-run changes. User edits/pause/end block old executions.
        Reuse the same canonical UUID and identical arguments after a lost response; never
        generate a new request ID to bypass a conflict. Confirm next_at from the receipt.
        """
        return await call(
            "reschedule_self",
            request_id=request_id,
            revision=revision,
            trigger=trigger,
            reason=reason,
            evidence=evidence,
        )

    @server.tool(annotations=WRITE)
    async def complete_own_task(request_id: str, revision: int, reason: str, evidence: str) -> dict:
        """Archive this plan only after its explicit self_completion condition is verified.

        Bound running identity, captured/current authorization and revision must agree.
        Stops future scheduling, leaves this execution alive to finish reporting. Evidence
        is executor-reported, not independently proved by this tool. Missing search results
        are not proof that a series ended. Reuse UUID after a lost acknowledgment.
        """
        return await call(
            "complete_self", request_id=request_id, revision=revision, reason=reason, evidence=evidence
        )

    @server.tool(annotations=WRITE)
    async def run_task(task_id: str, request_id: str) -> dict:
        """Explicit new execution/retry with stable UUID; unfinished work prevents overlapping runs."""
        return await call("run", task_id=task_id, request_id=request_id)

    @server.tool(annotations=WRITE)
    async def pause_task(task_id: str) -> dict:
        """Pause future triggers; active executions continue."""
        return await call("pause", task_id=task_id)

    @server.tool(annotations=WRITE)
    async def resume_task(task_id: str) -> dict:
        """Resume future triggers without replaying missed executions."""
        return await call("resume", task_id=task_id)

    @server.tool(annotations=WRITE)
    async def terminate_task(task_id: str) -> dict:
        """End a task and its children, cancel current execution and stop future triggers."""
        return await call("terminate", task_id=task_id)

    @server.tool(annotations=WRITE)
    async def archive_task(task_id: str) -> dict:
        """Archive a task, retaining history; stop unfinished work and future triggers."""
        return await call("archive", task_id=task_id)

    @server.tool(annotations=WRITE)
    async def cancel_task_run(run_id: str) -> dict:
        """Cancel only this execution, including its owned terminals; preserve future schedule."""
        return await call("cancel_run", run_id=run_id)

    @server.tool(annotations=WRITE)
    async def pause_task_run(run_id: str) -> dict:
        """Interrupt at current state; arbitrary external processes cannot be checkpointed automatically."""
        return await call("pause_run", run_id=run_id)

    @server.tool(annotations=WRITE)
    async def continue_task_run(run_id: str, text: str) -> dict:
        """Explicitly continue stopped/uncertain work; first reconcile existing results, never blindly repeat actions."""
        return await call("continue", run_id=run_id, text=text)

    @server.tool(annotations=WRITE)
    async def supplement_task(run_id: str, text: str) -> dict:
        """Send a correction or answer to this running/waiting task only."""
        return await call("supplement", run_id=run_id, text=text)

    @server.tool(annotations=WRITE)
    async def request_task_model_upgrade(
        run_id: str, request_id: str, profile: str, cause: str, reason: str, evidence: str, checkpoint: dict
    ) -> dict:
        """Request a bounded higher-profile handoff for THIS active run, then finish the current turn.

        Reuse UUID after a lost reply. No immediate switch or replay. cause: reasoning_limit,
        conflicting_evidence, unfamiliar_workflow. Offline devices, timeouts and missing tools
        do not justify upgrades. For ultra, explain why deep is insufficient for this unusually
        complex reasoning/research problem. Ordinary research search/summaries do not qualify.
        checkpoint={completed:[facts/operation IDs], next_step:text,
        uncertain_operations:[], active_operations:[]}; reconcile/wait before requesting, never
        include credentials. Background caller may target only itself. Native Goal runs and
        instance-fixed settings do not permit automatic handoffs. Host waits for a clean completed
        turn, no active tools/terminals, then resumes the same thread with checkpoint/history.
        """
        return await call(
            "upgrade_model",
            run_id=run_id,
            request_id=request_id,
            profile=profile,
            cause=cause,
            reason=reason,
            evidence=evidence,
            checkpoint=checkpoint,
        )

    @server.tool(annotations=READ)
    async def list_task_artifacts(run_id: str) -> dict:
        """List retained deliverables of a specific execution."""
        return await call("artifacts", run_id=run_id)

    return server
