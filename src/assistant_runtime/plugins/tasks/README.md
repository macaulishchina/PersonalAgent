# Tasks plugin

Trusted local plugin for independent background work on the existing ChatGPT subscription.
Enable `notifications` and `tasks` in the instance `plugins` list and install the `tasks` extra in the existing `.venv`.
Requires the conversation and configured brain capabilities. Does not activate the microphone.

## Configuration

```toml
[tasks]
timezone = "Asia/Shanghai"
concurrency = 2
misfire_seconds = 300
[tasks.execution]
model = "auto"
reasoning_effort = "auto"

[tasks.model_policy]
default_profile = "fast"
max_upgrades = 1
[tasks.model_policy.profiles.fast]
model = "gpt-6-luna"
reasoning_effort = "low"
[tasks.model_policy.profiles.balanced]
model = "gpt-6.1-sol"
reasoning_effort = "low"
[tasks.model_policy.profiles.deep]
model = "gpt-6.1-sol"
reasoning_effort = "medium"

[work_files]
retention_days = 7
# root = "/absolute/private/instance/work"
```

`auto` is an application policy, never sent as a model name to Codex. The initiating
model reads `list_task_models` for current models, supported efforts, profile availability
and shared guidance. Choose the lightest suitable profile using uncertainty, existing
procedures and acceptance checks; step count or wall time alone does not imply difficult
reasoning. There is no classifier model call, task-keyword routing or model per button.
Use `execution={"profile":"fast","reason":"known steps; read back the resulting state"}`.
Concrete model/effort choices remain supported with a nonempty reason (max 400 chars).
Missing/auto values use the configured profile, never the foreground or parent model.
Fixed instance settings override requests. The resolved model/effort is stored with the definition and each run.
Unsupported models/efforts fail explicitly. Repeated executions reuse saved choices while settings remain auto; fixed instance settings are applied again before every execution and saved in its run snapshot.
Foreground and Mem0 retain their existing independent model settings.

Profiles are instance-owned opaque model IDs; partial overrides merge into the three
defaults above. Unavailable choices fail, without paid API fallback or silent replacement.
Codex owns catalog caching (five-minute TTL), concrete model validation and provider RPC.
Existing saved concrete selections are not silently changed by a new default policy.
Unrelated definition edits preserve selection provenance. Selection metadata supplied
by clients is discarded/recomputed; the host records version, profile, origin of each
field (`agent`, `profile_default`, `instance_fixed`), reason and resolved values.

Runs retain the most recent 16 `model_selections` segments with monotonic segment numbers.
Provider-confirmed model/effort are recorded separately when available, never inferred
from tool latency. `get_task_execution_trace` returns these receipts and bounded upgrade
history alongside tool metadata. Total execution time is not claimed as model reasoning
time: queueing, tools, user waits and other orchestration also contribute.

## Controlled model upgrades

`request_task_model_upgrade` / CLI `tasks upgrade_model` applies to an active non-Goal
Codex execution. It changes only that run, not future recurring runs or the definition.
Background callers can target only their own run. Fixed model/effort settings prohibit
automatic upgrades. `max_upgrades` defaults to one, permits 0..3; accepted requests consume
the budget even if a handoff is subsequently blocked. Custom choices outside the configured
profiles and native long-term Goals require explicit task management instead. Goal's
autonomous continuation lifecycle is not silently interrupted or raced by a model handoff.
The Codex adapter also rejects a configured higher profile that would reduce or retain
the reasoning effort of the same model.

Request a higher profile with a stable UUID, concrete `reason` and `evidence`, and a cause
of `reasoning_limit`, `conflicting_evidence` or `unfamiliar_workflow`. Offline devices,
timeouts, missing capabilities and uncertain actions are not upgrade causes. Checkpoint:

```json
{"completed":["Verified completed work and stable operation IDs"],
 "next_step":"Verify current state, then perform only the remaining action",
 "active_operations":[],"uncertain_operations":[]}
```

