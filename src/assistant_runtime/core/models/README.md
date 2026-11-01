# Unified model access

Models owns immutable account/provider/model configuration, bounded no-tool inference and durable receipts. Pi owns the only agent loop and its tools/Skill/session protocol. Tasks owns selection, authorization, schedules, goals and handoff. All API and account routes reuse Pi's pinned provider library. Voice is an unavailable placeholder; subscription models do not introduce a second executor.

## Configuration

`main_model` and `main_reasoning_effort` select a registered alias and supported effort. Task recommendations reference the same aliases. `model_api.memory` explicitly selects a structured-output model for upstream Mem0; an empty binding leaves extraction unavailable rather than launching another agent. Local embeddings, source validation, corrections and memory history keep their existing owners.

Configure `[model_api.providers.NAME]` with `preset`, `enabled` and optionally `key_env`. Official presets: `openai`, `claude`, `gemini`, `deepseek`. OpenCode Go presets: `opencode_go_responses`, `opencode_go_chat`, `opencode_go_anthropic`, `opencode_go_gemini`, using `OPENCODE_API_KEY`. Custom endpoints declare `protocol`, HTTPS `base_url` and `key_env`; supported protocols are `openai_responses`, `openai_chat`, `anthropic`, `gemini`. Presets cannot silently override their fixed endpoint. A local `enabled` switch does not change the supplier's data policy.

OpenCode Console presets are `opencode_responses`, `opencode_chat`, `opencode_anthropic` and `opencode_gemini`. Console uses `https://opencode.ai/inference/v1/models`; Go uses `https://opencode.ai/inference/go/v1/models`. All four protocols share their route's OpenAI-shaped directory; inference URLs stay protocol-specific. These public directories do not establish key validity, account quota or inference acceptance. See the [Console API](https://opencode.ai/v2/docs/console/api/inference/) and [Go endpoints](https://opencode.ai/v2/docs/console/go/#endpoints).

Custom connections may declare `catalog = { url = "https://supplier.example/catalog/models", format = "openai" }`. Supported formats are `openai`, `anthropic` and `gemini`; the directory must share the inference origin so credentials cannot be forwarded to another host. Omit it to retain the protocol's default directory. The configuration page exposes this in advanced protocol settings; presets supply it automatically. Directory checks are bounded metadata GETs, never paid inference or automatic retries.

Each `[model_api.entries.ALIAS]` identifies exactly one provider and upstream model. Capability fields may be omitted. A route-bound `capabilities` snapshot supplies advertised metadata; ordinary `images`, `structured`, `context_window`, `output_limit` and `reasoning_efforts` settings are authoritative. Default effort is `auto` for new automatic entries; existing explicit lists retain their previous first-level default. Explicit unsupported inputs/efforts fail before dispatch; images and key parameters are never silently dropped. Different providers for the same upstream model have distinct aliases. There is no implicit balancing or model inheritance.

See [complete example](../../../../config.example/models.toml) and [key template](../../../../deploy/model-api.env.example). Secrets live in private `.env`, never TOML or logs. Config is composed by `Config.load()` and applied through the configuration service at a safe execution boundary; provider changes take effect at a safe boundary, without separate deployment.

```sh
pa --config config/config.toml models check
pa --config config/config.toml models list
```

API connections declare a protocol, endpoint and credential reference. The owned Pi helper registers these models with the corresponding upstream SDK adapter. No container, extra listener or instance transport key is required. Operator-only account login retains the existing isolated credential store. The execution loop sees only a host-authenticated local transport; supplier keys never enter model sessions. New route preparation captures credentials without inference, and failed updates retain the previous effective settings.

## Auxiliary inference

`ModelRequest`, CLI `models infer` and the independent models MCP expose the same no-tool capability. MCP caller identity is server-bound. Images use registered references owned by that caller, not arbitrary filesystem paths. A caller cannot supply endpoints, keys or recursive tools.

Stable request UUIDs are scoped/fingerprinted and reserved before dispatch. Reusing a UUID returns its receipt; lost response, cancellation or restart during a request remains unknown. Do not resubmit uncertain work using a new UUID. Each receipt includes caller/version, selected model, upstream request ID, latency, usage and explicit outcome without copying input bodies into audit logs.

Defaults: 4 workers, 16 queued calls, 600-second total deadline, 24,000 text characters, 4 images and a 2 MiB response bound. Single-call output caps default to disabled (`max_output_tokens=0`, `execution_max_output_tokens=0`) for tests, auxiliary inference, memory, foreground, tasks and compaction. Optional wire caps are omitted; required parameters such as Anthropic `max_tokens` retain Pi's model/context-derived value. Explicit positive global/per-entry caps remain opt-in and include reasoning. Advertised model capacity is reserved for usage admission when no request cap is specified; this reservation is accounting, not a parameter sent to the supplier. Cumulative usage policy, request counts, cancellation and deadlines remain independent.

