"""Independent task MCP; thin typed capability mapping, no scheduler/provider code."""

from assistant_runtime.mcp.contracts import checked
from fastmcp import FastMCP
from typing import Annotated, Literal
from pydantic import Field, WithJsonSchema
from .tool_contracts import EXECUTION, TRIGGER, CHANGES, SELF_SCHEDULE, SELF_COMPLETION, CHECKPOINT
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api.annotations import READ, WRITE


def create_server(client, caller_run=None):
    server = FastMCP(
        "tasks",
        instructions="Manage durable background work without blocking the conversation. Create only user-authorized work. Get models before selecting execution.model and reasoning_effort; explain selection in execution.reason. The host may enforce fixed settings. Use stable UUID request_id for idempotent creation. Triggers: immediate; date with timezone-aware ISO at; interval with seconds; cron with expression/timezone; event with source(device/task), id, metric, operator(eq/gt/lt), value, duration_seconds, cooldown_seconds, hysteresis; or event with source=records, types (1..16 exact names), optional source_id/subject, merge_seconds (default 3), cooldown_seconds (30), max_age_seconds (300), capacity (128). Durable records subscriptions consume only new observations; no historical replay on create/resume/update. Baselines/corrections/deletions are not triggers. Query get_task_event_inbox and get_task_execution_trace to explain trigger decisions and execution. Execution kind: agent or reminder (no model). long_term uses a persistent Goal. Query run_id before steering/canceling; pause_task changes future scheduling only. Child tasks inherit scope. Never claim actions completed from memory.",
    )

    # Routing is positional-only: business fields may also be named action.
    async def call(action, /, **arguments):
        return await client.invoke(TaskRequest(action, arguments, caller_run))

    @server.tool(annotations=READ)
    async def list_task_models() -> dict:
        """Current main defaults, configured models/efforts and optional named recommendations.

        Omit execution.model/recommendation to use the main conversation model. Omitted
        effort inherits its concrete effort. In tasks, auto asks YOU (the initiating model)
        to choose a supported concrete reasoning_effort; if main effort is also auto,
        supply one explicitly. Models without adjustable reasoning use auto. Main
        conversation auto is independently provider-controlled.
        Optional execution={recommendation:ID, reasoning_effort:SUPPORTED, reason:WHY}
        selects a named suggestion; concrete execution.model is also supported. Recommendations
        have no built-in rank. Pick by the supplied reason, description and acceptance checks.
        Explicit model must match the selected recommendation; effort may override its default.
        Declare model_source/effort_source=assistant for your own concrete recommendations,
        user only for explicit user choices. Omit labels for missing/auto fields. Unlabelled
        explicit choices are protected. Free-form user_preference is context, not a lock.
        Include execution.assessment={acceptance,procedure,uncertainties:[facts],
        user_preference,prior_run_id} when useful. Prior evidence is read by the host;
        completion is not goal success. Optional decisions review may adjust assistant
        recommendations, never user/fixed choices. Unavailable/timeout preserves the valid
        original choice. No running model changes on unrelated edits; no fallback models.
        """
        return await call("models")

    @server.tool(annotations=WRITE)
    async def create_task(
        request_id: str,
        title: str,
        prompt: str,
        trigger: Annotated[dict, WithJsonSchema(TRIGGER), checked(TRIGGER, "trigger")] | None = None,
        execution: Annotated[dict, WithJsonSchema(EXECUTION), checked(EXECUTION, "execution")] | None = None,
        long_term: bool = False,
        workspace: str | None = None,
        resources: Annotated[
            list[str] | None,
            Field(
                description="Internal resource bindings for the user's authorized objective. Discover exact keys from the owning capability before delegation (media_capabilities.target_bindings for media). Match provider/account, not a guessed generic alias. Bind necessary scope yourself; users need not supply resource keys or repeat existing authorization. Children must stay within parent resources."
            ),
        ] = None,
        notify_voice: bool = False,
        notify_completion: bool | None = None,
        feedback_owner: Literal["conversation", "background"] = "background",
        auto_archive: bool | None = None,
        self_schedule: Annotated[dict, WithJsonSchema(SELF_SCHEDULE), checked(SELF_SCHEDULE, "self_schedule")]
        | None = None,
        self_completion: Annotated[
            dict, WithJsonSchema(SELF_COMPLETION), checked(SELF_COMPLETION, "self_completion")
        ]
        | None = None,
    ) -> dict:
        """Persist authorized work. One-shot non-Goal tasks default to auto_archive=true: after success, failure, cancellation or a skipped occurrence they move to history, retaining results. Set false only when the user wants to keep the task reusable. Unfinished/waiting/uncertain tasks stay visible. Immediate one-shot work is categorized as temporary; scheduled and Goal work stays in plans. A multi-step task runs independently; do not also execute it in foreground. Use kind=reminder for simple notifications. Include authorized scope/completion criteria in prompt. All children must stay within the parent's scope. notify_completion controls automatic end-of-run notifications (including failures); notify_voice only adds speech when completion notifications are enabled. feedback_owner=background hands result delivery to the background (default). Set conversation ONLY when you are explicitly retaining responsibility to wait for and answer this immediate task; its result is projected immediately into this conversation without a separate unread notice. Do not repeat that report verbatim; add only useful interpretation. If handing off and moving on, use background. get_task never transfers ownership. Use transfer_task_feedback with the returned execution round/version to explicitly take back or hand off an existing run. Ending/interruption of the owning turn releases undelivered results; no timer decides ownership. Durable event subscriptions default to notify_completion=false; other tasks default true. For monitoring, set notify_completion=false and publish notifications only for meaningful new events; unchanged checks stay silent. Only an explicitly user-authorized reusable timed plan may set self_schedule={allowed_triggers:[exact interval/date/cron choices], condition:when the user permits change}. For authorized automatic ending, self_completion={condition:concrete stopping criteria} permits complete_own_task after verification. No grant is inferred from a prompt; children cannot receive either permission."""
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
        return await call("create", request_id=request_id, spec=spec, feedback_owner=feedback_owner)

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
        """Get a page of runs (limit 1–50; use returned before for older runs), plus a separate bounded active_runs snapshot, definition revision, execution IDs, model choices, progress, questions and results. Each run.feedback exposes result owner, round, version and durable delivery receipt. Querying does not acquire ownership or acknowledge delivery. Run data includes started_at, finished_at and duration_seconds when measured. Duration excludes queue and explicit pauses, includes waiting for answers. Top-level attachments are registered media from attachment_run_id (the page's newest run), displayed by the conversation UI. An empty attachment list is not a delivered picture; do not claim one was sent from prose alone. Older run media stays associated with that run."""
        return await call("get", task_id=task_id, before=before, limit=limit)

    @server.tool(annotations=WRITE)
    async def transfer_task_feedback(
        run_id: str, round: int, version: int, owner: Literal["conversation", "background"]
    ) -> dict:
        """Foreground only: explicitly transfer this execution's result delivery responsibility.

        Read get_task's run.feedback for round/version. owner=conversation takes responsibility
        in the CURRENT server-bound turn; background hands it off. Mere interest/progress queries
        do not transfer. A delivered/committed result returns transferred=false and cannot be
        recalled or re-notified. Conflict: reread before deciding again. No execution or schedule
        is changed. After taking ownership keep this turn active until the result arrives; the
        framework publishes it directly, without waiting for model prose. Ended turns release it.
        """
        return await call("transfer_feedback", run_id=run_id, round=round, version=version, owner=owner)

    @server.tool(annotations=READ)
    async def get_task_event_inbox(task_id: str) -> dict:
        """Bounded pending event references, subscription cursor/health, decision counters and recent skip/assignment reasons. No raw event payload copy."""
        return await call("event_inbox", task_id=task_id)

    @server.tool(annotations=READ)
    async def get_task_execution_trace(run_id: str, after: int = 0, limit: int = 50) -> dict:
        """Trigger event references, run/provider identity, latest 20 schedule-change receipts and paginated tool metadata (max 100/page, 500/run). Tool completion and executor-reported schedule evidence are not proof of a physical outcome. Tool arguments, commands and output are not copied into trace metadata."""
        return await call("trace", run_id=run_id, after=after, limit=limit)

    @server.tool(annotations=WRITE)
    async def update_task(
        task_id: str,
        revision: int,
        changes: Annotated[dict, WithJsonSchema(CHANGES), checked(CHANGES, "changes")],
    ) -> dict:
        """Foreground management only; background callers must use update_own_task_schedule for preauthorized scheduling. Patch future executions with optimistic revision checking; omitted nested fields stay unchanged, null clears optional fields and lists replace; use changes={"notify_completion": false} to disable completion notifications, true to enable. notify_voice independently controls spoken delivery when enabled. Explicit user authorization may grant/revoke self_schedule. Active executions keep their creation-time settings; use supplement for active work."""
        return await call("update", task_id=task_id, revision=revision, changes=changes)

    @server.tool(annotations=WRITE)
    async def repair_task_run_resources(
        run_id: str, revision: int, resources: list[str], reason: str
    ) -> dict:
        """Foreground only: correct internal resource bindings using EXISTING user authorization.

        Read get_task for the run revision. resources replaces this run's complete resource list;
        reason must identify the original user request and explain the binding mistake.
        Do not ask users to name internal resource keys or repeat an already authorized request.
        Discover exact resource keys from the owning capability before binding; never guess aliases.
        Pause running work first and confirm it stopped. Only queued, paused or waiting executions
        with no active turn can be repaired; child/unknown/ended executions are rejected.
        No resumption, tool replay, future-definition edit or unrelated grant occurs. Inspect pending
        operations before continue/supplement. For future runs use update_task; for an ended task
        create a scoped follow-up from its evidence and reconcile existing operations first.
        New objectives or destructive actions outside the original request still need user approval.
        """
        return await call(
            "repair_resources", run_id=run_id, revision=revision, resources=resources, reason=reason
        )

    @server.tool(annotations=WRITE)
    async def update_own_task_schedule(
        request_id: str,
        revision: int,
        trigger: Annotated[dict, WithJsonSchema(TRIGGER), checked(TRIGGER, "trigger")],
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
        run_id: str,
        request_id: str,
        recommendation: str,
        cause: str,
        reason: str,
        evidence: str,
        checkpoint: Annotated[dict, WithJsonSchema(CHECKPOINT), checked(CHECKPOINT, "checkpoint")],
        reasoning_effort: str | None = None,
    ) -> dict:
        """Request a bounded handoff to a named recommendation for THIS active run, then finish the current turn.

        Reuse UUID after a lost reply. No immediate switch or replay. cause: reasoning_limit,
        conflicting_evidence, unfamiliar_workflow. Offline devices, timeouts and missing tools
        do not justify upgrades. Recommendations have no implicit order: explain why the
        selected model/effort is better suited. For auto effort choose a concrete supported value.
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
            recommendation=recommendation,
            **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
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
