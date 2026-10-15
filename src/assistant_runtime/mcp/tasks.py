"""Independent task MCP; thin typed capability mapping, no scheduler/provider code."""

from fastmcp import FastMCP
from ..kernel.contracts import TaskRequest
from .memory import READ, WRITE


def create_server(client, caller_run=None):
    server = FastMCP(
        "tasks",
        instructions="Manage durable background work without blocking the conversation. Create only user-authorized work. Get models before selecting execution.model and reasoning_effort; explain selection in execution.reason. The host may enforce fixed settings. Use stable UUID request_id for idempotent creation. Triggers: immediate; date with timezone-aware ISO at; interval with seconds; cron with expression/timezone; event with source(device/task), id, metric, operator(eq/gt/lt), value, duration_seconds, cooldown_seconds, hysteresis. Execution kind: codex or reminder (no model). long_term uses a persistent Goal. Query run_id before steering/canceling; pause_task changes future scheduling only. Child tasks inherit scope. Never claim actions completed from memory.",
    )

    async def call(action, **arguments):
        return await client.invoke(TaskRequest(action, arguments, caller_run))

    @server.tool(annotations=READ)
    async def list_task_models() -> dict:
        """Available subscription models, reasoning levels and enforced task settings."""
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
    ) -> dict:
        """Persist authorized work. A multi-step task runs independently; do not also execute it in foreground. Use kind=reminder for simple notifications. Include authorized scope/completion criteria in prompt. All children must stay within the parent's scope."""
        spec = {
            "title": title,
            "prompt": prompt,
            "trigger": trigger or {"kind": "immediate"},
            "execution": execution or {},
            "long_term": long_term,
            "resources": resources or [],
            "notify_voice": notify_voice,
        }
        if workspace:
            spec["workspace"] = workspace
        return await call("create", request_id=request_id, spec=spec)

    @server.tool(annotations=READ)
    async def list_tasks(query: str = "", state: str | None = None) -> dict:
        """Search definitions and recent execution states, including next trigger times."""
        return await call("list", query=query, state=state)

    @server.tool(annotations=READ)
    async def get_task(task_id: str) -> dict:
        """Get definition revision, execution IDs, model choices, progress, questions and results."""
        return await call("get", task_id=task_id)

    @server.tool(annotations=WRITE)
    async def update_task(task_id: str, revision: int, changes: dict) -> dict:
        """Change future executions with optimistic revision checking; use supplement for active work."""
        return await call("update", task_id=task_id, revision=revision, changes=changes)

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

    @server.tool(annotations=READ)
    async def list_task_artifacts(run_id: str) -> dict:
        """List retained deliverables of a specific execution."""
        return await call("artifacts", run_id=run_id)

    return server
