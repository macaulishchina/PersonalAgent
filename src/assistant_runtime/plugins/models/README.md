# Unified model access

Models owns immutable account/provider/model configuration, bounded no-tool inference and durable receipts. Pi owns the only agent loop and its tools/Skill/session protocol. Tasks owns selection, authorization, schedules, goals and handoff. LiteLLM is an isolated API protocol gateway; native accounts reuse Pi's provider library. Voice is an unavailable placeholder; subscription models do not introduce a second executor.

## Configuration

`main_model` and `main_reasoning_effort` select a registered alias and supported effort. Task profiles reference the same aliases. `model_api.memory` explicitly selects a structured-output model for upstream Mem0; an empty binding leaves extraction unavailable rather than launching another agent. Local embeddings, source validation, corrections and memory history keep their existing owners.

Configure `[model_api.providers.NAME]` with `preset`, `enabled` and optionally `key_env`. Official presets: `openai`, `claude`, `gemini`, `deepseek`. OpenCode Go presets: `opencode_go_responses`, `opencode_go_chat`, `opencode_go_anthropic`, `opencode_go_gemini`, using `OPENCODE_API_KEY`. Custom endpoints declare `protocol`, HTTPS `base_url` and `key_env`; supported protocols are `openai_responses`, `openai_chat`, `anthropic`, `gemini`. Presets cannot silently override their fixed endpoint. A local `enabled` switch does not change the supplier's data policy.

Each `[model_api.entries.ALIAS]` identifies exactly one provider and upstream model. Declare `images`, `structured`, `execution_validated`, `context_window`, `reasoning_efforts` and `default_reasoning_effort` from actual acceptance. Explicit unsupported inputs/efforts fail before dispatch; images and key parameters are never silently dropped. Different providers for the same upstream model have distinct aliases. There is no implicit balancing or model inheritance.

See [complete example](../../../../config.example/models.toml) and [key template](../../../../deploy/model-api.env.example). Secrets live in private `.env`, never TOML or logs. Config is composed by `Config.load()` and takes effect on restart.

```sh
.venv/bin/python -m assistant_runtime.cli --config config/config.toml models check
.venv/bin/python -m assistant_runtime.cli --config config/config.toml models list
.venv/bin/python -m scripts.prepare_model_gateway --config config/config.toml
```

The preparer generates a private Compose manifest and secret-free gateway assets. The LiteLLM image is pinned by digest and uses its own dependencies, loopback listener, authentication and bounded resources. Do not print its manifest/environment. Gateway outages must not block event ingestion or device observation. Regeneration/recreation is explicit after checking in-flight work; it never constitutes task cancellation.

## Auxiliary inference

`ModelRequest`, CLI `models infer` and the independent models MCP expose the same no-tool capability. MCP caller identity is server-bound. Images use registered references owned by that caller, not arbitrary filesystem paths. A caller cannot supply endpoints, keys or recursive tools.

Stable request UUIDs are scoped/fingerprinted and reserved before dispatch. Reusing a UUID returns its receipt; lost response, cancellation or restart during a request remains unknown. Do not resubmit uncertain work using a new UUID. Each receipt includes caller/version, selected model, upstream request ID, latency, usage and explicit outcome without copying input bodies into audit logs.

Defaults: 4 workers, 16 queued calls, 60-second total deadline, 24,000 text characters, 4 images, 2 MiB response and 8,192 output tokens. Execution has its own 16,384-token per-request output default. Both output budgets allow per-entry overrides and include reasoning. A cap may truncate a tool argument or thinking as well as an answer; it is unrelated to account quota.

## Native accounts and subscriptions

