# Tasks plugin

Trusted local plugin for independent background work through the configured Codex executor
(subscription or an explicitly accepted API model).
Enable `notifications` and `tasks` in the instance `plugins` list and install the `tasks` extra in the existing `.venv`.
Requires the conversation and configured brain capabilities. Does not activate the microphone.

Requested media belongs to the producing run. Provider callbacks accumulate up to
four deduplicated registered references; the conversation media owner validates and
pins them even when completion notifications are disabled. `get_task` exposes each
run's validated references and top-level `attachments` from the newest run on that
page, identified by `attachment_run_id`. An empty newer run never inherits an older
picture. Main conversation tool replies and task details render those references
through the same authenticated media viewer, outside collapsed tool text. Image
preview uses a native modal above the mobile task dialog. Raw pixels do not enter
task JSON/SSE or notification prose. Prose saying "sent" is not delivery evidence.
Recorded task replies can restore missing conversation attachments from their
existing registered references without rerunning tasks or capturing another image.

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
[tasks.model_policy.profiles.ultra]
model = "gpt-6-astra"
reasoning_effort = "high"

[work_files]
retention_days = 7
# root = "/absolute/private/instance/work"
```

`auto` is an application policy, never sent as a model name to Codex. The initiating
model reads `list_task_models` for current models, supported efforts, profile availability
and shared guidance. Choose the lightest suitable profile using uncertainty, existing
procedures and acceptance checks; step count or wall time alone does not imply difficult
reasoning. Profile selects the execution model tier; reasoning effort can be chosen
independently from that model's current account-supported efforts. Assistants may specify
either or both with an evidence-based reason, e.g. `execution={"profile":"deep",
"reasoning_effort":"high","reason":"conflicting evidence needs difficult reasoning"}`.
Low suits familiar steps; medium suits exploration; high and above need reasoning
difficulty rather than a long task. Higher efforts remain a deliberate, sparing choice.

Optional `decisions` support makes at most one joint `model_profile` query for missing
profile/effort fields. The HTTP request batches two small native Choice questions,
one for the tier and one for effort, instead of asking the provider to discriminate
the Cartesian product. Confidence is checked independently for each unresolved field;
an uncertain field falls back to its configured default while a trusted field can be
adopted. The resulting combination must pass the same supported-option and executor
checks; incompatible partial answers revert to the original policy. Its bounded `options` come from configured profiles and the current
account catalog, with fixed instance values and explicit agent fields already applied.
Only unresolved fields may change. Identical concrete model/effort aliases are collapsed
to avoid splitting probabilities; matching configured defaults name the canonical tier.
One supported combination needs no semantic query. With both fields determined, Jev is
bypassed. Disabled/unconsented/shadow/unavailable/uncertain/timeout advice uses original
profile defaults and preserves explicit fields; unsupported explicit choices still fail.
No extra inference service or provider replacement is introduced. A model_catalog failure
does not hide a subsequent executor validation error. Creating a task does not alter a
running task's model or effort; active changes retain the controlled handoff mechanism.
`ultra` is above `deep`, reserved for exceptionally complex reasoning or original
scientific research with concrete justification that `deep` is insufficient. Use it
sparingly; routine literature search, summaries, task length and a research-related
title do not justify it. The default remains `fast`; all four model/effort mappings
can be overridden in instance configuration. `ultra` is a task profile name, separate
from any provider reasoning-effort value with the same name.
Use `execution={"profile":"fast","reason":"known steps; read back the resulting state"}`.
Concrete model/effort choices remain supported with a nonempty reason (max 400 chars).
Missing/auto values use the configured profile, never the foreground or parent model.
Fixed instance settings override requests. The resolved model/effort is stored with the definition and each run.
Unsupported models/efforts fail explicitly. Repeated executions reuse saved choices while settings remain auto; fixed instance settings are applied again before every execution and saved in its run snapshot.
Foreground retains its subscription binding. Mem0 has its independent subscription settings
or the explicit `model_api.memory` binding; task profiles do not change either.

Profiles are instance-owned opaque model IDs; partial overrides merge into the four
defaults above. Unavailable choices fail unless an explicitly configured fallback applies at the safe first-turn boundary below; there is no implicit replacement.
Codex owns catalog caching (five-minute TTL), concrete model validation and provider RPC.
Existing saved concrete selections are not silently changed by a new default policy.
Unrelated definition edits preserve selection provenance. Selection metadata supplied
by clients is discarded/recomputed; the host records version, profile, origin of each
field (`agent`, `profile_default`, `instance_fixed`, `jev`, `catalog_only_option`), reason
and resolved values. Joint advice also retains the original explicit fields and the
validated recommended combination. A different supported effort does not erase a known
profile's identity or silently label it a custom model.

Runs retain the most recent 16 `model_selections` segments with monotonic segment numbers.
Provider-confirmed model/effort are recorded separately when available, never inferred
from tool latency. `get_task_execution_trace` returns these receipts and bounded upgrade
history alongside tool metadata. Total execution time is not claimed as model reasoning
time: queueing, tools, user waits and other orchestration also contribute.

## Explicit failure fallback

Configure an ordered list per concrete primary model, separately from complexity
upgrades and from no-tool inference fallback lists:

```toml
[tasks.model_policy.fallbacks]
gpt-6-luna = [{ model = "deepseek-assist", reasoning_effort = "none" }]
# Example with verified API entries:
go-claude = [
  { model = "go-gpt", reasoning_effort = "none" },
  { model = "gpt-6.1-sol", reasoning_effort = "medium" },
]
```

The primary is tried first, then at most three listed backups once each. The
original list remains fixed: no recursive backup expansion. Each backup has an
explicit model and effort validated by the executor. Same upstream model through
a different provider must have a distinct API entry alias. Existing saved task
choices remain primary; the instance mapping also applies to those choices.

Only a structured provider rejection on a fresh first execution is eligible.
The Codex adapter must read back one failed turn, complete items, no tool/assistant
activity, no active terminals and no Goal. Then tasks records a new model selection,
revokes the old binding and starts a fresh thread for the same run and grants.
Already-executed work, resumed turns, long-term Goals, user cancellation, policy
errors, stream loss and unknown outcomes are not automatically replayed. Existing
safe-checkpoint upgrades remain separate. Fallback can change model quality; it
is availability policy, not a promise of equivalent reasoning strength.

`get_task_execution_trace` exposes `model_fallback` with ordered errors, selected
model and phase. Logs record failure and switch without credentials. Exhaustion
fails the run. Restart during a pending handoff becomes unknown and requires
reconciliation rather than another dispatch. Late old-thread results cannot replace
the new run. Primary failure alone is never evidence that no operation happened.

## Controlled model upgrades

Provider turn completion is not native Goal completion. Even if an ordinary run
creates a Goal during execution, intermediate turns keep that SAME run and its
existing resource grants active; no definition, scope or grant is expanded. Only
Goal completion plus an ended turn permits completion/delivery/archival. Goal
blocked/paused/usage/budget limits await user input. Ordinary one-shots should not
create native Goals; configured long-term work uses them explicitly. Pause/cancel
also pauses any actual native Goal before interrupting the turn. Late continuation
cannot revive terminal/paused runs and is stopped by the provider adapter. Recovery
reads current Goal status and pauses unfinished Goals without replaying work.
Native Goals also prevent automatic model handoff, including Goals created after
a handoff was requested.

`request_task_model_upgrade` / CLI `tasks upgrade_model` applies to an active non-Goal
Codex execution. It changes only that run, not future recurring runs or the definition.
Background callers can target only their own run. Fixed model/effort settings prohibit
automatic upgrades. `max_upgrades` defaults to one, permits 0..3; accepted requests consume
the budget even if a handoff is subsequently blocked. Custom choices outside the configured
profiles and native long-term Goals require explicit task management instead. Goal's
autonomous continuation lifecycle is not silently interrupted or raced by a model handoff.
The Codex adapter also rejects a configured higher profile that would reduce or retain
the reasoning effort of the same model.
The order is `fast → balanced → deep → ultra`; the default one-upgrade budget remains
unchanged, and a justified request may move directly to a higher profile. `ultra`
uses the same reason/evidence/checkpoint and authorization checks as other upgrades.

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

Resource identifiers are internal delegation details, not a second user consent step.
Foreground must resolve the authorized target through its owning capability before
creating work; for media, use `media_capabilities.target_bindings` and its exact account,
provider and `task_resource`. Never infer a provider from a generic target alias.
An existing user request authorizes its necessary scope, not unrelated destructive work.

`repair_task_run_resources(run_id, revision, resources, reason)` lets foreground correct
one execution's complete resource list using that original request. Pause and confirm
running work stopped first. Only queued, paused or waiting runs without an active turn
can change; launching, child, unknown and ended executions fail closed. Run revision is
checked, and before/after bindings plus the reason are persisted in `resource_repairs`
and exposed by execution trace (bounded to 32 corrections). The tool does not resume,
replay actions, change model selection, update future definitions or affect another run.
Future definitions use `update_task`; archived work needs a correctly scoped follow-up
that reconciles existing operation IDs. Background callers cannot repair their own grants.

## Controlled self scheduling

Foreground creation/update may record an explicit user grant on a reusable timed plan:

```json
{"self_schedule":{"allowed_triggers":[{"kind":"interval","seconds":86400}],
 "condition":"After all authorized historical items are covered and pending errors resolved, check new items daily."}}
