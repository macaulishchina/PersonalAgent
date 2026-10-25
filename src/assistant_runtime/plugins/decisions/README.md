# Fast semantic decisions

Platform: Linux and Windows / Windows Server. Platform certification covers the PA adapter; physical devices and external services require their own acceptance. See the [module index](../../../../docs/reference/modules.md).

`decisions` owns TypeSafe Jev transport, admission, policy and observations. It does
not grant permissions, execute tools or certify effects. Exact controls, arithmetic
and known rules remain local. Main/background agents and framework owners share the
same provider pool and receipt model. Kernel contracts remain standard-library only.

## Use and ownership

- Direct module use: `await decide(runtime, DecisionQuery(kind, reference, state,
  summary=..., input_version=...))`. Inputs are detached and bounded to 16 KiB.
- Owned parallel use: `submit_decision(context, request, consume, current=...)`.
  It cannot deliver stale answers; the consumer still validates operation scope.
- Shadow: observe through `DecisionSample` or configure a scene as `shadow`. The
  caller receives original-workflow fallback, never an actionable choice.
- Direct/parallel: both return validated choices; the caller's execution structure
  determines whether it waits or runs concurrently. Configuration never spawns a
  second executor or gives a general MCP caller direct action authority.
- `report_decision(runtime, result, status, reason, operation_id)` records adoption,
  rejection, suggestion or known outcome. Returning `ok` is only a judgment result.
- MCP `decision_support_status` and `judge_options` retain bounded advisory access.
  `get_voice_decision`/`confirm_voice_decision` dispatch to the voice owner, not this
  plugin. Background identities cannot confirm foreground voice changes.

Trusted scenes live in `questions.py`: voice intent, event relevance, unresolved
model profile, joint model review, and generic selection. Adding a scene requires
versioned question/state validation, bounded owner summary, meaningful fallback,
owner adoption handling and tests. User text/options are data, never instructions.
A missing/disabled plugin uses the original path; cancellation propagates, rather
than starting fallback work. Do not loop on judgment failures or replay actions.

## Configuration

Use the separate `config/decisions.toml` included by the root configuration. Changes
require restart; `decisions reload` reloads credentials only, not policy. Defaults:

```toml
[decisions]
enabled = true
external_data_consent = false
mode = "active"
model = "jev-1.13.0"
default_threshold = 0.6
active_timeout_seconds = 3
concurrency = 4
queue_size = 32
retention = 2000 # Recent indexes only; archives are never pruned by this limit.
timezone = "Asia/Shanghai"
timeout_seconds = 8
max_age_seconds = 30
warmup_enabled = true
keepalive_seconds = 120
dns_cache_seconds = 300

[decisions.thresholds]
model_review = 0.2

# Optional overrides, not required for each scene:
# [decisions.scenes.voice_intent]
# enabled = true
# mode = "parallel" # shadow / parallel / direct
# threshold = 0.6
# timeout_seconds = 3
```

Scene threshold overrides precede `thresholds[kind]`, then the global threshold.
Default modes: voice/selection parallel, task-model/event direct. Global shadow
forces all scenes to observation-only. Policies are hashed, with effective values
recorded per invocation. The threshold uses the selected candidate probability,
not provider `confidence` and not an empirically calibrated accuracy guarantee.
`uncertain` always falls back. Model-profile's legacy two-question result still
requires each adopted field to meet its own threshold; joint model review selects
one legal pair. User choices and fixed settings remain task-owned protections.

`TYPESAFE_API_KEY` belongs in the private root `.env` or environment. Explicit
instance consent covers curated text/context only; credentials and raw audio/video
are rejected. The structural guard is not a universal secret detector: owners must
provide safe, bounded summaries. No daily/lifetime request-count limit is imposed.

## Scheduling and failure

Three ordinary workers plus one interactive-only worker are the default. Interactive
voice control has queue priority and a reserved queue slot; running calls are not
preempted. Queue and concurrency are bounded. Equal in-flight inputs share one
provider request, but every caller gets its own decision ID and adoption record.
A canceled waiter cannot cancel other waiters or deliver a late action. Pending
work is volatile and not replayed after process restart.

