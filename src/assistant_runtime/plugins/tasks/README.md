# Tasks plugin

Trusted local plugin for independent background work on the existing ChatGPT subscription.
Enable `tasks` in the instance `plugins` list and install the `tasks` extra in the existing `.venv`.
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

[work_files]
retention_days = 7
# root = "/absolute/private/instance/work"
```

`auto` is an application policy, never sent as a model name to Codex. The initiating
model selects concrete values from `list_task_models`, with an `execution.reason`.
Missing selections inherit the initiating context; fixed instance settings override
requests. The resolved model/effort is stored with the definition and each run.
Unsupported models/efforts fail explicitly. Repeated executions reuse saved choices while settings remain auto; fixed instance settings are applied again before every execution and saved in its run snapshot.
Foreground and Mem0 retain their existing independent model settings.

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
Run `python scripts/verify_tasks.py --config config.toml` against the installed service
for a silent reminder, real Codex artifact, long-term Goal and cancellation. It writes
only to managed task areas and saves private evidence under `state/acceptance/tasks/`.
Do not count fake tests as provider/hardware acceptance. No camera or microphone capture
is needed to validate this plugin.