```

No grant is inferred from prompt text. The default is no self scheduling. The plan must
have user authorization, a timed trigger and `auto_archive=false`. The grant lists
1..16 exact interval/date/cron choices and a bounded authorization condition; immediate
and event triggers are excluded. Children cannot grant or inherit this capability.
Revocation uses foreground `update_task(changes={"self_schedule":null})` with a fresh revision.
Grant/revocation and other edits that do not change the trigger preserve the existing
next occurrence; they do not create an extra execution.

An active provider turn uses `update_own_task_schedule(request_id, revision, trigger,
reason, evidence)`. The host binds identity to that execution, derives its definition
and accepts no task/run target. It checks both the execution's captured grant and the
current plan, the latest revision and a storage-level compare-and-swap. No prompt,
workspace, resources, model, permissions, notifications or current-run snapshot changes.
The agent determines whether the granted condition is met and supplies concrete evidence;
the host enforces the declared choices, not semantic proof that the objective succeeded.
Receipts mark evidence as executor-reported. Tool output, events and memory cannot grant
additional permission. Background `update_task` and management of other tasks stay denied.

Only enabled plans and running, identified provider turns can submit new changes. Any
foreground edit, pause/resume, termination or grant change invalidates an older run's
ability to reschedule, even if it reads the new revision. The next execution captures
the new authorization. An execution may make successive authorized changes using the
version returned by its last change. The currently running work is never restarted.

Each change commits the new schedule and an idempotent receipt in one host SQLite
transaction. UUID retries with identical arguments return the original receipt, including
after execution completion; changed arguments conflict. Retrying an older receipt cannot
restore its old schedule. In-memory scheduling is rebuilt from the current committed
definition. A matching schedule is a no-op error rather than moving its next occurrence;
new interval/cron occurrences are strictly future, and past dates are rejected.
The latest 20 receipts appear in execution trace with a truncation flag. All receipts
remain durable for idempotency and audit; they include before/after schedule, revisions,
identity, time, reason, evidence and the authorization condition.

Tests: `scripts/check.py --test test_task_self_schedule`. Explicit installed-service
acceptance: `scripts/verify_task_self_schedule.py --config config/config.toml`. It creates one
silent synthetic timed plan, verifies its real MCP change and unchanged run snapshot,
then archives it. It uses no physical devices and does not replay failed/unknown work.

## Management

Independent `tasks` MCP exposes create/list/get/update/run, schedule pause/resume,
execution pause/cancel/continue/supplement, terminate/archive and artifact listing.
`device` no longer owns task management. Web task cards and the Task panel use the same
capabilities; CLI examples:

```sh
personal-agent --config config/config.toml tasks list
personal-agent --config config/config.toml tasks models
personal-agent --config config/config.toml tasks get '{"task_id":"ID"}'
personal-agent --config config/config.toml tasks pause '{"task_id":"ID"}'
personal-agent --config config/config.toml tasks supplement '{"run_id":"ID","text":"补充要求"}'
personal-agent --config config/config.toml tasks workfiles
personal-agent --config config/config.toml tasks workfiles '{"apply":true}'
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
Run `python scripts/verify_tasks.py --config config/config.toml` against the installed service
for a silent reminder, real Codex artifact, long-term Goal and cancellation. It writes
only to managed task areas and saves private evidence under `state/acceptance/tasks/`.
Do not count fake tests as provider/hardware acceptance. No camera or microphone capture
is needed to validate this plugin.

