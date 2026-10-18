# Unified model access

This optional plugin owns **no-tool inference** and durable call receipts. Codex still
owns the execution loop, tools, skills and provider-thread protocol; tasks owns
scheduling, permissions, selection and checkpoints. LiteLLM only translates model
traffic. Subscription voice and its main thread remain bound to the existing backend.
API execution runs in a separate app-server process and never changes the subscription
login or shared Codex configuration. Jev retains its specialized decision contract.

## Configuration and deployment

Install the `models` extra in the project environment; add `models` to `plugins`.
Configure `model_api` in the instance `config.toml`. Official `preset` values are
`openai`, `claude`, `gemini`, `deepseek`; these supply protocol, official URL and key
variable. Fixed third-party `opencode_go_responses`, `opencode_go_chat`,
`opencode_go_anthropic` and `opencode_go_gemini` presets use `OPENCODE_API_KEY` and
Go-specific endpoints, not the ordinary inference endpoints. See `config.example/models.toml` and `deploy/model-api.env.example`. Custom services
require `protocol`, HTTPS `base_url`, and `key_env`; Gemini bases include the API
version path (such as `/v1beta`), because LiteLLM appends `/models/...` to an explicit base.
A preset cannot silently override
its fixed endpoint. Optional `auth_scheme = "bearer"` handles services whose authentication
differs from the native protocol. Optional `session_header` carries a hashed conversation
identity, derived server-side for auxiliary calls and from the native Codex thread header
for execution. Missing required identity fails before upstream dispatch. `.env` interpolation is disabled. Never place keys in TOML.

A model entry is a stable local alias, one upstream model and declared capabilities.
No random balancing, implicit model substitution, parameter dropping or paid keepalive. Explicit ordered fallback lists are supported as described below.
`context_window` declares an accepted execution context bound (default 32,768); too
small a window causes premature compaction. Configure it from the upstream limit.
`model_api.enabled` controls the whole API capability; each provider's `enabled`
controls whether its models may be called (default false). Disabled providers are
excluded from gateway routes and the executor catalog, and direct calls are rejected.
This is a local switch, not provider-side consent, a data filter or a training-policy
setting; turning it on does not prove credentials valid. `images`, `structured`, `reasoning_efforts` and
`execution_validated` describe **verified** capabilities, not provider marketing.
Unvalidated entries can be used in explicit acceptance, but never task execution.

```
.venv/bin/python -m assistant_runtime.cli --config config/config.toml models check
.venv/bin/python -m scripts.prepare_model_gateway --config config/config.toml
# The command prints the private compose.json path; do not cat it or commit it.
docker compose -f /absolute/instance/state/model-api/compose.json up -d
```

Generated Compose and LiteLLM files are disposable derivatives, not another registry.
The manifest contains **only referenced credentials** and is mode 0600 in a 0700
private directory. Its loopback listener requires a generated private gateway key.
The official LiteLLM 1.106.0 image is fixed by digest in the preparer; it has an
independent Python environment. No project dependency version is changed for LiteLLM.
One worker, CPU/memory/PID/log bounds are explicit. It has no mounted workspace,
Docker socket, audio, devices or subscription credentials. Do not expose its port.
The tested host/image combination rejects `no-new-privileges` at process startup;
the generated container uses Docker's ordinary isolation without added capabilities.

Configuration changes take effect together: back up private config/data, check/drain
in-flight tasks, regenerate gateway inputs, recreate the gateway, then restart the
assistant. Do not kill uncertain tool operations to force deployment. Avoid inspecting
Compose/Docker environment in ordinary logs. Gateway failure affects only API work.
Updating the image is an explicit revalidation and digest change, not a floating tag.
Only the secret-free `gateway/` assets are mounted; private Compose and local gateway
keys are outside that mount. The small transport callback sets the client user agent,
declared upstream authentication and session identity. It does not forward arbitrary
client headers, route models, retry, schedule work or execute tools. Transport normalization removes OpenAI's nonsemantic `prompt_cache_key` for Anthropic
Messages and Gemini. Declared reasoning levels are checked before forwarding; Anthropic
`none` explicitly sends `thinking.type=disabled`, because omission uses adaptive thinking.
Tools, images, positive reasoning levels and schemas remain strict.
Codex header names remain in the Codex adapter; the gateway receives only the declared
identity-header mapping.

Use `models list`, `models status` and `models health` to inspect local capabilities
and connectivity. Health queries do not request inference. `models infer '<JSON>'`
uses the same contract as MCP, including a stable `request_id`. `gateway-config`
prints only the secret-free conversion configuration.