`model_api.accounts.NAME` declares a native Pi `provider` and an explicit `auth="oauth"` or `auth="api_key"`. An API-key account can reference `key_env`; otherwise operator login saves the key privately. OAuth always uses provider login and refresh, never a pasted password/token or a global Codex auth file. A provider table with `account="NAME"` selects this route instead of an endpoint preset. Model aliases, task profiles, ordered fallback and Jev selection remain the same registry and policies.

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
.venv/bin/python -m assistant_runtime.cli --config config/config.toml model-accounts providers
.venv/bin/python -m assistant_runtime.cli --config config/config.toml model-accounts login --account chatgpt
.venv/bin/python -m assistant_runtime.cli --config config/config.toml model-accounts status --account chatgpt
.venv/bin/python -m assistant_runtime.cli --config config/config.toml model-accounts catalog --account chatgpt
.venv/bin/python -m assistant_runtime.cli --config config/config.toml model-accounts logout --account chatgpt
```

These are local operator commands, not model/MCP actions. Account management is available even while execution bindings need repair. On a remote server, forward the callback port shown by the native login (ChatGPT currently uses `127.0.0.1:1455`), or paste the final callback URL into the private terminal prompt; never paste credentials/callback codes into chat. Browser completion cancels terminal input. Credentials are in mode-0600 files under mode-0700 `state/model-accounts/<account>/`; main/auxiliary processes share the native file lock for refresh. Different named accounts remain separate even for the same supplier. Logout affects only that account and does not cancel an already dispatched action.

The official [Sign in with ChatGPT flow](https://developers.openai.com/siwc/quickstart) uses the public Responses endpoint, separate authorized scopes and a participating account. Its [preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) differ from API keys: stream/store requirements, supported tools and output parameters need real acceptance. Pi owns those protocol details; the framework does not translate subscription credentials into ordinary API keys.

**Output limit exception:** ChatGPT subscription does not accept `max_output_tokens`. Configuration therefore requires explicit `output_limit="model"`; global/per-entry request token caps cannot constrain that upstream request. Usage reserves the native catalog's advertised maximum and records actual reported tokens. This is not proof that the supplier enforces that maximum. Work request limits, deadlines, stream bounds and accounting still apply. If strict monetary/token admission cannot reserve the request, it blocks. Other native routes must express a requested output cap in their built payload or fail before sending; no silent drop is accepted. Do not copy Pi's API price estimate into subscription financial charges.

No new provider is enabled or selected automatically. Register an alias with capabilities marked unaccepted, validate bounded synthetic requests, then enable only accepted uses. Native paths work without sending model traffic through LiteLLM; API endpoint paths keep the pinned gateway. Both execution and no-tool inference share existing durable usage admission and no-replay behavior.

## AI usage admission

All framework AI-service calls pass the independent [ai_usage plugin](../ai_usage/README.md). It owns counting, budgets, statistics and durable reservations. Pi retains only authenticated transport adaptation. Models retains inference idempotency and business results, linked to usage receipts. `[model_api.execution_limits]` is removed; use `[ai_usage]` in its own configuration fragment. Defaults: 100 requests per work, detail warning at 50; no other consumption hard limit by default. Missing token telemetry is recorded as unknown and does not alone invalidate a completed result. Retry, fallback, compaction and descendant calls share the host-owned work allowance across restart.

## Fallback and model changes

Auxiliary entry `fallbacks` expands only the primary's ordered list, once per candidate. A backup must honor explicit effort/image/schema requirements; incompatible candidates are skipped with an error receipt, never downgraded silently. Cancellation, unknown transport outcomes and lost streams do not retry another supplier.

Task fallback is separately configured under `tasks.model_policy.fallbacks`. It requires a structured first-turn rejection and authoritative evidence that no tool/assistant activity occurred. Complex-task upgrades require a checkpoint with no active/unknown operations; old context is archived and the new model receives confirmed progress, not supplier-private reasoning state. User-explicit model choices remain protected from Jev advice.

## Acceptance

Run `scripts/check.py --group models --group execution --group tasks`. Native Pi/local transport tests cover tools, mixed delegation, cancellation, clarification, restart and budget enforcement without calling a supplier. Earlier Codex acceptance is not evidence for Pi. Real routes are validated separately, per capability, with synthetic data and explicit fee/data authorization.

```sh
.venv/bin/python -m scripts.verify_models --config /private/candidate/config.toml \
  --model deepseek-assist --case tools --reasoning-effort none \
  --max-requests 3 --max-output-tokens 1024 --confirm-paid \
  --output /private/acceptance/deepseek-tools.json
```

The explicit acceptance entry caps forwarded requests and output tokens, uses a temporary workspace and no household MCPs, and never restarts production. Unverified images/search/long-context/provider-specific capabilities remain unverified. Voice and interactive MCP OAuth are TODOs.

Instance acceptance on 2026-10-23: independent ChatGPT OAuth authorization persisted and was reused across provider processes. `gpt-6.1-sol` at medium effort passed exact text, native image-color identification without tools, a real file command with readback, and strict JSON Schema extraction with synthetic source fields (5 total requests). An isolated host then passed subscription-main to native DeepSeek task delegation at low effort: duplicate submission produced one task, two caller identities shared one durable work allowance, and the saved file was read back (6 requests, all within the configured cap). A separate real Mem0 worker with local embeddings passed synthetic source-backed capture, search, restart/readback, receipt replay without another model call, and deletion, using 2 subscription requests through the models and usage plugins. Its database and usage ledger were temporary; OAuth used the existing locked account store without copying tokens. No production model, service, household data, memory database or voice route was changed. Receipts remain in private instance acceptance storage. This validates the tested routes, not every subscription model, long-context behavior or natural token-expiry refresh. Corrections, source rejection, stale jobs and cancellation additionally retain local real-engine contract tests.