Task definitions, startup and ordinary progress are available in the task panel and
tool results, without separate chat cards. The notifications owner is the single task
feedback delivery path: typed results per execution/completion round and deduplicated
attention for waiting-user/unknown states. Attention is independent of the routine
completion preference; changing progress while waiting does not repeat a question.
Questions use their structured identity, never a match against task titles. The main
reply can acknowledge delegation without a second automatic “created/enabled” message.

### Result delivery ownership

`create_task(feedback_owner="background")` (default) hands both work and result notification
responsibility to the background. Use `feedback_owner="conversation"` only for immediate,
non-Goal work whose result the current main turn explicitly takes responsibility for.
The originating session/turn/input revision is captured by the server before asynchronous
model selection and persisted atomically with task creation. Clients cannot supply an
identity. This creation choice covers only the first execution/round; later occurrences,
continuations and other tasks are independent. `notify_completion` remains the on/off
policy and `notify_voice` the background voice preference, not ownership switches.

`get_task` includes `run.feedback`: owner, round, version, state and delivery receipt.
It is read-only: querying or inspecting a result never takes over responsibility.
Main may explicitly call `transfer_task_feedback(run_id, round, version, owner)` with
`owner="conversation"` to take responsibility, or `"background"` to hand it off. The
server checks current conversation availability, caller identity and the latest round/version.
Background callers cannot impersonate the main turn. Stale versions must be reread;
committed or delivered results return `transferred=false`, without recalling/reissuing them.