## Calls and ownership

`ModelRequest` is a standard-library kernel contract. The independent `models` MCP
exposes `list_models` and `infer_with_model`. The server binds background run identity;
no caller-provided endpoints, credentials, tools or filesystem paths are accepted.
Text is bounded; images must be registered media IDs, and background images must
belong to the active run. These are auxiliary results, never operation receipts.

Each UUID is scoped to a caller, fingerprinted and reserved durably before transmission.
Lost replies, cancelled calls and restart during a call remain unknown; reuse the UUID
to inspect the receipt instead of resubmitting with a new one. Queue waits share the
same total deadline. Shutdown stops admission and cancels/drains existing calls.
Active task/version checks reject stale answers after handoff. The private database
stores output, usage, response identity, elapsed time, caller/version and config hash;
input bodies/images are not copied into the audit table or ordinary logs.

Inference has at most 4 workers + 16 queued calls by default, 60 seconds total,
24,000 text characters, 4 registered images, 2 MiB response stream and 8,192 auxiliary
output tokens. Native execution has a separate 16,384-token default output budget. Bounds are configurable within validated limits; no arbitrary daily call cap.
The gateway, client and API Codex provider all disable retries. An upstream service
may still process a request after a disconnect; cancellation is not proof of zero cost.

For task execution use an accepted entry alias in the existing four task profiles,
or explicitly select that model and a verified effort. Existing Jev selection sees
these same candidates. Cross-provider upgrades require the existing no-active/unknown
operation checkpoint guard, archive the old thread and start a fresh context; provider
private reasoning/continuation state is never copied. Changing an active run's provider
without that handoff fails. Web search, compression, images and free-form editing must
be tested individually; protocol compatibility alone does not establish support.

`model_api.memory` optionally binds existing Mem0 extraction to a structured-output
entry. Mem0, local embeddings, source validation, correction/deletion and history keep
their owners. Empty binding keeps existing subscription extraction. API memory uses only an explicitly configured entry fallback chain; every candidate must preserve the required schema capability. Subscription memory does not switch automatically. The API entry must be enabled and validated against actual
memory schemas/context sizes before enabling this binding. A disabled provider is
rejected during configuration validation.

## Reasoning configuration

Each API entry must declare the real supported `reasoning_efforts`; omission fails
configuration validation. It can also declare an optional
`default_reasoning_effort` selected from that list. An omitted default uses the first
listed value; it never inherits the main session or another provider. Both the models
MCP catalog and derived Codex metadata expose the same configured default and levels.
Task advice sees every supported level for the selected model. Explicit task choices
remain authoritative; Jev fills unresolved choices using the existing policy.

`infer_with_model` / CLI `models infer` accepts optional `reasoning_effort`. Omission
uses the selected entry's default. An explicit unsupported level fails before dispatch.
Each attempt and successful receipt records its actual effort. A configured fallback
that cannot honor the requested effort, image or schema capability is logged/skipped,
with no paid call and no silent downgrade. Omitted effort uses each candidate's own
default; explicit effort remains identical across all attempted candidates.

```toml
[model_api]
max_output_tokens = 8192           # auxiliary inference, includes thinking
execution_max_output_tokens = 16384 # native task model turns, includes thinking

[model_api.entries.deepseek-assist]
provider = "deepseek"
model = "deepseek-flash"
reasoning_efforts = ["none", "low", "high", "max"]
default_reasoning_effort = "low"
```

Both budgets allow per-entry overrides. Auxiliary output is bounded at 16,384 tokens;
execution output is bounded at 131,072 and must fit the selected upstream model.
The gateway's route default is the execution budget; auxiliary calls always send their
separate budget explicitly. A small cap can truncate thinking or tool arguments as well
as the final answer; reaching a cap is not evidence of an account quota failure.

Canonical levels verified through the current configured endpoints:

| Model | Exposed levels | Default |
| --- | --- | --- |
| DeepSeek Flash | none, low, high, max | low |
| Go GPT-6-Luna | none, low, medium, high, xhigh, max | low |
| Go Claude Haiku 5.5 | none, low, medium, high, xhigh, max | medium |
| Go GLM-5.3-Flash | low, high, max | low |
| Go Gemini 4 Argon Contributor | low, medium, high | medium; production provider disabled |