Reconcile uncertain actions and wait for active external operations before requesting;
never put passwords/tokens in the checkpoint. The agent finishes its current turn after
the request. Request acceptance is not an immediate model switch or task completion.
The host persists `requested → ready → dispatching → applied` receipts, waits for a clean
completed turn, verifies an idle independent provider thread, its matching latest completed
turn and no live background terminals, then resumes that SAME thread using history and
the checkpoint. The run retains workspace/resources, completion round and operation IDs;
no new task definition, foreground turn, speech or intermediate completion notice is created.
Old completion events cannot end the new segment.

User cancellation/pause wins before dispatch. Failed/interrupted turns and failed preflight
block handoff; no terminal is killed merely to make a model switch possible. A lost dispatch
acknowledgment or restart during unconfirmed dispatch becomes `unknown`, never automatically
replayed. A durable `ready` checkpoint can re-enter read-only preflight after restart; if
proof is unavailable it pauses for explicit reconciliation. The framework verifies provider
lifecycle, while external side-effect certainty still requires honest operation receipts
and checkpoint evidence. A stronger model must not repeat an acknowledged device action.

## Task definitions and runs

Definitions hold authorized objectives, trigger, workspace, model, notification policy
and optional resource lock names. Runs snapshot the definition revision and have stable
IDs, progress, questions, provider thread/turn IDs, results and artifacts. A reminder
uses `execution.kind=reminder` and does not invoke a model. A deterministic sensor check
uses `execution.kind=device_query` with a configured `device_id` and the existing typed
read capability. Other tasks use `kind=codex`.

Triggers:
- `{"kind":"immediate"}`
- `{"kind":"date","at":"2030-01-01T09:00:00+08:00"}`
- `{"kind":"interval","seconds":3600}`
- `{"kind":"cron","expression":"0 9 * * mon-fri","timezone":"Asia/Shanghai"}`
- `{"kind":"event","event":{"source":"device","id":"living_temperature","metric":"temperature","operator":"gt","value":28,"hysteresis":1,"duration_seconds":30,"cooldown_seconds":300}}`
- `{"kind":"event","event":{"source":"task","id":"TASK_ID","value":"completed"}}`

Event checks sample the existing device directory once per second, without polling
hardware or running inference. Missing/stale readings cannot meet conditions. Durable
task completion receipts avoid relying on the lossy generic event bus. Instantaneous
device transitions while the service is stopped cannot be recovered. New event sources
belong in adapters; this version does not expose unauthenticated webhooks.

`long_term=true` uses a persistent Codex Goal with a concrete completion condition in
the prompt. Intermediate turn completion is not goal completion. Arbitrary processes
cannot be transparently checkpointed: save progress/artifacts and external job identity,
and inspect before continuing. Task state is not proof of external-world success.

Automatic subtasks must stay inside the parent's workspace and declared resources;
children cannot create recurring triggers, change other tasks or enable voice delivery.
Depth is limited to three and each definition has at most sixteen direct children.
These are trusted local agents with the configured Codex sandbox, not security sandboxes.
Semantic authorization remains part of the instructions and tool approval policy.

## Management

Independent `tasks` MCP exposes create/list/get/update/run, schedule pause/resume,
execution pause/cancel/continue/supplement, terminate/archive and artifact listing.
`device` no longer owns task management. Web task cards and the Task panel use the same
capabilities; CLI examples:

```sh
personal-agent --config config.toml tasks list
personal-agent --config config.toml tasks models
personal-agent --config config.toml tasks get '{"task_id":"ID"}'
personal-agent --config config.toml tasks pause '{"task_id":"ID"}'
personal-agent --config config.toml tasks supplement '{"run_id":"ID","text":"补充要求"}'
personal-agent --config config.toml tasks workfiles
personal-agent --config config.toml tasks workfiles '{"apply":true}'
```

Creation/manual runs require a stable UUID request_id; reuse it on uncertain transport
failure. A different payload with the same ID is rejected. Updates require the current
definition revision. Schedule pause leaves active work running; cancel_run affects only
one execution; terminate/archive stops the definition and children. Explicit continuation
is required for stopped or unknown work. Stop playback does not cancel background work.

## Persistence and delivery

