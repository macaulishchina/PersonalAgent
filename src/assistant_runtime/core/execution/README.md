# Public single-execution capability

The `execution` core plugin provides `ExecutionRequest` through the existing runtime.
It uses the same Pi client and metered transport as the current conversation and tasks.
It owns individual execution receipts and an unbounded waiting queue; it does not schedule recurring
work, deliver notifications or implement an agent loop.

Enable `execution` explicitly in `plugins`. Optional settings:

```toml
[execution]
concurrency = 16
```

`concurrency` accepts 1..64. It bounds active execution attempts across conversations,
tasks, direct calls and auxiliary inference. Session registration persists metadata without starting a
process and is not capped by an unrelated idle-session count. A larger configured
maximum is not a hardware recommendation: Pi process memory, context sizes and
upstream provider quotas still matter.

`ExecutionRequest("capacity", {})` is a read-only, instance-wide count summary:
`limit`, `occupied` (including quarantined leases), `queued`,
`waiting_user`, `unknown` and `quarantined`.
Native model transport reuses the admitted execution slot. Independent inference uses
the same host-owned pool; trusted nested inference borrows its parent slot serially
instead of waiting for a second slot. Cancelling a parent retains the slot until an
in-flight borrowed call settles. There is no separate model or background concurrency
setting. Waiting is limited by process memory and caller deadlines, not a queue-length
configuration. Per-conversation order, resource locks and usage policy still apply.

It returns no execution identities or content. Unknown historical outcomes need
not occupy a current lease; `quarantined` identifies the subset that still does.

The application now assembles only the declared plugins. `init --preset slim` enables
this capability with models, Pi, usage, workfiles, conversation and CLI; no personal
assistant, device or voice service or chat TCP listener is created. `standard` enables the same capability plus
the existing assistant entrance. Conversation, scheduled tasks and direct submissions
share the same bounded admission, attempt receipts, cancellation and terminal-event rules.
Every production `run.start` passes the execution plugin's coordinator before the native
adapter dispatches it. The adapter also remains independently testable without an application.
Conversation/task rows are business projections and retain the canonical `execution_id`;
task scheduling and Goal policy are not confused with the completion of a single attempt.

## Calling

Both deployment compositions use the same CLI. The implemented command is:

```sh
pa --config /absolute/instance/config/config.toml executions submit \
  '{"request_id":"7a45c50b-54b3-40b7-a9b3-0b4fdad87102","text":"Summarize the supplied instructions"}'
pa --config /absolute/instance/config/config.toml executions get \
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
`supplement` (`id`, stable UUID `message_id`, `text`), and `answer`
(`id`, stable UUID `message_id`, current `question_id`, `text`). Both return a message
receipt; query it with `message_get` (`id`, `message_id`). The returned
question identifier is required so a delayed answer cannot accidentally answer a newer
question. Plugins declare `uses=(ExecutionRequest,)` and invoke the same operations.

Receipts include state, model, effort, result, usage and registered work-area artifact names.
Scratch files and deliverables use `state/work/executions/<id>/scratch` and `artifacts`.
Events retain the most recent 512 entries per execution, at most 16 KiB each; oversized
event payloads are explicitly marked truncated. Cursor queries return the earliest retained
cursor, latest cursor, deleted-through watermark, `gap`, `has_more` and execution state.
Global sequence gaps caused by other executions are not data loss. For rows created before
watermark tracking, `history_origin_known=false`; a query into that unknown prefix returns
`gap=null`, not a fabricated complete history. Oversized payloads retain bounded object IDs.
Final text is bounded to 65,536 characters with an explicit `result_truncated` flag;
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

The task entrance has one evidence-based exception: a proven pending clarification
checkpoint remains waiting across restart without inference. No old unknown record is
reopened. Task waits release active execution capacity; explicit answers reacquire it
and use the same durable message receipts, turn and work allowance. Missing/corrupt
checkpoints or another unresolved operation still become unknown. Direct execution and
conversation interruptions retain the rules above.

Usage admission stays in `ai_usage` and Pi transport. A child submitted from a bound call
inherits its parent's work allowance; it does not receive a fresh budget. There is no added
daily request quota.

Tests cover duplicate submissions, unbounded waiting, ownership, shared budgets, cancellation
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
See [Pi discovery](../pi/README.md#统一工具调用边界).

## Durable input receipts (API 2)

Supplementary text and clarification answers require a caller-generated stable UUID
`message_id`. Same ID/content/execution returns its original receipt; changed content or
another execution is rejected. The current host CallIdentity is recorded as call evidence,
not inferred project authorization. At most 64 unconsumed inputs may be pending per run.

- `accepted`: persisted and queued/submitted to the engine; not yet observed in a model request.
- `consumed`: matched the exact host-marked input in a specific Pi model-request context;
  includes `turn_id`, durable usage `model_request_id`, and `consumed_at`.
- `not_consumed`: known not dispatched, for example a new supplement arriving after termination.
- `unknown`: dispatch or consumption cannot be proved; never automatically re-send.

A small host-generated correlation marker is included in the user/tool input. The Pi adapter
matches role, body and (for answers) native tool-call identity at the metered model-input
boundary. This proves inclusion in that local model request, not remote acceptance,
understanding, use or business completion. Unobservable transformations/compaction or
interruption may leave a message unknown. No claim of supplier-internal exactly-once delivery.

The durable intent is written before dispatch. A lost response, duplicate query, restart or
late acknowledgement cannot send the input twice or clear a newer clarification question.
Terminal execution and unresolved message settlement share a database transaction.

Use `files finalize` for immutable publications rather than retaining the mutable artifact
names in a run receipt; see the workfiles contract. Unknown executions keep their scratch
area retained pending future explicit settlement; they do not enter timed cleanup.

API 2 requires the stable message ID and returns a message receipt. API 1 integrations must adapt before declaring compatibility.


## Operation observations

`observations` accepts `id`, optional `cursor` and `limit` (1..100). Its append-only
records are separate from the rolling UI event window. Native model requests (including
compaction) use their usage-ticket identity; tool calls get execution-scoped stable IDs,
with their parent request when observed. Arguments, file contents and credentials are
not copied into this journal. It records only the Pi native observation boundary, not
arbitrary plugin I/O or external services.

`reconcile` accepts `id` and appends a scope-specific observation. The response includes
recorded model-operation counts and the last observed local leader, with a host startup
ID and a per-launch ID. A PID is not used as sufficient identity. Local leader exit is
not process-tree, write-fence or remote-stop proof. The current full-permission backend
therefore reports `local_writes=unfenced`, missing scopes as `unknown`, and
`safe_to_retry=false`. The controlled broker must supply stronger evidence before this
can allow a conflicting retry. Controlled contexts add broker fencing; legacy full-permission execution stays unfenced.

Terminating an execution marks unsettled operations unknown. A late completion appends
evidence without rewriting that operation or the original execution outcome. Observations
survive restart; the new process has a distinct startup ID. These are observations, not
an authorization to access resources or a substitute for an upstream role decision.


## Controlled contexts

The injected `execution_control` resource binds the trusted installed controller. It is unavailable to model calls and disabled by default through an empty `execution.controllers` allowlist. See the complete [control API and lifecycle](../../../../docs/development/controlled-execution.md). Context, workspace, grant, execution, operation, usage scope and immutable publication identities are distinct. Windows fixed-command isolation is explicitly selected by this control API, never inferred from ordinary execution or a platform tag.