Subscription execution keeps using the signed-in account's native model catalog.
Its available levels need no duplicated local registry. Provider aliases are not
invented independent levels: DeepSeek's `medium` alias maps to `high`, while
GLM-5.3-Flash treats unrecognized values (including `medium`) as `max`.
Gemini Argon cannot disable thinking; `none` would be translated to a lower level
rather than off, so it is not advertised for that model.

Sources: [DeepSeek parameters](https://api-docs.deepseek.com/api/create-chat-completion/),
[GPT-6-Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[GLM model card](https://huggingface.co/zai-org/GLM-5.3-Flash),
[Gemini thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking).

Synthetic acceptance on 2026-10-19: all 22 configured model/effort combinations
returned the expected bounded answer through the pinned gateway. Reported reasoning
tokens were zero with DeepSeek/Claude/GPT `none`, and positive thinking samples were
observed at enabled levels. This does not imply monotonic token use or a quality
ranking: adaptive models can skip thinking on easy inputs. All five API routes also
passed native patch, edit and shell-readback checks at `high` (three actual tool items
each). Tests used isolated state, synthetic text, no household data, and no automatic
request retries. Real-provider acceptance still does not replace task-specific evals.

Repeat an explicit level only with existing synthetic-data/budget authorization:

```
.venv/bin/python -m scripts.verify_models --config /absolute/acceptance/config.toml \
  --model deepseek-assist --reasoning-effort high --confirm-paid --case text
.venv/bin/python -m scripts.verify_models --config /absolute/acceptance/config.toml \
  --model deepseek-assist --reasoning-effort high --confirm-paid --case execution-only --execution
```

## Explicit ordered fallback

An entry may declare `fallbacks = ["second-alias", "third-alias"]`. The requested
entry is first; at most three explicit backups follow, each once. A backup's own
list is not expanded. Known HTTP rejection (401/402/403/404/408/429/500/502/503/504)
permits trying the next candidate within the original total deadline. Unknown
transport outcomes, incomplete streams, cancellation and invalid output do not
silently start another call. Capability checks never drop images or schemas to fit
a backup. Entries must be enabled and suitable for the actual data/use scope.

One caller UUID covers the entire chain. Receipts retain `model` (requested),
`selected_model` (successful), and ordered `attempts`; exhaustion is failed, unknown
submission remains unknown. Logs include the request/model/error code, never keys,
input bodies or upstream error bodies. Reusing the UUID reads this receipt without
another paid call. Subscription voice/main and unbound subscription memory have no
API fallback through this setting. Task execution has its separate safe-boundary
policy in the tasks README; it does not use inference entry lists.

Synthetic paid acceptance with a local HTTP 402 injector and real backup execution:

```
.venv/bin/python -m scripts.verify_model_fallbacks --config /absolute/test-instance/config.toml \
  --model deepseek-assist --confirm-paid
```

The injector is loopback-only and never changes a real account balance. This checks
both inference and native execution, patch/shell/MCP evidence, UUID deduplication,
and selected-model receipts. Start and health-check the isolated gateway before
running the command; container creation alone does not establish HTTP readiness.

## Acceptance and limits

Run `scripts/check.py --group models --group codex --group tasks --group memory`
during development and the full gate after final edits. Offline tests cover HTTP
stream loss, tool refusal, schema checks, concurrency, cancellation, duplicate/unknown
receipts, caller binding, isolated process settings and guarded fresh-thread handoffs.
They do not establish provider fidelity or voice compatibility.

Real paid acceptance is explicit, synthetic and budgeted. Start with text, structured
output, registered image input and response usage. Then verify Codex shell/file edits,
tool results, cancellation/resume, MCP, skills/search/Goal, compression and restart.
Do not enable `execution_validated` or production profile changes until required
capabilities pass. Main subscription voice is not made interchangeable by this plugin.
Only the user-approved test provider may receive test data; other presets remain
unverified until their credentials, data scope and budget are supplied.

### Current isolated acceptance (2026-10-18)

DeepSeek `deepseek-flash`, official endpoint through the pinned gateway:

| Capability | Evidence |
| --- | --- |
| Text via Responses → Chat Completions | Passed; synthetic `17 + 25 = 42`, cold first text about 1.94 s |
| Registered image input | Passed; synthetic red image identified, total about 0.82 s |
| Codex native free-form patch + shell readback | Passed after explicit derived metadata; created/edited/read `probe.txt`, about 7.4 s |
| Strict JSON Schema | Rejected by upstream (`response_format` unavailable); do not enable `structured` |
| Other providers, complete MCP/search/Goal/resume/compaction/voice parity | Not yet accepted |

The test host's workspace sandbox could not initialize (`bwrap` network namespace
error). The successful isolated file test used the **existing instance's** configured
execution policy, with a temporary workspace and synthetic data; production policy
was not changed. The initial unknown-alias fallback omitted the dedicated patch tool;
`plugins/codex/api_catalog.py` now generates explicit native tool/capability metadata.
It does not copy provider continuation state or enable Responses-Lite features.

The production instance is not switched: execution and memory remain on their
existing paths until complete capability acceptance. Never infer permission to relax
schema requirements or use a shell substitute from these partial results.

Explicit repeatable acceptance (only after budget/data approval):

```
.venv/bin/python -m scripts.verify_models --config /absolute/test-instance/config.toml \
  --model deepseek-test --confirm-paid --case image
.venv/bin/python -m scripts.verify_models --config /absolute/test-instance/config.toml \
  --model deepseek-test --confirm-paid --case execution-only --execution
```

The script uses a private database and synthetic registered images. Execution also
requires a real `fileChange` receipt, not merely a successful shell substitution. It
uses the supplied config's sandbox policy, never silently elevates on failure, and
archives its provider thread on exit. It does not modify production configuration.


### OpenCode Go acceptance (2026-10-18)

All four presets are configured in the isolated acceptance instance. Synthetic coding
text passed both native requests and project Responses → gateway transport for:

| Protocol | Explicit upstream model | Gateway total latency (one sample) |
| --- | --- | --- |
| Responses | `gpt-6-luna` | 3.26 s |
| Chat Completions | `glm-5.3-flash` | 10.49 s |
| Anthropic Messages | `claude-haiku-5-5` | 3.94 s |
| Gemini native | `gemini-4-argon-contributor` | 6.66 s (subsequent authorized test) |

Gemini Contributor subsequently passed after the user confirmed platform access and
explicitly reauthorized synthetic testing. The non-Contributor `gemini-4-argon`
returned “Model is unavailable” on Go; no ordinary paid endpoint was substituted.
Latencies above are single samples, not a model ranking or gateway-overhead benchmark.
None of these entries is enabled for production.

Go endpoints use a truthful client user agent and stable conversation identity.
Model selection uses configured capabilities, supported efforts and ordered fallback
lists. A contributor model's data policy needs separate review before sending personal
data. Selecting a Go URL does not change the platform's optional balance overflow
setting; usage accounting remains the provider's responsibility.

GLM-5.3-Flash rejects `reasoning_effort="none"` because it is thinking-only.
The initial probe used `medium`, which the upstream accepted, but subsequent native
model-card review established that GLM-5.3-Flash maps unknown values to `max`. Current
configuration exposes only `low/high/max`; the earlier acceptance proved transport,
not the semantics of `medium`. Probes use each entry's configured default or an explicit
validated effort, and never assume that all models support `none`.

The three available routes also passed isolated **Codex native file-edit + shell
readback**: create `probe.txt`, edit `alpha` to `beta`, read it back, verify both file
contents and a real `fileChange` receipt. Results (single bounded synthetic runs):

| Route | Total execution time | Tool receipts |
| --- | --- | --- |
| `go-anthropic` | 27.45 s | 3 |
| `go-chat` | 28.32 s | 3 |
| `go-responses` | 27.07 s | 3 |

This establishes the basic multi-turn tool loop for those exact aliases, not complete
search or voice equivalence; the additional checks below cover selected MCP, Goal,
restart and image paths. All
`execution_validated` flags remain false. The isolated gateway is stopped after
acceptance; production service/configuration remains unchanged.


### Workflow acceptance (2026-10-19, Asia/Shanghai)

Synthetic tests use the real runtime, scheduler, independent MCP processes, native
Codex clients and isolated gateway. `scripts/model_acceptance_host.py` supplies inert
voice/device lifecycle boundaries, a private temporary Git project and database.
Inherited external MCP is disabled **in the fixture only**. Account apps are also
disabled, except the explicit search case, which enables only Search Service. Test
aliases are enabled for execution only in the fixture's memory; production flags and
subscription voice stay unchanged. Native Skill discovery is checked before execution;
its instructions and file proof stay inside the synthetic project.

Passed real-provider checks:

- Subscription GPT → tasks MCP → Claude task → models MCP → GLM; concurrent GPT API
  task → Claude inference. Two identical Claude MCP calls produce one durable model
  receipt. Completed task identities cannot make another inference call.
- Subscription GPT → Gemini task → Claude inference, with concurrent GPT API → Gemini.
  Results and model receipts retain the originating run/version.
- Pause a real running shell command, wait beyond its scheduled write time, restart
  the isolated runtime and both executor processes, then explicitly continue the same
  run/thread. The canceled write never appears and the earlier action is not repeated.
- Cancel a running command: no delayed write. Kill the API executor during inference:
  its task becomes unknown, restart does not replay it, explicit continuation recovers.
- Cancel auxiliary inference: its unknown receipt survives restart and duplicate
  queries do not create a second provider request.
- Claude discovers a synthetic native Skill, edits/verifies its proof using the native
  file tool, and completes its pre-existing native Goal through `update_goal`.
- All four Go routes consume a real synthetic MCP image result, and native
  `thread/compact/start` preserves a synthetic identifier for the following turn.
  Claude/Gemini passed the initial 128×64 two-color image. GPT misclassified that
  small sample and passed a 640×320 repeat; GLM recognized both colors but added
  an extra line, so visual correctness and formatting were assessed separately.
  These are transport/context proofs, not broad vision-accuracy or long-context
  stress benchmarks. Private native RPC traces record both failed and passed samples.
- All four Go routes pass synthetic registered image input and JSON Schema output.
  Gemini also passes native file edit + shell readback (41.49 s, three tool receipts).
  Gemini's image sample took 30.90 s; this is not a latency guarantee.

The initial mixed test exposed a real cold-start race: native optional MCP discovery
normally gets a shared one-second grace. API execution now sets
`mcp_optional_startup_grace_ms=0`, meaning **wait using each configured server's bounded
startup timeout**, not zero waiting. This uses Codex's existing startup mechanism;
there is no polling/retry loop or prompt workaround. The final mixed run passed with
this adapter setting, without a test-only wait override.

Gemini acceptance also exposed explicit-base URL composition and unsupported cache
hint issues. Versioned presets and the narrow transport normalization fix those;
reasoning, tools, images and output schema are still validated rather than dropped.

Repeat selected workflow cases only with explicit synthetic-data/budget approval:

```
.venv/bin/python -m scripts.verify_model_workflows --config /absolute/go-test/config.toml \
  --confirm-paid --case mixed --case gemini-mixed
.venv/bin/python -m scripts.verify_model_workflows --config /absolute/go-test/config.toml \
  --confirm-paid --case lifecycle --case inference-cancel --case executor-loss --case goal-skill
```

This workflow fixture expects `go-responses`, `go-chat`, `go-anthropic` and, when
requested, `go-gemini` aliases from the isolated acceptance setup. Private receipts
live under its `state/acceptance/`; temporary workspaces and native threads are cleaned
up. These explicit provider tests are excluded from automatic/offline test groups.

Additional real acceptance on 2026-10-19:

| Capability | Result and actual boundary |
| --- | --- |
| Search | Claude API task used the existing subscription Search Service MCP and returned the official Python TaskGroup source. No new search engine; subscription authentication remains required. |
| User clarification | Claude called native `request_user_input`, the task remained `waiting_user`, and a synthetic CSV answer resumed the same thread to completion. Background threads now explicitly enable the native Default-mode question capability. |
| Cross-provider upgrade | Claude wrote/read a checkpoint, requested the configured higher profile, ended its turn, and GPT completed verification in a fresh thread. One run and one applied handoff; no provider-private state transfer. |
| Memory | Real Mem0/local embeddings with the configured Claude API callback passed extraction, unchanged-fact deduplication, correction, restart/recall, exclusion of assistant reports and hypothetical/temporary facts, deletion with stale replay/import, and project/user isolation. |
| Established voice result handoff | A real subscription foreground execution called API inference through MCP; actual native PCM and final transcripts said `计算结果:42。` and, for a rejected model, `工具调用失败,没有结果。`. The success has one model receipt; validation failure dispatched none. Exit rejects old-generation audio. Input was synthetic execution text, not microphone/ASR acceptance. |
| Long context | 750 generated coding settings (68,458 input characters) retained start/middle/end markers after native compaction. This is a bounded contextual test, not maximum-window fidelity. |
| Gateway outage | Stopping the isolated gateway left its inference receipt unknown without replay; subscription foreground still completed, management stayed available, queue slots were released, and explicit gateway restart restored health. |

Four simultaneous synthetic inference calls finished in 4.52 seconds. Observed first
text ranged 2.54–4.38 seconds; host event-loop p95/max lag was 1.18/2.07 ms. Peak host
RSS was about 401 MiB (whole test process, not incremental gateway cost). Gateway
reported overhead was 27–48 ms for the three routes that supplied it; Gemini supplied
no overhead value. These headers are optional gateway telemetry, not independent
network decomposition, billing evidence or an SLA. Results now retain these optional
values without inventing missing measurements. Real local HTTP tests also cover
401/403/429/500, redirects, incomplete streams, output bounds, deadline cancellation,
queue overflow, bounded concurrency and management responsiveness without external calls.

Two boundaries remain explicit:

- An API-backed native thread sends realtime startup to the configured gateway's
  `/v1/live`, which returned 404 in the isolated probe. The gateway is a model
  conversion service, not a subscription voice backend. Main voice remains bound to
  the subscription thread; no main-model switch is exposed on this evidence.
- **Cold output-only notification speech is not accepted.** The native V3 endpoint
  sometimes acknowledges the first `appendSpeech` without producing a final spoken
  turn. Isolated samples succeeded, but the full cold notification case still failed;
  none of those successes override the failure. Media-session readiness and excluding
  conversational startup context do not establish a complete fix. Delivery now also
  requires a completed spoken turn plus drained audio, so startup noise/a quiet gap
  cannot falsely mark a notification delivered. A V2 subscription probe was also
  rejected (`AVAS realtime calls require realtime v1 or v3`), so no alternate native
  protocol is silently selected. Unknown delivery is not replayed and
  no fake user utterance, fixed warmup delay or replacement speech service is used.

Repeat only the necessary explicit cases with the same synthetic-data authorization:

```
.venv/bin/python -m scripts.verify_model_workflows --config /absolute/acceptance-instance/config.toml \
  --confirm-paid --case search --case clarification --case upgrade --case memory
.venv/bin/python -m scripts.verify_model_workflows --config /absolute/acceptance-instance/config.toml \
  --confirm-paid --case voice-active --case context --case performance
```

`--case voice` retains the failing cold-notification regression. `--case gateway-outage`
stops/restarts only a gateway under an explicit acceptance directory. Neither runs in
the automatic test gate. The four protocol routes, rather than every official vendor
endpoint, are this rollout's accepted transport scope. Production model bindings,
`execution_validated` flags, USB devices and the installed service are unchanged.
The user explicitly deferred the cold-notification limitation as a low-priority TODO
on 2026-10-19; it is no longer a blocker for this model-access scope. This decision
does not turn the failed case into a pass or authorize changing production bindings.

### TODO: reliable first speech for cold background notifications (low priority)

- Investigate native V3 startup/first-speech ordering when an output-only session is
  opened for a background notification. The first accepted `appendSpeech` can still
  produce no completed spoken turn.
- Keep uncertain delivery marked unknown, without automatic replay. Preserve the
  failing `--case voice` regression and the separate passing active-session evidence.
- Close this TODO only after repeated cold-start acceptance produces both audible
  output and a completed spoken turn, with cancellation and late audio still isolated.
- Deferred by user decision; do not expand the current implementation or restart
  production solely to address this issue.


### Ordered fallback acceptance and deployment (2026-10-19)

The subsequent implementation supersedes the earlier disabled-production notes.
A loopback fault injector returned HTTP 402 before any model work; auxiliary inference
and a native task each switched to official DeepSeek and completed. The task used
native patch, shell readback and task MCP, with one run and a fresh thread. Duplicate
inference UUIDs did not create another paid request. Native rejected threads can have
`systemError` status; eligibility still requires a complete, single failed-turn history
and no action items/terminals, not a status label alone.

Official DeepSeek also passed synthetic Search Service MCP, user clarification,
context compaction, Skill/Goal completion, pause/restart/same-thread continuation,
and cancellation without delayed writes. This uses existing subscription Search
Service access; it does not establish subscription-independent search. Strict JSON
Schema remains unsupported, so DeepSeek is not enabled for memory extraction.
One Go GPT cancellation repeat failed because the model misquoted the instructed
shell command; its prior successful transport/lifecycle tests do not imply perfect
command-following. The DeepSeek lifecycle repeat passed actual operation checks.

Deployment keeps subscription main voice, four primary task profiles and memory.
First-turn task rejections follow the configured ordered DeepSeek and Go backups. Contributor remains disabled
for production. No changed device bindings or automatic retry of uncertain work.
The gateway must be healthy before starting API work; the initial cold container
probe rejected transport before readiness, and the affected checks were repeated
only after health passed. Full local checks: 667 tests, zero skips.