The active deadline includes queuing and receipt IO. A bounded archive queue keeps callers from awaiting an archive write. Receipt and
archive IO share one serial storage worker; disk stalls can cause bounded judgment
fallback, but cannot block the audio loop. Provider response history
is committed before delivering a usable result; unavailable receipt storage therefore
falls back, preserving the business workflow. Shutdown drains accepted archive work.
Log drops and IO failures remain visible as health/counter information, not silent
claims of complete coverage.

The HTTP adapter owns one reusable session, with `concurrency + 1` connections for
judgments and optional startup metadata warmup. Warmup starts after Runtime.ready,
is bounded, performs no inference and never holds a judgment slot. There is no paid
keepwarm, automatic HTTP retry, or model switching. Auth failures suspend until
credential reload/restart; other faults back off. Quota failure (HTTP 402), rate
limits, timeouts and network faults are explicit. One notification opens an outage;
repeated failures are suppressed, recovery is reported. Missing notifications do
not block original business workflows.

## Voice and event adoption

Known local speech controls bypass inference. Other bounded accepted native text
can be classified without a keyword-only admission gate. Sleep, stop and chat may
apply locally after epoch/input/focus checks; cancel/takeover requires a uniquely
associated operation and stop readback. Correction/supplement is only a suggestion
until the foreground executor explicitly confirms it via the voice owner. Confirmation
is claimed before side effects, so a lost response cannot repeat the handoff.

Native automatic handoff remains a notification, not an approval hook. Context-only
suggestion injection is not a second user submission. A safe foreground checkpoint
must exclude unknown/in-flight tools and terminals; background correction pauses the
related original run before continuation; a later scheduled run cannot inherit that binding. No confirmation can archive an entire schedule.
Multiple targets, changed focus or unverified stops require clarification/reconciliation.
The optional local ASR/TTS path stays disabled until its own acoustic acceptance.

Event recording never waits for judgment. Only a batch whose admitted, current events
are all classified irrelevant may be skipped. Events and run state are re-read after
judgment; invalidation is explicitly recorded. Uncertainty/failure preserves dispatch.

## Archives and queries

`state/decisions/archive/YYYY-MM-DD.jsonl` is a private, append-only daily archive in
the configured timezone. No automatic time/total-size deletion. Records include:

- invocation/receipt IDs, scene, caller/operation link, input hash/version;
- bounded owner summary, candidate IDs, question/policy version and effective settings;
- selected candidate and probability distribution, usage and provider/queue/total latency;
- result/fallback, owner suggestion/adoption/rejection and any reported operation outcome.

Complete judgment input, original media and credentials are excluded. Provider returns
and business adoption are different stages. `shadow.sqlite3` remains the bounded recent
receipt/audit index; pruning it never removes date archives. Historical data is preserved;
missing historical adoption remains unknown. Synthetic-prefixed/manual fixtures are
separate cohorts, not evidence of normal household accuracy. Status statistics describe
retained indexes, not lifetime accuracy or task success.

```sh
pa --config config/config.toml decisions status
pa --config config/config.toml decisions list '{"limit":20}'
pa --config config/config.toml decisions archive '{"day":"2026-10-21","limit":50}'
pa --config config/config.toml decisions archive '{"day":"2026-10-21","kind":"voice_intent","status":"adopted"}'
```

Archive filters also include `decision_id` and `operation_id`; `next_offset` is passed
as `offset`. Each read scans at most 4 MiB and returns at most 100 records; an empty
page may still have a continuation cursor. Configuration is validated by Config.load;
status exposes effective scene policies, index statistics, queue pressure and faults.

## Verification and rollout

Run `scripts/check.py --group decisions --group tasks --group speech --group execution`
during development and the full gate after final edits. Tests cover policy inheritance,
concurrency/reserved capacity, deduplication with separate caller outcomes, cancellation,
late results, daily archives, write failure, shutdown, voice confirmation and unknown
side effects. Pure protocol tests do not establish real microphone/room or device success.

Explicit synthetic acceptance scripts remain opt-in. Measure complete user-response
latency and actual adoption separately from HTTP duration. Do not enable a new native
voice behavior solely because a mocked checkpoint passed. If the provider cannot
expose a reliable boundary, retain advisory/unconfirmed behavior and document that
limitation rather than reconstructing the native voice stack.


Historical results are preserved in the [module acceptance archive](../../../../docs/archive/module-acceptance-2026-10.md).
