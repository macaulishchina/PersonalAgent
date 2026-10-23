# Public single-execution capability

The `execution` core plugin provides `ExecutionRequest` through the existing runtime.
It uses the same Pi client and metered transport as the current conversation and tasks.
It owns individual execution receipts and a bounded queue; it does not schedule recurring
work, deliver notifications or implement an agent loop.

Enable `execution` explicitly in `plugins`. Optional settings:

```toml
[execution]
concurrency = 4
queue_size = 64
```

The application now assembles only the declared plugins. `init --preset slim` enables
this capability with models, Pi, usage and workfiles; no assistant state, conversation,
device service or TCP listener is created. `standard` enables the same capability plus
the existing assistant entrance. Conversation, scheduled tasks and direct submissions
share the same bounded admission, attempt receipts, cancellation and terminal-event rules.
Every production `run.start` passes the execution plugin's coordinator before the native
adapter dispatches it. The adapter also remains independently testable without an application.
Conversation/task rows are business projections and retain the canonical `execution_id`;
task scheduling and Goal policy are not confused with the completion of a single attempt.

## Calling

Both deployment compositions use the same CLI. The implemented command is:

```sh
personal-agent --config /absolute/instance/config/config.toml executions submit \
  '{"request_id":"7a45c50b-54b3-40b7-a9b3-0b4fdad87102","text":"Summarize the supplied instructions"}'
personal-agent --config /absolute/instance/config/config.toml executions get \
  '{"id":"7a45c50b-54b3-40b7-a9b3-0b4fdad87102"}'
```

`submit` accepts text, a stable request UUID, optional configured model and effort,
`inputs` (registered file IDs), `tools` (exact tool names) and `skills` (enabled skill names).
Omitting tools/skills uses the enabled instance inventory; an empty list selects none.
Selection only narrows configured availability. Tool declarations and actual calls are
both constrained in the Pi adapter; this is not an OS sandbox. Built-in tool names are
`read`, `write`, `edit`, `bash`, `grep`, `find`, `ls`, `request_user_input`; plugin tools use
`mcp__<server>__<tool>`. Selected tools must exist on the configured server; a missing
remote tool still returns its native unavailable result. Skills are captured per session
and survive its native restart without expanding to all instance skills.

Use the shared `files put|get` CLI or `FileRequest` to register bytes first. See the
[workfiles contract](../workfiles/README.md) for ownership, limits and image requirements.
The receipt lists input metadata and the requested tool/skill selections. No credentials, endpoint or caller identity
can be provided in the request. Local operator calls are trusted; plugin calls inherit
host-bound ownership and the current work budget.

Other operations: `events` (`id`, optional `cursor`/`limit`), `cancel` (`id`),
`supplement` (`id`, `text`), and `answer` (`id`, current `question_id`, `text`). The returned
question identifier is required so a delayed answer cannot accidentally answer a newer
question. Plugins declare `uses=(ExecutionRequest,)` and invoke the same operations.

Receipts include state, model, effort, result, usage and registered work-area artifact names.
Scratch files and deliverables use `state/work/executions/<id>/scratch` and `artifacts`.
Events retain the most recent 512 entries per execution, at most 16 KiB each; oversized
event payloads are explicitly marked truncated. Cursor queries return the earliest retained
cursor. Final text is bounded to 65,536 characters with an explicit `result_truncated` flag;
artifacts carry larger deliverables.

## Lifecycle guarantees

A repeated request UUID returns the existing receipt; changing its content is an error.
The resolved model and reasoning level are checked before a run starts; mismatch fails
without paid dispatch. Unsupported executor interaction requests receive explicit errors
rather than waiting indefinitely. Queued cancellation never dispatches a model. Cancellation during dispatch remains pending
until the started run can be cancelled. Native cancellation failure is `unknown`, not success.
Unknown submission and shutdown attempt to cancel the owned session, without replaying work.
Executor disconnect immediately marks queued work interrupted and in-flight work unknown,
rejects new submissions, and wakes waiting workers. Shutdown during admission or session
creation cannot enqueue or launch new work after the ownership has ended.
Restart marks queued work interrupted and in-flight work unknown; neither is automatically
submitted again. Terminal results cannot be overwritten by late provider events.

Usage admission stays in `ai_usage` and Pi transport. A child submitted from a bound call
inherits its parent's work allowance; it does not receive a fresh budget. There is no added
daily request quota.

Tests cover duplicate submissions, queue saturation, ownership, shared budgets, cancellation
races, question identity and recovery. A real Pi subprocess with a synthetic local model
covers CLI transport, clarification, actual file creation, artifacts, usage, cancelling a
running shell command and persisted receipts. No real provider credentials are used.

The Pi adapter now supports transport startup without creating a foreground conversation.
An isolated real-Pi/synthetic-provider test exercises the execution capability in this mode,
without device services, conversation tables or assistant instructions. Both presets also run through the same application and CLI in synthetic integration
tests: clarification, artifacts, cancellation, usage and durable receipts are verified.

Real Pi/synthetic-model tests additionally verify registered file reads, native image input,
selected skill loading, exact tool declarations and rejection of a model-generated write
outside a read-only selection. Both presets use the same path. These scopes do not
replace future execution authorization, and shell access still has full local permissions.

The execution concurrency setting bounds active attempts across all entrances, not just
CLI submissions. Queued cancellation removes ownership before native dispatch; expired
or late completions cannot overwrite the canonical result or reopen business projections.
Task cancellation additionally retains its existing operation-settlement checks. Both
foreground and task results are linked to the common record; their public queries expose
the canonical receipt without duplicating an agent loop.

If dispatch becomes uncertain, the coordinator attempts bounded native cancellation.
An unconfirmed cancellation quarantines its concurrency slot until session closure or
service shutdown; it does not free capacity for an accumulating chain of uncertain runs.
The durable outcome remains unknown even if subsequent cleanup stops the process.

MCP definitions are discovered through Pi's native tool search, with the same bounded
loadout as conversation and tasks. `tools` is still the exact business-call ceiling;
the host adds the discovery infrastructure without allowing additional business tools.
See [Pi discovery](../pi/README.md#native-tool-discovery-and-bounded-declarations).
