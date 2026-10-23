# Personal Agent

A local assistant for Linux with one Pi execution harness, configured API or native-account models, a Web conversation and durable background tasks. Device, media, search, event, notification and memory capabilities remain separate plugins. Voice input/dialogue/playback is currently disabled pending independent acceptance; existing USB bindings and the independent audio benchmark are retained.

## Install and configure

Use Python 3.12 and the project's existing `.venv`. Install the required optional extras from `pyproject.toml`; install the pinned external execution runtime explicitly:

```sh
scripts/setup_pi.sh
cp -r config.example config
.venv/bin/python -m assistant_runtime.cli --config config/config.toml models check
```

Private instance configuration lives in ignored `config/`. `config/config.toml` explicitly includes fragments for models, tasks, devices, memory and services. `root` is the instance root containing `.env`, `state/` and `models/`; preserve it when relocating configuration. Includes cannot be recursive or silently override a field. Secrets belong only in private `.env` or credential storage, never in TOML or Git.

Set `backend="pi"`, `pi_binary`, `main_model` and `main_reasoning_effort`. Main and task model aliases refer to the same `model_api.entries` catalog. Official OpenAI, Claude, Gemini and DeepSeek presets provide URL/protocol/key defaults; custom endpoints and OpenCode Go presets are supported. Declare actual image/context/reasoning capabilities and validate a route before enabling execution. Task profiles `fast / balanced / deep / ultra` reference aliases and supported efforts; there is no implicit parent-model inheritance. Mem0 requires its own explicit structured-output `model_api.memory` binding.

See [model configuration](src/assistant_runtime/plugins/models/README.md), [task configuration](src/assistant_runtime/plugins/tasks/README.md), [environment keys](deploy/model-api.env.example) and [Pi adapter](src/assistant_runtime/plugins/pi/README.md).

Pi-native account providers reuse its OAuth/API-key implementation without launching another agent. Configure named `model_api.accounts` and bind providers to those accounts. Operator commands `model-accounts providers`, `login --account NAME`, `status --account NAME` and `catalog --account NAME` manage the private connection. ChatGPT subscription uses the official Sign in with ChatGPT route; its single-request output cap is unavailable and must be explicitly configured as `output_limit="model"`. Available login methods, authentication, static model catalogs and real capability acceptance are distinct; see the model guide before enabling a route.

LiteLLM is a pinned, isolated protocol gateway. It does not own tools or task scheduling:

```sh
.venv/bin/python -m scripts.prepare_model_gateway --config config/config.toml
# Use the private compose.json path printed by the preparer; do not print its contents.
docker compose -f /absolute/instance/state/model-api/compose.json up -d
.venv/bin/python -m assistant_runtime.cli --config config/config.toml run
```

The gateway listens on loopback with private authentication. Model switches, retry policy and accepted capabilities remain application configuration. Configuration changes require an explicit restart after in-flight work is checked.

## Execution limits and recovery

AI calls use the independent [ai_usage plugin](src/assistant_runtime/plugins/ai_usage/README.md): each work shares a durable allowance across its tasks, auxiliary models, Jev and paid search. Defaults are 100 upstream requests, a task-detail warning at 50 and denial before request 101. Other consumption hard limits are opt-in. Configure `ai-usage.toml`; records distinguish verified outcomes, unknown usage and estimated cost. Metering failure pauses new AI calls without shutting down devices or the web.

Reservations precede forwarding. Continuations, fallback, model upgrades and restart retain the same run budget. An interrupted execution request without a confirmed result blocks further execution dispatch as unknown. Missing token telemetry alone does not fail a confirmed action. Native retries and paid cache warming are disabled. Every attempt records model, time, status and reported usage without prompt bodies or credentials. Execution trace includes the budget receipt. A periodic plan dispatches one finite run per trigger; tasks owns bounded goal continuation, not an agent sleeping until the next schedule. Budget exhaustion is a stopped execution, never successful completion or a reason to try another model.

