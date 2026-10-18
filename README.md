# Personal Agent

A local, reactive personal assistant for Linux. The current brain is Codex with the existing ChatGPT subscription: native voice, task execution and persistent task context. Local plugins supply microphones, speakers, wake detection, long-term memory and durable background tasks. The application is not named after its wake word or deployment machine.

See [architecture](ARCHITECTURE.md), the [hardware onboarding standard](ARCHITECTURE.md#hardware-onboarding-standard), [requirements](REQUIREMENTS.md), [validation](VALIDATION.md), [wake-word evaluation](reports/WAKE_EVALUATION.md), the [ASR candidate CPU comparison](reports/ASR_CANDIDATE_COMPARISON.html) ([full text](reports/ASR_CANDIDATE_COMPARISON.md)), the [human-voice comparison](reports/WAKE_HUMAN_COMPARISON.html) ([full text](reports/WAKE_HUMAN_COMPARISON.md)), and the [synthetic-voice comparison](reports/WAKE_MODEL_COMPARISON.html) ([full text](reports/WAKE_MODEL_COMPARISON.md)).

Camera usage experience is maintained in [相机设备使用指南](.agents/skills/camera-device-guide/SKILL.md).

## Install

Python 3.12 and `uv` are required. All application and memory dependencies share one `.venv`.

```sh
uv sync --locked --extra voice --extra memory --extra tasks --extra dev
.venv/bin/playwright install chromium
.venv/bin/python scripts/download_models.py --wake-bundle /path/to/fsmn-v2
cp config.example.toml config.toml
```

The example starts a hardware-free management host. For speech configure `profile="voice"`, an absolute instance `root`, `workspace`, model location, input identity and output map. Configure Codex's existing subscription login normally. No separate paid API key is used. Linux voice also needs `pactl` and `pacat`; Bluetooth outputs need BlueZ. Optional camera capture occurs only on explicit requests.

```toml
profile = "voice"
root = "/path/to/instance"
workspace = "/path/to/workspace"
model_dir = "/path/to/wake-models"
codex_binary = "/path/to/codex"
codex_model = "gpt-6.1-sol"
codex_reasoning_effort = "high"
memory_model = "gpt-6-luna"
memory_reasoning_effort = "low"
realtime_voice = "cove"
assistant_name = "个人助理"
wake_words = ["嘟嘟", "你好嘟嘟"]
input_device = "office_microphone"
default_output = "office"
minimum_volume = 80
[devices.office_microphone]
adapter = "pipewire"
kind = "microphone"
label = "书房麦克风"
location = "书房"
[devices.office_microphone.connection]
match = "stable microphone identity"
[devices.office]
adapter = "pipewire"
kind = "speaker"
label = "书房音箱"
location = "书房"
[devices.office.connection]
address = "AA:BB:CC:DD:EE:FF" # or sink = "exact PipeWire sink name"
```

`codex_model` and `codex_reasoning_effort` select the foreground task model and effort. Mem0 extraction and ADD/UPDATE/DELETE/NONE decisions independently use `memory_model` and `memory_reasoning_effort` (default `gpt-6-luna` / `low`), through the same official ChatGPT subscription login. Changing one pair does not change the other; global Codex settings are untouched. Use values supported by the installed Codex and account. There is no paid API or automatic model fallback; a lighter model does not imply a specific subscription quota saving or measured latency. Local embeddings remain independent. `realtime_voice` selects the voice timbre, not its model. After editing `config.toml`, run `personal-agent --config config.toml restart`. `status` reports task settings and voice, while its `memory` section reports requested and last provider-returned memory model/effort; unavailable returned fields are null rather than guessed.

`minimum_volume` raises readable channels below the floor, preserves louder channels and skips unknown volume. There is no extra gain/limiter. Output loss does not route audio to another room. Native audio uses Chromium WebRTC and optional WebRTC AEC3; the wake model receives the original mic stream.

Optional `quiet_hours = {enable = true, start = "23:00", end = "07:00", timezone = "Asia/Shanghai"}` suppresses keyword/gesture activation, speech PCM, confirmations and wake/error chimes in that local-time interval. Set `enable = false` to disable silence while preserving the schedule; the flag must be a TOML boolean, and schedule fields are validated even when disabled. Start is inclusive and end exclusive; overnight intervals cross midnight. The one-second maintenance loop clears queued playback and closes an active voice session, preserving background tasks. At the end the assistant stays in standby until explicitly awakened. This does not change device volume or wake sensitivity. An omitted/empty table has no silence schedule. Configuration changes require a service reload/restart; this setting currently has no voice-command override.

For a core-only, hardware-free installation use `uv sync --locked`; `profile="headless"` runs management/explicit-trigger validation without voice, models, Codex or memory. Missing capabilities are reported rather than simulated as completed tasks.

## Memory and device MCP services

The assistant connects two independent local MCP servers. They reuse the existing host capability handlers and one `.venv`; no new device driver or memory database runs inside MCP.

| Server | Tools |
| --- | --- |
| `memory` | `search_memory`, `read_memory`, `remember_memory`, `update_memory`, `forget_memory` |
| `device` | `list_devices`, `inspect_device`, `capture_image`, `record_video`, `move_camera`, `stop_camera`, `list_device_operations`, `get_device_operation`, `cancel_device_operation`, `set_audio_volume`, `stop_playback`, `end_voice_conversation` |

Memory updates require the current revision obtained from `read_memory`; read/search never save facts. Device queries read the shared live inventory, so newly onboarded sensors/cameras/speakers appear without editing MCP. Supported read capabilities are listed in each device descriptor. Adding a new kind of actuator command still requires an explicit capability and tool. Stop playback and cancel task remain distinct; task-status queries do not cause speech.

Codex registers these services in this project's foreground and authorized background threads, with the already authorized local-operation permissions. Memory-curation threads disable them. Another MCP client can launch either service independently with `.venv/bin/python -m assistant_runtime.mcp memory --socket /path/to/instance/state/control.sock` or replace `memory` with `device`. Use the actual `Config.socket` path (see `doctor`/instance root); the combined `personal_agent` MCP is removed. Registration does not alter global Codex settings or memory identities on disk.

For repeatable isolated verification run `.venv/bin/python scripts/verify_mcp.py --config config.toml`. This checks separate real stdio inventories, actual Mem0 create/read/update/search/forget and revision conflicts, newly registered synthetic devices, native image content, and real Codex calls to both servers. It uses temporary synthetic state, no room capture/playback or production memories. Private evidence goes to `state/acceptance/mcp/`.

## Shared Web chat

The optional `web` plugin is a text-and-image entrance to the **same persistent conversation** as native voice. It is not a new assistant, task thread, or management website. It runs inside the existing service. Browser tabs receive ordered message snapshots over resumable SSE; duplicate request IDs do not execute twice. Historical transcripts are imported locally and marked as historical records.

Build once with Node/npm; the deployed service needs only Python:

```sh
uv sync --locked --extra voice --extra memory --extra tasks --extra web --extra dev
npm --prefix frontend ci
npm --prefix frontend run build
```

Retain any other enabled extras, such as `airplay` and `camera`, when running `uv sync`. In the voice-profile instance configuration, add `"web"` to the existing `plugins` list and add this top-level setting before any TOML tables:

```toml
web = {enable = true, host = "0.0.0.0", port = 8046}
```

Restart with `.venv/bin/personal-agent --config config.toml restart`. Open `http://SERVER_IP:8046/`. The existing report server on 8045 is independent. At first startup a random access credential is written to `<instance-root>/state/web-access-token`, readable only by the service account. Read it locally and enter it on the login page; do not commit it or put it in a URL. `web.token_file` optionally overrides that path. An explicitly approved existing password can be pinned with `web.approved_short_password_sha256` in private instance configuration; only that exact password (at least eight characters) is exempt from the normal 24-character minimum. This does not change or regenerate the credential file. Browser login lasts seven days and survives service restarts. To rotate access, replace that file with a new strong random token and restart; existing cookies are invalidated. Bind to `127.0.0.1` for SSH tunnels or use a trusted LAN/VPN. The plain HTTP listener is not intended for public Internet exposure.

The page supports text, paste/drop/upload of up to four JPEG/PNG/WebP images (10 MB each), Markdown/code copying, task details, stop playback, and cancel task. User uploads and image content returned by tools share private chat attachments. Tool images are visible even when execution details are collapsed, support the same preview, and survive refresh/restart. Requested camera frames are saved before provider display/history truncation; the result carries registered attachment IDs. Available complete image content or valid references can repair missing attachments without another device capture. Already-truncated old pictures cannot be reconstructed. There is no browser audio input/output. Web messages select silent playback until the next actual voice user turn; microphone reception and background work continue. Quiet hours affect voice only. A new message during a task steers that task instead of replacing it. Interrupted or uncertain outcomes are shown, never automatically replayed.

Chat data and normalized images live in private `state/`; unattached uploads expire after a day. Never expose that directory through a static file server. Final voice transcripts are synchronized into the provider task context without creating tasks; Web input/results update live voice context. If synchronization fails, the page displays the failure. Different tabs converge on the same authoritative server state; while disconnected they display their last received state, then catch up.

Validation:

```sh
.venv/bin/python scripts/check.py
.venv/bin/python scripts/verify_web.py --config config.toml
```

For the real subscription → MCP → two-browser image path, including a large synthetic camera frame and recovery without recapture, use `.venv/bin/python scripts/verify_web.py --config config.toml --reply-images`. It does not capture the room.

For a read-only check of existing assistant images in the installed local UI use `.venv/bin/python scripts/verify_web.py --config config.toml --installed-images`; it checks two browsers, preview, reload and connection recovery without capturing another image or starting a model task.

The default second command uses real subscription calls, two isolated browsers, synthetic speech and a generated image in a temporary workspace. It neither records the room nor plays through speakers, and does not read production conversation history. Private acceptance evidence is written to `state/acceptance/web/`. Frontend changes require a rebuild; the standard check runs its build when its dependencies are installed.

## Development checks

For the development loop, select affected contracts instead of rebuilding and
testing every domain after each edit:

```sh
.venv/bin/python scripts/check.py --list
.venv/bin/python scripts/check.py --group codex --group tasks
.venv/bin/python scripts/check.py --test test_integrations.AdapterTest
.venv/bin/python scripts/check.py --static-only
```

No arguments run the full pre-commit gate: format, lint, kernel types, installed
frontend build, and every discovered test module. Selection is explicitly labeled
`SELECTED`; it never counts as a full pass. Frontend checks run for web selections
or explicit `--frontend`, so backend iterations skip the build. New/nested test
modules are included automatically; unassigned modules appear in `core`.

Tests run in separate module processes, up to three at a time, with measured slow
modules first. Temporary databases/ports remain isolated and the real Mem0 tests
stay in one process lane. Use `--jobs 1` for serial diagnosis, `--timeout` to adjust
the per-command/module bound, and `--verbose` for complete output and slow test
durations. Failures, skips, per-stage/module times and total wall time are reported;
failure/timeout returns nonzero. Real hardware/provider acceptance and service
restart are separate explicit operations, never part of parallel unit checks.

Run the full gate once the final source/tests/dependencies are ready, before
commit/push or application-code deployment. Configuration-only changes reuse an
unchanged code gate and validate the configuration plus affected live behavior.
Do not add result caching or reduce coverage to improve the reported time.

## Run and maintain

```sh
export PERSONAL_AGENT_CONFIG="$PWD/config.toml"
.venv/bin/personal-agent run
.venv/bin/personal-agent status
.venv/bin/personal-agent doctor
.venv/bin/personal-agent devices list
.venv/bin/personal-agent devices inspect office_microphone
.venv/bin/personal-agent gesture wave
.venv/bin/personal-agent stop
.venv/bin/personal-agent sleep
.venv/bin/personal-agent cancel
.venv/bin/personal-agent volume 80
```

The sole production wake model is **FSMN V2**, threshold **0.485**, 80 ms input and one ONNX thread. Its self-contained bundle lives under `models/fsmn-v2/` (ONNX, normalization, tokens, lexicon and checksummed manifest). Copy this private bundle with the instance when deploying to another Linux host; it is not downloaded from a public release. No old detector or automatic fallback is shipped. `status` reports the loaded model/hash and streaming buffer sizes.

`wake_words` selects equivalent activation phrases supported by the trained model (currently “嘟嘟” and “你好嘟嘟”); a different phrase requires a corresponding trained model bundle. “嘟嘟” and “你好嘟嘟” both start the same session, with the same controls and idle behavior. Say either phrase, then speak continuously. Ordinary interruption stops speech without canceling work. “结束对话” returns to standby. Any speech refreshes the 60-second idle timer; active work or playback also keeps the session alive. “取消任务” explicitly requests cancellation. Raw audio is not recorded during normal operation.

Wake acknowledgement uses a locally generated 1.04-second rising chime with a soft tail. Its authored source level is independent of speech PCM; device volume continues to follow `minimum_volume`.

Install a user service after validating the foreground run:

```sh
.venv/bin/python scripts/install_service.py --config "$PWD/config.toml"
.venv/bin/personal-agent start
.venv/bin/personal-agent logs
.venv/bin/personal-agent halt
```

The unit is `personal-agent.service`. `--output /tmp/personal-agent.service` renders without changing the host. The installer does not configure Bluetooth codecs or enable user lingering. A headless boot requires the host to keep that user's services running; the current DELL deployment already has this enabled. Wake keyword, display name and unit identity are separate settings.

## Add hardware

The [hardware onboarding acceptance report](reports/DEVICE_ONBOARDING.html) records the actual microphone/speaker, acoustic, Codex tool and service-restart checks.

All physical endpoints use `devices.<logical_id>`, with `adapter`, `kind`, `label`, optional `location`/`enabled`/`max_age_seconds`/`physical_id`, and an adapter-owned `connection` table. Old top-level microphone/speaker maps are removed. The main voice input/output are fixed logical IDs (`input_device` and `default_output`), frozen for the process lifetime. Change instance configuration and restart to replace them; previous room selections are ignored. MCP/CLI/voice cannot switch the main channel. Inventory `voice_role` and `voice_binding_locked` distinguish these bindings from extra tool devices. At most 32 devices are allowed. The shared directory reports capabilities, timestamped availability and freshness; it never substitutes another device or exposes connection secrets to AI. `available` reports the adapter's probe result: for PipeWire it means a hardware node is present, while for go2rtc it means the gateway can list the configured stream. It does not by itself prove physical sound/capture. `capturing` and `route_ready` describe active audio use separately; gateway video reports `capture_verified` for successful acquisition in the current observed generation.

For another PipeWire microphone/speaker, add its device table and stable identity, choose input/output if needed, restart, then inspect it. Unknown/duplicate/ambiguous identities fail rather than select the first device. The [PipeWire integration guide](src/assistant_runtime/plugins/pipewire/README.md) contains the supported connection schema and repeatable real-device acceptance command.

For another protocol, add one trusted adapter factory via `local_plugins`/`plugins`. Its startup registers each configured device with `host.devices.register(owner, id, capabilities, async_probe)`; updates use `DeviceObservation` and `host.devices.observe`. The shared `DevicesPlugin` owns `DeviceQuery` dispatch by logical ID. Do not register another `DeviceQuery` handler in an individual adapter. The existing device MCP's `list_devices` / `inspect_device` tools and CLI use the same runtime handler. Native voice receives the enabled inventory on session startup and delegates current-state queries to Codex. No observation activates a conversation or becomes long-term memory automatically.

Read-only state integrations (including future sensor snapshots) reuse this directory/query/tool path. A new actuator action still needs a narrowly defined capability and tool contract. AirPlay/RAOP is now implemented by the optional [AirPlay adapter](src/assistant_runtime/plugins/airplay/README.md). Enable the airplay extra/plugin and configure the receiver identity and UDP ports; inspect it through device MCP without changing the main voice route. To bind it as the main voice output, set `default_output` and restart. Audio inputs/outputs register independent stream providers with explicit sharing rules. The main voice streams use those same resource registries; a tool can target a main or additional device without changing the binding. A busy exclusive device preserves its existing stream. New speech/listening/music features still need their own bounded capability and tool.

`set_audio_volume` requires `device_id` and `percent`; it affects that speaker, while `stop_playback` and `end_voice_conversation` control the main session. Existing media, camera and volume requests have independent operation IDs. Use the operation tools to query/cancel only that work; `devices operations <id>` and `devices cancel-operation <id> --operation-id <operation-id>` use the same handlers. Query receipts before retrying an uncertain action: identities survive restart and duplicate IDs never execute twice. Directory results separate hardware `capabilities`, implemented `tool_operations` and `media_streams`.

Video inputs use the optional [go2rtc adapter](src/assistant_runtime/plugins/go2rtc/README.md). Enable the camera extra and go2rtc plugin, then configure each camera with its gateway URL and stable stream name. USB paths, formats and network camera protocols belong to go2rtc; the assistant only requests fresh JPEGs through the shared `VideoCapture` capability. `devices snapshot <id>` captures a frame; `--output state/preview.jpg` explicitly saves it. The `device` MCP's `capture_image` tool receives native image content for user-requested visual questions. Video stays separate from microphone selection, generic events and long-term memory. No continuous capture or automatic visual trigger is enabled.

HomePod temperature/humidity uses the optional [Apple Home adapter](src/assistant_runtime/plugins/apple_home/README.md), separate from AirPlay playback. Enable `apple_home`, declare a sensor endpoint, associate it with the speaker using `physical_id`, and configure the once-only paired refresh bridge/Home automation. Sensor `readings` include separate validity, source time, unit and age per metric; expired or calibrating data is never a current value. This source neither wakes the assistant nor captures memory. Existing CLI and device MCP queries discover it without new HomePod-specific tools.

## Optional camera gateway (go2rtc)

`docker-compose.yml` runs only the official go2rtc 1.9.14 camera gateway. It uses Linux host networking, with HTTP on `0.0.0.0:8553`, RTSP on `0.0.0.0:8554`, and WebRTC TCP/UDP on `0.0.0.0:8555`. This gateway has its own lifecycle; the assistant continues to run through its existing user service. A gateway alone does not register a camera capability with the assistant.

See the [go2rtc Docker device onboarding guide](deploy/go2rtc.md) for private configuration, USB mappings and permissions, adding multiple cameras, unplug/replug recovery, Linux host migration and acceptance checks. Read it before adding a camera or changing the gateway deployment. The current installation's Xiaomi authorization and access instructions live in private, ignored `state/go2rtc/`.

```sh
docker compose up -d
docker compose ps
docker compose logs --tail 100 go2rtc
docker compose restart go2rtc
docker compose stop go2rtc
```

Open `http://SERVER_IP:8553/`. The configured access credential is in private `state/go2rtc/access.txt`. `restart: unless-stopped` provides persistence across Docker startup; a deliberate stop stays stopped. The health check reports API availability, not successful video capture.

## Memory

Mem0 1.0.11 runs locally in a worker using the same environment. Prepare its multilingual embedding cache with `scripts/prepare_memory.py --config config.toml`. Embeddings stay under `models/memory-embedding/`; local vectors and Mem0 history live under `state/mem0/`. Mem0 owns automatic extraction and dedup/update decisions. Its model provider calls the existing official Codex App Server through internal stdio callbacks using ChatGPT login; no API key, HTTP model proxy or hosted Mem0 account is required. Automatic jobs use original user evidence, run while idle and are preempted by wake. Failures are retried with bounded backoff.

```sh
.venv/bin/personal-agent memory search '{"query":"饮品偏好","scope":"user"}'
.venv/bin/personal-agent memory remember '{"title":"饮品偏好","text":"喜欢乌龙茶，不加糖。","scope":"user"}'
.venv/bin/personal-agent memory read '{"id":"ID_FROM_RESULT"}'
.venv/bin/personal-agent memory forget '{"id":"ID_FROM_RESULT"}'
.venv/bin/personal-agent memory export > memory-export.json
```

A correction uses the returned `id` and current `revision` with `remember`. Import accepts `{"records":[...]}`; the export has this shape. Treat exports as private data and place them outside version control. Forgetting long-term memory does not erase conversation history. Search and context exclude deleted IDs immediately, including when backend deletion must be retried.

## Develop and recover

```sh
.venv/bin/python scripts/check.py
```

This runs formatting, lint, kernel typing and unit contracts, without changing the installed service. Reusable real-device tests are explicit scripts and store necessary private evidence under ignored `state/acceptance/`. Never run two voice instances against the same audio devices. See `ARCHITECTURE.md` for module boundaries and `AGENTS.md` for editing rules.

The pre-refactor Git tag is `baseline-before-refactor`. Code rollback must preserve the newest task records: stop the service, check out the desired commit, restore its dependencies/service definition and restart against the existing database. Do not restore an old task database after executed actions. New tables are additive. The private pre-cutover config/unit snapshots are under `state/backups/`; they are not in Git.


## Maintenance scripts and local artifacts

`scripts/` contains reusable installation, preparation and acceptance entries:

- `check.py`: formatting, lint, kernel typing and runtime contract tests.
- `install_service.py`: render/install the user systemd unit.
- `check_audio_input.py`: independent input/wake benchmark; stop the service first. See the [audio acceptance guide](src/assistant_runtime/plugins/pipewire/README.md#independent-input-and-wake-benchmark).
- `download_models.py`: validate/install the current FSMN bundle and prepare VAD.
- `prepare_memory.py`: initialize local Mem0 and its embedding model, without inference or user facts.
- `verify_memory.py`: repeat real Mem0/ChatGPT-subscription acceptance with isolated synthetic facts and actual MCP queries.
- `verify_device_onboarding.py`: explicit microphone/speaker acoustic and Codex capability acceptance.
- `verify_video_onboarding.py`: explicit camera capture, restart and optionally authorized Codex image acceptance.
- `verify_environment.py`: isolated synthetic sensor source or real Apple Home readback, stdio MCP and optional Codex query/restart acceptance.

One-off protocol probes, old model-comparison/training experiments, fixture-specific room tests and report generators have been removed. Their source remains available in Git history. Runtime and reusable hardware tests do not depend on them. Reports are produced only on an explicit user request.

Generated artifacts remain outside version control:

- `reports/`: existing HTML/Markdown reports; available through the workspace static server on fixed port 8045.
- `state/`: private runtime state, necessary acceptance evidence, old experiment results and explicit recordings/screenshots.
- `models/`: deployed model bundles and existing checkpoints.

The selected FSMN V2 bundle and its production keyword adapter remain the current wake detector. Removing experiment code does not alter the deployed model or its checksum.

## Background tasks and working files

Enable the `tasks` plugin for independent scheduled, event-triggered and long-running work. See [task configuration, MCP/CLI and recovery](src/assistant_runtime/plugins/tasks/README.md). Background model and reasoning settings live in `[tasks.execution]`, defaulting to `auto`; `[work_files]` configures shared temporary-file retention. Web has a Task panel, while results remain in the shared conversation.

The [notification plugin](src/assistant_runtime/plugins/notifications/README.md) adds a
persistent inbox, composer unread badge, independent channel receipts and quiet-hour-aware
optional voice delivery. Add `notifications` to an explicit plugin list alongside `tasks`.
Monitoring tasks can set `notify_completion=false` and publish only new source events
through the separate notifications MCP. Phone push is not installed.

## Third-party MCP management

Enable `integrations` and install its optional extra in the existing environment.
The assistant can configure instance-owned stdio/Streamable HTTP MCPs, choose their
foreground/background scope and tool policy, verify actual connections, and refresh
the same foreground conversation at idle. Background runs retain their tool snapshot;
authorized installation tasks use the `mcp_management` resource. Non-secret definitions
in `.agents/mcp/servers.json` belong in Git; credentials and runtime state stay private.
Shared Codex configuration is untouched. See the
[integration guide](src/assistant_runtime/plugins/integrations/README.md) for schemas,
CLI/tools, OAuth and reproducible hardware-free acceptance.