## Native accounts and subscriptions

`model_api.accounts.NAME` declares a native Pi `provider` and an explicit `auth="oauth"` or `auth="api_key"`. An API-key account can reference `key_env`; otherwise operator login saves the key privately. OAuth always uses provider login and refresh, never a pasted password/token or a global Codex auth file. A provider table with `account="NAME"` selects this route instead of an endpoint preset. Model aliases and task recommendations use the same registry; optional Jev review respects explicit user choices.

```toml
[model_api.accounts.chatgpt]
provider = "openai"
auth = "oauth"
output_limit = "model"

[model_api.providers.chatgpt]
account = "chatgpt"
enabled = false
```

Native provider IDs and auth methods come from the installed, pinned Pi package, not a second maintained provider catalog:

| Common provider ID | Native account method in Pi 1.4.0 |
| --- | --- |
| `openai` | API key or Sign in with ChatGPT OAuth |
| `anthropic` | API key or Claude OAuth |
| `github-copilot` | API key or OAuth |
| `kimi-coding` | API key or OAuth |
| `xai` | API key or OAuth |
| `openrouter` | API key or OAuth; OAuth is not a subscription plan |
| `google` | Gemini API key; no Google-subscription OAuth in this adapter |
| `deepseek`, `mistral`, `groq`, `moonshotai`, `minimax`, `zai`, `opencode-go` | API key |

This table describes upstream implementation availability, not account eligibility or real-service acceptance. Other native providers can be inspected with the first command; ambient/IAM-specific configuration is not automatically synthesized. Account/model eligibility, quotas and terms remain supplier-specific. A successful login, bundled catalog and accepted model capability are separate states.

```sh
pa --config config/config.toml model-accounts providers
pa --config config/config.toml model-accounts login --account chatgpt
pa --config config/config.toml model-accounts status --account chatgpt
pa --config config/config.toml model-accounts catalog --account chatgpt
pa --config config/config.toml model-accounts logout --account chatgpt
```

These are local operator commands, not model/MCP actions. Account management is available even while execution bindings need repair. On a remote server, forward the callback port shown by the native login (ChatGPT currently uses `127.0.0.1:1455`), or paste the final callback URL into the private terminal prompt; never paste credentials/callback codes into chat. Browser completion cancels terminal input. Credentials are in mode-0600 files under mode-0700 `state/model-accounts/<account>/`; main/auxiliary processes share the native file lock for refresh. Different named accounts remain separate even for the same supplier. Logout affects only that account and does not cancel an already dispatched action.