At completion, tasks freezes the delivery payload/owner and commits it before transport.
Conversation writes one stable result card, with task link and registered attachments,
immediately; voice uses the existing output path. No model-generated answer is needed
before delivery. Main can add interpretation, but should not repeat the result verbatim.
Turn completion/interruption, new input, voice exit and restart recovery release outstanding
ownership when delivery checks that turn. There is no elapsed-time ownership rule.

An RPC error/timeout does **not** imply failure to deliver: the same result identity is
retried until its stored receipt or explicit release is known. Inaccessible conversation
capability keeps a visible task-plugin health error, not an unverified opposite-channel
send. Once released, ordinary durable notifications take over. Uncertain speech is not
replayed; a silent inbox notice shares the existing card. Output receipts do not claim the
user heard/read anything. Delivery is bounded/concurrent and does not stall task dispatch.

Implementation: tasks owns `job_result_claims` / `job_result_feedback`; conversation owns
`chat_result_receipts` / turn lifecycle. `ConversationFeedbackRequest` is internal, absent
from IPC/MCP. The Codex adapter has no new protocol/prose acknowledgement heuristics.
`tests/test_task_feedback.py` covers handoffs, conflicts, query independence, terminal races,
turn exit, restart, lost replies, speech uncertainty and attachment projection without
hardware/model calls. Real voice timing is left to everyday evaluation.

Background-owned results enter the notifications outbox before completion acknowledgement;
conversation-owned results require their durable conversation receipt.
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
personal-agent --config config/config.toml tasks event_inbox '{"task_id":"..."}'
personal-agent --config config/config.toml tasks trace '{"run_id":"...","limit":50}'
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


## Completing an authorized plan from its own execution

Foreground creation/update may grant `self_completion={"condition":"concrete user-authorized stopping criteria"}`
to a reusable user-authorized timed plan. Children cannot grant themselves this authority.
`complete_own_task(request_id, revision, reason, evidence)` derives the target from its
bound active provider run; it accepts no arbitrary task identity. Both the captured and
current grant must exist, and current revision must match the run or its last controlled
self-change. User edits/revocation/pause invalidate older authority. Archive and UUID
receipt commit together; retries return the receipt without another transition.

Completion stops future scheduling, preserving the current execution for its final
report. It does not call the general archive action that cancels active work. The existing
self-change audit records `action=complete_self` and executor-reported evidence; the task
framework does not independently prove a domain goal. For media watches, verify the
known final episode range, requested storage level and absence of unresolved operations;
no search hits is not completion evidence. Unknown totals require continued tracking or
user clarification. Tests live in `test_task_self_schedule`.

## Evidence-based model review and active selection

`tasks.model_policy.review_mode` supports `off`, `shadow` (default) and `active`.
Off/shadow keep existing missing-field selection behavior; shadow additionally observes
initial dispatch asynchronously. Active uses one bounded pre-execution decision on task
creation or explicit execution edits, replacing missing-field support rather than making
two selection calls. It never reselects running work or changes saved recurring plans
merely because the service restarts. Unrelated edits preserve execution verbatim.

```toml
[tasks.model_policy]
review_mode = "active"

[decisions.thresholds]
model_review = 0.2
```

The candidate probability must meet this configurable threshold (0..1; default 0.90).
It is not a measured execution-success probability. Other decision thresholds retain
their existing minimum 0.5. The decisions global enabled/consent/mode switches still
apply. `active_timeout_seconds` (default 3 seconds) also bounds catalog/review/adoption
work after validating the original selection. Failure, timeout, missing support or an
invalid recommendation keeps that valid original selection/default without a second
Jev query, model retry or tool replay.