Host SQLite owns definitions, next trigger times, occurrence IDs and execution snapshots.
APScheduler calendars are a rebuildable in-memory projection. Due occurrences are
persisted before dispatch. Recurrence coalesces within a configured lateness window;
missed or overlapping occurrences are recorded instead of replaying a backlog.
Unknown side effects are never automatically retried. Startup reads provider history
before reporting completed/failed/unknown work. A running external process is never
silently recreated. Project/resource lock names serialize conflicting background jobs;
foreground work is delegated rather than issuing duplicate operations.

The shared timeline receives one stable card per definition/run. Results update the card
rather than producing a new message on every progress update. Explicit voice delivery
obeys quiet hours, uses a bounded output-only connection in standby and does not send
microphone frames. Ordinary results remain silent. Voice delivery is best effort; no
exactly-once physical playback claim is made. Files are served only by authenticated
artifact routes, not a static workspace server.

## Working files

All contexts receive a managed `state/work/conversations/SESSION` or
`state/work/tasks/TASK/RUN` area with `scratch` and `artifacts`. Background shell temp
environment points to its scratch area. Project changes remain in the requested project.
This directory policy cannot compel arbitrary tools to obey it; it is not a filesystem
sandbox. Registered terminal scratch areas expire after seven days; active, paused or
unknown work is retained. Artifacts never expire under the scratch policy. Cleanup
rejects symlink traversal and does not scan user projects, secrets or model directories.

## Verification

Run `python scripts/check.py` for scheduling, idempotency, cancellation, event freshness,
model choice, concurrent resources, recovery, file boundaries and the existing suites.
`--group tasks --group codex` includes profile defaults, no parent inheritance, fixed
overrides/provenance, UUID idempotency, upgrade boundaries, live terminals, cancellation,
lost acknowledgments, restart recovery and late prior-turn events. Provider fixtures do
not establish real subscription model-switch success; perform explicit scratch-only live
acceptance before claiming deployment verification. No TV/camera interaction is needed.
Run `python scripts/verify_tasks.py --config config.toml` against the installed service
for a silent reminder, real Codex artifact, long-term Goal and cancellation. It writes
only to managed task areas and saves private evidence under `state/acceptance/tasks/`.
Do not count fake tests as provider/hardware acceptance. No camera or microphone capture
is needed to validate this plugin.

Task results now enter the notifications outbox before completion acknowledgement.
`notify_completion=false` keeps routine monitoring runs out of the chat/inbox; agents
explicitly publish new events through notifications MCP. Automatic failure notifications also respect this setting.
The notifications plugin owns quiet-hour deferral and transport recovery.

Execution results retain `started_at`, `finished_at`, and accumulated `duration_seconds`.
Timing starts on dispatch, includes waiting for answers, excludes queue, explicit pause
and unknown-state intervals, and remains fixed during notification delivery/retries.
Continuing a run adds its new execution segments. Old runs without measured starts
have no fabricated duration. Result notifications carry this value in origin metadata.
`create_task(notify_completion=False)` disables automatic end-of-run notifications,
including failure; `update_task(changes={"notify_completion": True})` changes future
run snapshots. `notify_voice` only adds spoken delivery when notifications are enabled.

A scheduled occurrence with state `missed` is displayed as “本次已跳过”: it never
started. New records retain `skip_reason` (`overlap` / `late`), blocking run IDs and
an explanatory progress message. Overlap does not queue a duplicate; recurring tasks
continue with their next scheduled occurrence. Older records without recorded reasons
remain generic. Terminal records never use a pending-execution UI placeholder.

User-facing wording distinguishes plan state from execution state. Pausing a plan
changes future triggers; pausing one execution does not pause that plan. Queued,
starting, waiting for user input, unconfirmed, failed and skipped records have their
own descriptions. An empty completed result is not displayed as pending. Result
notifications describe one execution, not completion of a recurring task. The
notification switch applies to future run snapshots and automatic result notices;
authorized event alerts remain separate. Duration is accumulated execution time,
not queue time or model-only inference latency.