The official [Sign in with ChatGPT flow](https://developers.openai.com/siwc/quickstart) uses the public Responses endpoint, separate authorized scopes and a participating account. Its [preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) differ from API keys: stream/store requirements, supported tools and output parameters need real acceptance. Pi owns those protocol details; the framework does not translate subscription credentials into ordinary API keys.

**Output limit exception:** ChatGPT subscription does not accept `max_output_tokens`. Configuration therefore requires explicit `output_limit="model"`; global/per-entry request token caps cannot constrain that upstream request. Usage reserves the native catalog's advertised maximum and records actual reported tokens. This is not proof that the supplier enforces that maximum. Work request limits, deadlines, stream bounds and accounting still apply. If strict monetary/token admission cannot reserve the request, it blocks. When a positive request cap is explicitly configured, other native routes must express it in their built payload or fail before sending; no silent drop is accepted. With the default zero setting, no artificial per-request cap is imposed. Do not copy Pi's API price estimate into subscription financial charges.

No new provider is enabled or selected automatically. Register an alias with capabilities marked unaccepted, validate bounded synthetic requests, then enable only accepted uses. Both native accounts and custom API connections use the shared Pi adapter. Both execution and no-tool inference share existing durable usage admission and no-replay behavior.

## AI usage admission

All framework AI-service calls pass the independent [ai_usage plugin](../ai_usage/README.md). It owns counting, budgets, statistics and durable reservations. Pi retains only authenticated transport adaptation. Models retains inference idempotency and business results, linked to usage receipts. `[model_api.execution_limits]` is removed; use `[ai_usage]` in its own configuration fragment. The request allowance and warning fraction use the ai_usage configuration; no additional model-specific request cap is introduced. Missing token telemetry is recorded as unknown and does not alone invalidate a completed result. Compaction and descendant calls share the host-owned work allowance across restart.

## Failure and explicit model changes

A request invokes only its selected model. Provider rejection is a failure; unknown
transport outcomes remain unknown. Neither tasks nor auxiliary inference automatically
switch models or replay calls. Removed `fallbacks` configuration is rejected.

Task model changes require an explicit recommendation, a clean checkpoint and verified
idle operations. The old context is archived; the new context receives confirmed progress,
not provider-private reasoning. User-explicit choices stay protected from optional Jev advice.

## Operator connection checks and model directories

The configuration console can inspect the current connection draft without enabling inference.
`catalog.py` performs one authenticated metadata GET with a 12-second timeout, a 1 MiB
response bound and at most 500 displayed entries. It never follows redirects, retries,
calls completion endpoints or reports upstream error bodies. Authentication uses the
configured protocol and optional bearer/session-header settings. An incomplete directory
is marked partial; manual model IDs remain available. Directory success is distinct from
inference capability and usage acceptance.

Protocol references: [OpenAI model listing](https://platform.openai.com/docs/api-reference/models/list),
[Anthropic model listing](https://platform.claude.com/docs/en/api/models/list),
and [Gemini model listing](https://ai.google.dev/api/models#method:-models.list).
Native accounts use Pi's existing local authentication check and bundled catalog, explicitly
labelled as local checks rather than successful remote inference.

## Acceptance

The model-library **测试模型** button performs one bounded text inference against the
current draft, without saving or enabling it. `probe.py` uses the same Pi adapters,
model concurrency semaphore and durable usage service as normal inference. Its fixed
synthetic prompt has no history, images or tools; the selected default effort is retained,
and the deadline is 30 seconds. Tests default to no per-request token cap, like other
model uses; an explicitly configured global/per-model positive cap is still honored.
Native `output_limit="model"` accounts use the existing adapter and native model bound
for usage reservation; that bound is not presented as an enforced request cap.
There are no retries or fallbacks. Request IDs have durable receipts so disconnected or
repeated submissions do not replay uncertain calls. Draft API keys stay in memory and
never replace saved credentials. A successful text reply does not assert image/tool/schema support.
Model selection has no manual acceptance flag; connection, credential and parameter
validation still applies. The authenticated management endpoint
forwards an operator-only `model-test` control command; it exposes no assistant tool.

Run `scripts/check.py --group models --group execution --group tasks`. Native Pi/local transport tests cover tools, mixed delegation, cancellation, clarification, restart and budget enforcement without calling a supplier. Earlier Codex acceptance is not evidence for Pi. Real routes are validated separately, per capability, with synthetic data and explicit fee/data authorization.

```sh
.venv/bin/python -m scripts.verify_models --config /private/candidate/config.toml \
  --model deepseek-assist --case tools --reasoning-effort none \
  --max-requests 3 --max-output-tokens 1024 --confirm-paid \
  --output /private/acceptance/deepseek-tools.json
```

The explicit acceptance entry caps forwarded requests and output tokens, uses a temporary workspace and no household MCPs, and never restarts production. Unverified images/search/long-context/provider-specific capabilities remain unverified. Voice and interactive MCP OAuth are TODOs.

Instance acceptance on 2026-10-23: independent ChatGPT OAuth authorization persisted and was reused across provider processes. `gpt-6.1-sol` at medium effort passed exact text, native image-color identification without tools, a real file command with readback, and strict JSON Schema extraction with synthetic source fields (5 total requests). An isolated host then passed subscription-main to native DeepSeek task delegation at low effort: duplicate submission produced one task, two caller identities shared one durable work allowance, and the saved file was read back (6 requests, all within the configured cap). A separate real Mem0 worker with local embeddings passed synthetic source-backed capture, search, restart/readback, receipt replay without another model call, and deletion, using 2 subscription requests through the models and usage plugins. Its database and usage ledger were temporary; OAuth used the existing locked account store without copying tokens. No production model, service, household data, memory database or voice route was changed. Receipts remain in private instance acceptance storage. This validates the tested routes, not every subscription model, long-context behavior or natural token-expiry refresh. Corrections, source rejection, stale jobs and cancellation additionally retain local real-engine contract tests.

## Deadlines

Ordinary inference and compaction each default to 600 seconds, configurable independently
with `model_api.timeout_seconds` and `model_api.compaction_timeout_seconds` (1..3600).
These are total deadlines, including streaming output; one task may contain many calls.
The models capability owns its configured inference deadline plus 30 seconds for settlement;
public plugin callers inherit it unless they explicitly supply a shorter deadline. Auxiliary
inference includes admission and its single selected-model request in the total budget.
The Pi outer provider envelope use the larger configured deadline plus 60 seconds;
local CLI/MCP transports allow further return margin. They do not extend the inner deadline.
Jev advisory fallback and device cancellation retain independent short deadlines.
Timeout logs include owning layer, operation, effective deadline and elapsed time, not payloads.
Timeouts never imply that a remote request or tool action did not occur: metering stays durable,
unknown results are not replayed. Configuration updates prepare a replacement adapter and switch at an idle boundary, preserving active execution context.

## Automatic capability metadata

`capabilities.py` resolves one immutable snapshot for UI, CLI and runtime. Only exact official endpoint/provider/model identities auto-match the packaged models.dev catalog. A custom endpoint may explicitly select a public `provider/model` reference; this remains advertised metadata, not route acceptance. Pi native model descriptions are read without opening a credential store or making a network request. Custom API connections use the declared protocol and explicit capability fields; account catalog metadata is not guessed for an unrelated endpoint. A reasoning boolean never generates effort levels. Unknown capability fields remain absent in metadata; explicit unsupported inputs remain rejected.

Snapshots include a non-secret connection/model binding, source and revision. Saving a stale binding fails with a repair instruction. Refresh is an operator draft operation, not a startup network request. Explicit adoption or refill copies known facts into ordinary settings, replacing their old values; absent facts leave settings unchanged. Model `output_limit` bounds output budgets and remains directly editable; snapshots supply defaults only for omitted settings. Existing native `output_limit="model"` semantics and durable usage admission are unchanged. `auto` is distinct from `none`: the API adapter omits automatic control before dispatch, and native Pi receives no explicit reasoning override. Normalized Pi engine `off` is translated back to the host's `auto` selection at the owned transport boundary.

Anthropic connections import explicitly advertised effort levels through the same
configuration fields, including OpenCode and custom endpoints. The Pi adapter reuses
its pinned catalog's adaptive-thinking declaration for exact upstream model IDs;
it never replaces the configured endpoint, key or enabled levels. Adaptive models
send `thinking.type="adaptive"` and `output_config.effort`; legacy models use Pi's
token-budget mapping. Unknown IDs retain Pi's budget-based behavior rather than
guessing adaptive support from a name. `auto` omits thinking controls, while an
enabled `none` explicitly disables thinking. Thinking and answer tokens share the
configured output ceiling; insufficient legacy thinking room fails before dispatch.
Structured output retains its format alongside effort. Catalog adoption is not
supplier acceptance: third-party support still requires separate route verification.

The MIT-licensed models.dev snapshot and license ship in the wheel. Maintainers explicitly run `python scripts/update_model_catalog.py` (or `--source <downloaded-json>`) and review the resulting metadata diff before release. Source hash and retrieval time are recorded; no runtime download or extra service is introduced. See https://github.com/anomalyco/models.dev for the source schema. Synthetic transport tests prove protocol behavior, not supplier capability acceptance.

Public capability references use a searchable dropdown with all matching records across all packaged models.dev suppliers. Names are ranked by normalized exact match, substring and search terms; suggestions never change a route or grant capabilities. The operator previews a selected record and explicitly copies its known values into the normal configuration draft. Unknown models remain savable. The dropdown reuses the collection filter and model-list styling; filtering is local, selection explicit, and no pages are hidden. Searches are local, authenticated and revision checked, and require neither credentials nor inference calls.

Catalog origin comes exclusively from the upstream `canonical_model_id` lab/model attribution retained by the compiler. Matching provider/lab IDs identify the original catalog; different IDs mean other catalogs, not proof of unofficial service. Missing or malformed attribution stays unknown. Search includes canonical identities to find service aliases, and original catalog entries rank first. No vendor allowlists or name-prefix heuristics participate. See [upstream attribution](https://github.com/anomalyco/models.dev#api-reference).

Model aliases accept letters, digits, dots, underscores and hyphens, beginning with a letter (64 characters maximum). The UI and owner schema share this rule; invalid names produce a name-specific error without echoing values. The catalog compiler retains explicit `reasoning_options` effort lists. Generic OpenAI routes import these lists. Gemini routes import explicitly declared none/minimal/low/medium/high controls; Pi owns their level or token-budget wire mapping. Unsupported levels are not copied or silently downgraded. Native Pi uses its adapter metadata first, and budget/toggle controls are not converted into guessed effort levels. Adopting metadata fills the normal effort settings and retains auto as the default unless the operator has selected a supported explicit level. Other protocol mappings remain explicit.


## Public capability references

The configuration console reads an instance-local models.dev snapshot immediately and
starts a single background refresh on metadata searches or capability reads. Concurrent
reads share that update; status polling does not start another download. The fixed public
HTTPS endpoint receives no supplier credentials. Downloads have a 20-second deadline and
16 MiB bound; only validated, complete metadata is atomically stored in
`state/model-catalog/models.dev.json`. A failed download, invalid response or failed save
keeps the previous snapshot. The packaged snapshot remains the offline fallback.

The editor shows the snapshot retrieval time, update state and a manual check/retry action.
Updated candidates are refreshed without applying them. Search, select a reference and use
the single fill action to stage known facts into normal parameter fields. Metadata updates
never save model configuration, replace an editing draft or change an active model route.