Explicit recommendations should include `execution.model_source="assistant"` for a
model/profile and `execution.effort_source="assistant"` for an explicit effort. Use
`"user"` only for fields explicitly chosen by the user. User/fixed fields are filtered
out of Jev's change space. Unlabelled explicit fields are conservatively protected,
including older plans. A user-chosen model does not lock an unspecified effort.
At the shared selection boundary, redundant `assistant` source labels on missing/auto
values are removed without changing the caller's input. `user` labels still require
a concrete model/profile or effort; defaults are not explicit user choices. Invalid
source values are rejected. Free-form `assessment.user_preference` only informs Jev;
it never adds selection locks, even when nonempty. Encode actual user choices in the
corresponding structured fields and sources, not solely in preference prose.
Declarations only restrict this choice; they are not verified user authorization and
cannot grant tools, resources or rights. User constraints persist through execution
updates unless the caller supplies an explicit user-directed replacement. No semantic
verification of the caller's attribution is claimed.

Active receipts record original/selected pairs, field origins, protected values,
threshold, decision receipt, adoption and whether the pair actually changed. Dispatch
copies this receipt into `run.data.model_review`, without an additional shadow call.
Existing fallback chains and checkpoint-based upgrades remain separate owners.

Agents should supply the following optional, bounded `execution.assessment`:

```json
{
  "acceptance": "Read back the destination and verify the intended objects",
  "procedure": "Existing transfer tool; search candidates still need identity checks",
  "uncertainties": ["Two candidates disagree about the object's identity"],
  "user_preference": "Exact explicit model/speed preference, only if the user stated one",
  "prior_run_id": "related execution ID when retrying"
}
```

Omit absent fields rather than inventing evidence. `uncertainties` allows up to four
300-character statements; other text fields are at most 500 characters. The framework
reads the referenced run itself and captures its state, selected model/effort and a
bounded, explicitly labelled executor summary. Background references must belong to
the parent's task or its children and the same workspace. This is reported evidence,
not independent verification of external effects. Source modules continue owning tools
and their operation receipts; there is no media-specific routing logic.

Caller-supplied facts remain assistant statements. Free-form `user_preference` is
unverified; field provenance provides conservative selection locks, not authority.
Supplying fake `source` or `user_verified` fields inside assessment is rejected.

The independent `model_review` question sees task facts and full legal model/effort
pairs, but not the baseline recommendation/reason as separate advice. It does not
create a universal scale between different providers' native efforts. Candidates come
from configured tiers plus the explicitly selected custom model, filtered by the live
catalog and instance constraints. The task prompt may itself contain preferences, so
this is not a claim of a statistically blinded experiment.

In shadow mode, one owned worker and a 16-entry queue bound review work; launch never waits for it.
The worker has a four-second total deadline and keeps the existing three-second Jev
active deadline by default. Saturation, unavailable support, fixed choices and errors
produce explicit skip/fallback records. Restart marks queued/running reviews interrupted
without replay. Identical facts/candidates use a stable digest to reuse existing decision
receipts; cache eviction can cause a later review, not an execution replay. No daily
call cap is added. Credentials/raw media remain prohibited and inputs retain the
16-KiB decisions limit; oversized inputs fall back, rather than silently losing facts.

Inspect `run.data.model_review` using task get or `get_task_execution_trace`. It includes
initial selection/source, candidate recommendation, receipt ID, disagreement, timing and
adoption state (`applied=false` in shadow). Original facts live in `spec.execution.assessment` and
`spec.execution.selection.prior_execution`. Compare these alongside model-selection
segments, tool trace, duration and verified task outcomes. Disagreement is not evidence
that Jev was correct; provider-reported completion is not a quality label. Active correction is explicitly opt-in; deployment of a low threshold is not evidence
of improved accuracy. Existing evidence/checkpoint-based upgrades remain unchanged.


Decision adoption now records its own observation after model policy validation.
The decisions service owns the global threshold (0.6); model-review override is 0.2
in the example/instance configuration. Raw provider confidence is not measured task
success. Selection receipts preserve the per-call decision ID for later execution
comparison; event-inbox stale decisions are rejected explicitly before dispatch.

### Web execution timeline

Each run has a collapsed **执行过程** disclosure. It reads the existing trace through
`GET /api/tasks/{run_id}/trace?after=0&limit=100` (same Web authentication). Only activity
metadata, ownership IDs, pagination and coverage flags cross this route; prompts, model
selection evidence, provider thread IDs and tool bodies are not included. Call start/end
records are merged by `call_id`, retaining parallel calls and turn-specific identities.
The displayed duration is the interval between received records, not external-device
verification. A completed run never converts a missing tool-end record into success.

One selected trace loads lazily, drains ordered bounded pages, then checks for new records
every three seconds while unfinished and visible. Collapsing/closing or switching tasks
aborts in-flight requests; history stays inspectable without background polling. Empty
and truncated histories are explicit. No older progress text or hidden reasoning is
reconstructed. Existing tasks trace storage and its 500-event limit are unchanged.