Immediate, non-Goal one-shot work is categorized as `temporary`; scheduled/event/Goal
work is `planned`. One-shot non-Goal tasks (immediate/date) default `auto_archive=true`.
All terminal outcomes (completed/failed/canceled/missed) archive automatically, retaining
results, artifacts and pending notification delivery. Waiting/paused/unknown/running
work remains visible. Automatic archive never invokes cancel/stop; parents wait for
unfinished descendants. Startup reconciles already-ended one-shots, including records
created before this default. `auto_archive=false` is available for explicitly reusable
work; changing trigger/Goal type recomputes defaults unless the request overrides them.
Archived tasks are read-only; follow-up work uses a new task. Web has two tabs: temporary delegations first, then plans. Temporary defaults to
unfinished non-archived work; “包含归档” shows all temporary results. Plans default
to non-archived definitions, including paused and terminated plans; “包含归档”
also shows archived plans. Both checkboxes default off and retain independent states.
Tabs, checkbox and loaded item count share one compact toolbar row.
Plan detail actions are ordered “触发一次”, “暂停计划/恢复计划”, “终止计划”.
The last action uses the existing archive operation: stop future triggers, cancel
unfinished executions/descendants, and retain results as archived. Its first click
arms an inline “确认终止” button; only a second click submits. Confirmation resets
on blur, outside pointer, Escape, panel close, task change or after five seconds.
There is no separate archive menu action in Web.

Task and run browsing use cursor pages (`before`, default `limit=20`, maximum 50).
`list_tasks` accepts `view=temporary|planned|all` and a server-side `query`;
Both temporary and planned accept `include_archived` (boolean, default false).
The planned API also supports `active_only=true` for enabled plans with future
triggers, event listening or unfinished executions. Filters are applied in SQLite and bound into the cursor, including
search and state, so filter changes start a fresh page. Run entries are concise summaries. `get_task` returns full run pages and a
separate bounded `active_runs` snapshot, so an older unfinished execution remains
visible after newer skipped occurrences. Pass each response's `before` unchanged
to continue that same query. Web scrolls to fetch older pages; expanding history
permits loading more executions. Polling never refetches all previously loaded
pages, and inactive/hidden views pause polling. The host accepts up to 50 `known`
IDs and returns matching off-page definitions in `updates` plus current execution
summaries, or filtered-out IDs in `removed`. Web prioritizes older unfinished work
in that bounded set so it can complete or archive without a stale list row.
No records are deleted by paging.

Each model run uses its registered `scratch/` as actual working directory and temp
directory; `artifacts/` retains deliverables. The target `workspace` is still included
explicitly for authorized project operations, with existing configured permissions.
Codex execution conversations are archived after each confirmed terminal execution,
independently of whether the recurring plan remains enabled or a definition is archived.
`job_finalizations` stores a per-run/continuation receipt; its owned worker restores
unfinished cleanup after restart and retries archive failures with backoff. It verifies
provider inactivity, a terminal latest turn and no live commands. Waiting/paused/unknown
runs stay available. An explicit continuation restores an archived thread; background
archiving never launches another model turn or changes delivery/read receipts. Existing
ended project executions are swept into this policy; unrelated/foreground threads are
not enumerated. Artifacts and execution records remain available in Web.


## Durable event-driven AI work

This extends `tasks`; there is no separate automation plugin or workflow language.
Business interpretation remains in the authorized AI objective. The existing `events`
capability supplies facts; task inboxes store only references and execution state.
Enable `events` before creating a durable subscription. Existing calendars, device
threshold triggers and task-completion triggers retain their semantics.

```json
{
  "kind": "event",
  "event": {
    "source": "records",
    "source_id": "living_presence",
    "types": ["presence.changed"],
    "subject": "living_room",
    "merge_seconds": 3,
    "cooldown_seconds": 30,
    "max_age_seconds": 300,
    "capacity": 128
  }
}
```

Use `create_task` with this trigger and a user-authorized objective, such as reviewing
current conditions before deciding whether action is needed. `types` is required
(1–16 exact type names); source_id and subject are optional exact filters. These are
routing selectors, not a device-condition expression language. Discover real source
identities/types through events MCP. This example does not install a presence sensor.
Subscriptions default to silent completion (`notify_completion=false`) unless explicitly
chosen otherwise; meaningful alerts use the existing notifications capability.

### Receipt, batching and lifecycle

