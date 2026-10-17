# Optional semantic support

`decisions` is the single owner of TypeSafe Jev. It answers small, closed semantic
questions; it never executes actions, grants permission, verifies effects or replaces
Codex. Main and background agents share its independent MCP, limits and receipts.
Exact rules, arithmetic, known controls and ordinary straightforward choices stay local.

## Configuration and credentials

Install the `decisions` extra in the existing `.venv`. The default plugin list includes
`decisions`; instances with an explicit list must add it. Defaults:

```toml
[decisions]
enabled = true
external_data_consent = false
mode = "active"
model = "jev-1.13.0"
active_timeout_seconds = 1.5
timeout_seconds = 8
queue_size = 32
daily_limit = 100
retention = 2000
max_age_seconds = 30

[decisions.thresholds]
voice_intent = 0.98
event_relevance = 0.98
model_profile = 0.90
selection = 0.90
```

Set `external_data_consent=true` only after the instance owner authorizes sending
judgment-relevant voice text, event content, task objectives, tool-result summaries
and candidate options to `api.typesafe.ai`. Credentials and raw audio/video are never
valid inputs. The global `enabled` switch defaults on; consent defaults off for new
instances. Set `enabled=false` and restart to use original logic everywhere. Removing
the plugin also leaves those paths usable. `mode="shadow"` restores observation-only
operation; active callers receive fallback. `daily_limit=0` prohibits paid attempts.

Put `TYPESAFE_API_KEY=...` in the root `.env` (mode 600), or the service environment.
Environment takes precedence. Dotenv interpolation is disabled; variables are never
exported to child processes. The adapter alone reads the key. Its HTTPS endpoint is
fixed; redirects are refused. Missing credentials, optional dependencies or provider
failure degrade this capability only. Disabled startup does not import the HTTP adapter.
After a key change, CLI `decisions reload` re-enables future requests, without replaying
old failures. This explicitly authorized paid service is never a paid fallback for Codex.

## Owner integration and adoption rules

The stdlib `DecisionQuery(kind, reference, state, caller_run)` contract and
`kernel.decisions.decide` form the optional seam. The answer is either `status="ok"`
with a validated choice/receipt, or `status="fallback"`, `choice=null`, a reason and
`fallback="use_original_logic"`. Owners always retain final validation and execution.
Cancellation propagates rather than starting fallback work.

- Voice: accepted short, unhandled control-like text may be evaluated in an owned
  background coroutine. Known local commands run immediately. Only high-probability
  sleep, stop, continuous-chat and explicitly scoped current-task cancellation are
  adopted. Epoch, input revision, interaction focus and captured task identity must
  remain current. Other intents keep native handling. This does not intercept native
  automatic handoff, implement a new pre-execution gate, or prove physical takeover.
- Task models: only unspecified/automatic Codex choices use a `model_profile`
  recommendation. Explicit agent choices, fixed instance values, reminders and direct
  device queries bypass it. Existing policy/catalog validation still resolves the
  model; unavailable recommendations revert to the original default. Selection evidence
  and fallback reasons are saved with the task's existing model-selection receipt.
- Event inbox: only already-admitted, valid recorded events are eligible. A whole
  batch is skipped only when all events are clearly unrelated, above the configured
  threshold. Unknown/low-probability/failing judgments follow original dispatch.
  Events and queued-run state are revalidated after the wait, including failures;
  corrections/deletions/expiry invalidate stale input. Skipping preserves event history
  and records its reason in run trace. Event recording never waits for inference.
- MCP: `decision_support_status` describes availability and use rules locally;
  `judge_options` accepts a stable canonical UUID, bounded question/context and 1..15
  named candidates. `uncertain` is reserved. Use real evidence when this can reduce
  reasoning, not for an obvious choice or to judge whether an action happened.
  Background callers must still be running/waiting for the user. Failure is ordinary
  structured fallback, not a tool exception that requires replaying work.

Main/background registration uses the shared builtin MCP catalog. No third-party
registry entry, global Codex config, main voice binding or memory policy changes.
MCP instructions require agents to continue their original reasoning on fallback,
never loop on the tool or report optional judgment failure as user-task failure.