Cancellation clears queued input, interrupts work and verifies tool settlement. Unconfirmed operations are never replayed. Model upgrades require an idle, verified checkpoint. Task results and notifications retain their business identities and delivery ownership.

Existing Codex conversations/plans are not imported. Before cutover, inspect in-flight operations, stop the instance and preview the explicit backup/reset:

```sh
.venv/bin/python -m scripts.reset_execution_state --root /absolute/instance
# Only after stopped-instance review:
.venv/bin/python -m scripts.reset_execution_state --root /absolute/instance --apply --confirmed-stopped
```

The tool archives configuration/database, resets old conversation/task state and preserves transcripts needed as memory evidence, memory, media, events, credentials and physical files. The old running service must not be restarted against new source and old execution configuration. Real-provider acceptance precedes cutover; old provider receipts do not establish Pi parity.

## Plugins and interfaces

The Web text/image entrance, CLI and independent MCP services reuse the same kernel capabilities. Execution has full local permissions; OS sandboxing is a TODO. Server-bound MCP task identity, resource scope, tool policy, revisions and no-replay rules remain enforced. Interactive MCP OAuth is not implemented by this application's Pi adapter; configure credentials and verify the connection explicitly.

- [Tasks](src/assistant_runtime/plugins/tasks/README.md): schedules, event inbox, goal progress, model selection, trace and result ownership.
- [Notifications](src/assistant_runtime/plugins/notifications/README.md): persistent inbox and grouping.
- [Events](src/assistant_runtime/plugins/events/README.md): producers report observations; storing them does not start inference.
- [Media](src/assistant_runtime/plugins/media/README.md): search, cloud storage, local downloads and file management.
- [Search](src/assistant_runtime/plugins/search/README.md): public webpage search/fetch through independently configured providers.
- [Integrations](src/assistant_runtime/plugins/integrations/README.md): revisioned stdio/HTTP MCP definitions and scoped tool policy.
- [PipeWire](src/assistant_runtime/plugins/pipewire/README.md): fixed audio resources and the independent audio/input benchmark.
- [Camera gateway](deploy/go2rtc.md): stable camera identity and explicit device acceptance.

No device observation, event write or memory lookup independently activates a model. Media capture requires an explicit request. Default policy does not retain raw audio/video. Fixed audio bindings cannot be switched by model tools.

## Development and verification

```sh
.venv/bin/python scripts/check.py --group execution --group tasks --group models
.venv/bin/python scripts/check.py
.venv/bin/python -m scripts.verify_integrations
```

Targeted checks support development; run the full gate after final source/dependency edits before commit or deployment. Native Pi tests use a private synthetic Responses endpoint and actual subprocesses, MCP calls and files. They cover mixed-model delegation, clarification, selection, cancellation, restart, runaway loops and compaction budgets without paid requests. Actual providers and hardware require separate explicit acceptance. `scripts.verify_models` has enforced synthetic-data request/output caps; do not remove them for convenience.

`scripts/check_audio_input.py` remains an independent audio benchmark; stop the service before acoustic testing. Voice support and OS sandboxing are future work, not simulated success. Private receipts and experiments belong under ignored `state/acceptance/`; reports are generated only when requested.

See [ARCHITECTURE.md](ARCHITECTURE.md) for ownership and [AGENTS.md](AGENTS.md) for engineering rules.

Production preparation and cutover: [Pi release runbook](deploy/pi-release.md).
`scripts.check_pi_release` is a read-only configuration preflight, separate from
real-provider acceptance and deployment.


助手技能分为业务插件随包交付的通用操作技能，以及私有
`config/skills/<名称>/SKILL.md` 中的实例设备经验。后者需在配置总入口用
`instance_skills = ["名称"]` 显式选择；默认不加载。开发工具的 `.agents/`
和 `.agent/` 完全忽略，不作为助手输入。根目录 `AGENTS.md` 是版本化的开发规范。
MCP 和事件源注册表分别放在私有 `config/mcp/servers.json`、
`config/observations/sources.json`；模板只用于新实例，勿覆盖已有注册表。