- Creating, editing or resuming starts at the recorder's current change cursor; it
  never replays earlier history. Pausing/editing/archiving clears pending references
  with recorded reasons. Already assigned runs retain their immutable snapshot;
  pause does not cancel them, while archive/terminate uses existing cancellation.
- Each page of changes, inbox decisions and resulting run insertion share one host
  SQLite transaction. Stable occurrence IDs and the cursor prevent duplicate automatic
  dispatch after a crash or lost hint. The event database is read through `EventRequest`,
  never directly. The lossy bus accelerates reads; a one-second fallback reads the
  durable log. This is not hardware polling. Recorder failure backs off five seconds
  per subscription and leaves unrelated tasks/voice running.
- Only new records labeled `observation` at revision 1 trigger work. Baselines, gaps,
  manual statements, summaries, corrections and deletions do not. Corrections/deletions
  invalidate waiting references; queued runs reread current revisions before dispatch.
  Changes arriving after execution starts do not undo physical effects; the AI must
  query current facts/state before acting.
- A batch closes after `merge_seconds` from its first receipt (default 3, range 0–60),
  not after endless trailing quiet. `cooldown_seconds` is the minimum dispatch spacing
  (default 30, range 0–86400). One active/paused/waiting/unknown execution blocks another
  for that task. New events accumulate independently rather than steering a running turn.
- Event lifetime uses occurrence time or observation-interval end: `max_age_seconds`
  defaults to 300, range 1–86400; merge must be shorter. More than five seconds in the
  future is rejected as clock uncertainty. Expired pending/queued events do not launch
  work. Explicit continuation remains a user decision and keeps its original context.
- Each inbox holds at most `capacity` pending references (default 128, range 1–256),
  each execution consumes at most 32, and at most 64 subscriptions may be enabled.
  When full, newer arrivals are counted as overflow; accepted pending work is retained.
  Counts persist; the last 100 decisions are retained, with 20 shown by inbox queries.
  No raw sensor payload is duplicated. An overflow or gap means incomplete evidence.
- Event-log cursor regression is reported as unhealthy; it is never silently reset to
  replay old actions. Explicit pause/resume establishes a new tail after investigation.

### AI context and tracing

An event execution snapshots `trigger_context`: subscription, referenced event IDs and
change cursors, observation times, batch count and previous run identity when present.
Dispatch adds `valid_events` and an invalidated count. The Codex adapter supplies this
as data beside the original objective, with task/run IDs. AI can use existing events,
memory, devices and tasks MCPs to fetch facts, examine previous outcomes and decide
what to do. New events do not expand authorization, change model selection, switch
fixed voice routes, or authorize child recurring tasks.

`get_task` includes the bounded inbox and run contexts. `get_task_event_inbox(task_id)`
shows cursor/health, pending references and received/assigned/expired/overflow/
invalidated/lifecycle-discard counters. `get_task_execution_trace(run_id, after, limit)`
links trigger references to run state, provider thread/turn and tool metadata. CLI:

```sh
personal-agent --config config.toml tasks event_inbox '{"task_id":"..."}'
personal-agent --config config.toml tasks trace '{"run_id":"...","limit":50}'
```

Trace stores at most 500 entries per run, paged 50 by default, maximum 100. Repeated
provider call IDs/phases deduplicate; truncation is explicit. It retains server/tool,
phase/status and reported device/operation IDs, never complete arguments, shell commands
or tool output. A completed tool call is provider evidence, not proof that hardware
changed. Native operation IDs can be checked through devices MCP; external tools may
lack this guarantee. Unknown executions are reconciled or explicitly continued, never
silently recreated. Raw provider-specific parsing stays inside `plugins/codex`.

The existing task panel displays the merged trigger count and current pending count;
it keeps its existing paging and controls. Complete trace inspection is MCP/CLI-first.

Tests: `scripts/check.py --group tasks --group events --group codex`. Coverage includes
transaction rollback, duplicate reports, real runtime/SQLite restart, pagination,
coalescing, overflow, expiration, mutation invalidation, paused subscriptions, stale
in-flight reads, unknown executions, and trace redaction/limits. Simulated sensor events
are not evidence that a physical presence sensor has been connected or validated.