Question version 2 includes voice intent, event relevance, model profile and bounded
candidate selection. Every kind includes `uncertain`. Thresholds apply to the selected
option's probability; provider `confidence` is also retained but is not measured accuracy.
Shadow producers publish `DecisionSample` without awaiting it. Active mode does not
also consume those publications, avoiding duplicate evaluation. CLI manual fixtures
remain effect-free in both modes. Active, shadow and manual inputs all require the
same explicit external-context consent.

## Data minimization and reliability

Owners supply only relevant text/state summaries. JSON over 16 KiB is rejected, never
silently truncated. A structural guard rejects known credential fields, bearer/private
key strings and raw-media payload fields. It is not a general secret detector: producers
must curate inputs and must not include credentials disguised as ordinary text.
Inputs are detached and transient. Private SQLite retains source/reference, input hash,
byte count, question version, choice IDs, requested/actual model, probabilities, tokens,
latency, errors and optional expected labels; it does not duplicate raw conversation,
event content, question text or candidate descriptions.

One HTTP worker, a bounded 32-entry queue and a separate 64-entry observation subscription
isolate producers. Active requests have a 1.5-second total deadline by default and fail
fast when another judgment is busy. Identical in-flight requests share one attempt.
Late results cannot call owner actions. Receipt IO uses one owned thread, at most 16
outstanding requests, and caller cancellation does not wait for slow SQLite; shutdown
queues close after pending IO and drains the executor. Overflow is a coverage gap,
not a failure of primary work. Queued input is volatile and not replayed after restart.

Intent and UTC-day attempt count commit before HTTP. Failed/timed-out/unconfirmed calls
consume budget. No automatic HTTP retries occur. Auth failures suspend until explicit
reload/restart; other failures back off up to 15 minutes. Restart marks unfinished
intents unknown. Same source/reference/input digest/question version/model deduplicates
within retained receipts; changed input is a new judgment. A failed identity is not
retried after reload. Unknown outcomes never imply permission to replay an action.

The newest 2000 receipts are retained (100..10000 configurable); SQLite has a 128 MiB
page ceiling. The existing private filename is `state/decisions/shadow.sqlite3` in either
mode, preserving earlier receipts and budget. Paging/results are bounded. Statistics
cover retained receipts, not lifetime usage. `source:kind` separates manual fixtures
from live cohorts; baseline-parser agreement is distinct from labelled accuracy.
Latency is HTTP time, not evidence of faster end-to-end tasks. New active cohorts need
normal-use evidence before claiming accuracy or savings.

## Management, tests and acceptance

Local CLI through the existing private Unix socket:

```sh
personal-agent --config config.toml decisions status
personal-agent --config config.toml decisions list '{"limit":20}'
personal-agent --config config.toml decisions get '{"id":"64-character-receipt-id"}'
personal-agent --config config.toml decisions evaluate '{"kind":"voice_intent","reference":"synthetic:exit:1","state":{"utterance":"退下吧","execution_active":false}}'
personal-agent --config config.toml decisions label '{"id":"64-character-receipt-id","expected":"sleep"}'
personal-agent --config config.toml decisions reload
```

Manual evaluate acknowledges only volatile admission; poll get/list for committed output.
List defaults to 20, maximum 100, with a before cursor. Labels stay local.

Use `scripts/check.py --group decisions --group tasks --group codex` during development,
then the full check before deployment. Tests exercise real runtime/storage/inbox and
local HTTP framing, deadlines, cancellation, missing/disabled service, privacy guards,
coalescing, quota, restart, model fallbacks and concurrent event invalidation. Fake
providers establish contracts, not live-provider success. Explicit paid acceptance:

```sh
.venv/bin/python scripts/verify_decisions.py --config config.toml
```

Synthetic fixtures cover all kinds and the installed active MCP/capability paths,
without speech, device actions or launching tasks. Private acceptance evidence includes
matches, fallback reasons and latency. These small fixtures do not measure household
accuracy. Real voice adoption and task benefits require normal use after deployment.

Protocol references: [API](https://docs.typesafe.ai/api),
[models](https://docs.typesafe.ai/models), [confidence](https://docs.typesafe.ai/confidence).
