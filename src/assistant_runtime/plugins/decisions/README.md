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
active_timeout_seconds = 3
timeout_seconds = 8
warmup_enabled = true
keepalive_seconds = 120
dns_cache_seconds = 300
queue_size = 32
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
operation; active callers receive fallback. There is no local daily or lifetime
call-count cap. To stop external requests, use the enabled switch.
When upgrading an older instance, remove the retired `daily_limit` setting from its
configuration; there is no replacement quota setting.

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
- Task models: missing profile and/or reasoning effort use one joint `model_profile`
  recommendation among bounded account-supported combinations. Fixed instance values
  and explicit agent fields filter the candidates and cannot be overwritten; fully
  determined choices, single-combination cases, reminders and direct device queries
  bypass semantic inference. Existing policy/catalog validation still resolves the
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

Voice intent, event relevance and bounded candidate selection use question version 2;
model profile uses version 5 with `objective` and 1..32 `options`, each a closed
`{profile, model, reasoning_effort}` combination keyed as `profile_effort` (e.g.
`deep_high`). The adapter batches two small native Choice questions in one HTTP
request, validating both responses and retaining each probability distribution.
`field_choices` contains only fields above their confidence threshold; other fields
are null. `confidence_scope=per_field` never claims a joint probability. `choice`
is the raw combined candidate (or `uncertain`), not authority to apply an uncertain
field. The task owner adopts only confident unresolved fields and validates the final
combination. Token usage is recorded once for the whole response, not summed twice.
No arbitrary model names or efforts are generated. The new model-profile version invalidates only that
kind's cached decisions; old objective-only model samples must supply options.
Profiles are `fast / balanced / deep / ultra`, plus `custom` for an explicitly supplied
model outside those mappings. `ultra` is reserved for exceptional
reasoning/original research with concrete justification that `deep` is insufficient;
ordinary research search/summaries do not qualify. Every kind includes `uncertain`. Thresholds apply to the selected
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
isolate producers. Active requests have a 3-second total deadline by default and fail
fast when another judgment is busy. Identical in-flight requests share one attempt.
Late results cannot call owner actions. Receipt IO uses one owned thread, at most 16
outstanding requests, and caller cancellation does not wait for slow SQLite; shutdown
queues close after pending IO and drains the executor. Overflow is a coverage gap,
not a failure of primary work. Queued input is volatile and not replayed after restart.

The adapter owns one HTTP session with at most two connections (one judgment plus one
startup metadata request), a 120-second idle reuse window and a 300-second DNS cache.
These are client ceilings, not a promise that the server or proxy retains connections.
DNS caching does not pin an established connection or override TLS validation.
After enabled/consent/credential checks, startup schedules one owned asynchronous
`GET /v1/models` after `Runtime.ready`, limited to 3 seconds (or a shorter provider
timeout) and 64 KiB. Plugin initialization does not consume that network budget.
It sends no judgment state and performs no model inference. It does not hold the
judgment lock, delay readiness or change judgment authentication/backoff on failure.
Shutdown cancels it before closing the shared session. Set `warmup_enabled=false`
to disable it. Credential reload does not repeat it. No periodic keepwarm, paid dummy
inference, automatic warmup retry or maintenance request is scheduled.

Status reports sanitized warmup outcome and the latest judgment's connection reuse,
DNS time, connection time, response-header wait and total HTTP time. Connection time
includes DNS/TCP/TLS and any proxy handshake; response wait includes network and server
work, not just inference. Missing phase times mean that phase was not observed.

Intent and UTC-day attempt count commit before HTTP. The count includes failed,
timed-out and unconfirmed calls and is informational only, never an admission limit.
No automatic HTTP retries occur. Auth failures suspend until explicit
reload/restart; other failures back off up to 15 minutes. Restart marks unfinished
intents unknown. Same source/reference/input digest/question version/model deduplicates
within retained receipts; changed input is a new judgment. A failed identity is not
retried after reload. Unknown outcomes never imply permission to replay an action.

The newest 2000 receipts are retained (100..10000 configurable); SQLite has a 128 MiB
page ceiling. The existing private filename is `state/decisions/shadow.sqlite3` in either
mode, preserving earlier receipts and usage counts. Paging/results are bounded. Statistics
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
coalescing, calls beyond the former daily cap, restart, model fallbacks and concurrent event invalidation. Fake
providers establish contracts, not live-provider success. Explicit paid acceptance:

```sh
.venv/bin/python scripts/verify_decisions.py --config config.toml
.venv/bin/python scripts/verify_decisions_transport.py --config config.toml
```

Synthetic fixtures cover all kinds and the installed active MCP/capability paths,
without speech, device actions or launching tasks. Private acceptance evidence includes
matches, fallback reasons and latency. These small fixtures do not measure household
accuracy. Real voice adoption and task benefits require normal use after deployment.

Transport acceptance explicitly makes three synthetic paid judgments: after metadata
warmup, immediately again, and after 20 seconds idle (past the old 15-second default).
It records actual reuse and timings in private instance storage, and checks installed
warmup/timeout configuration without sending device commands. It never runs on a timer.

Protocol references: [API](https://docs.typesafe.ai/api),
[models](https://docs.typesafe.ai/models), [confidence](https://docs.typesafe.ai/confidence).
