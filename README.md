# Personal Agent

A local, reactive personal assistant for Linux. The current brain is Codex with the existing ChatGPT subscription: native voice, task execution and persistent task context. Local plugins supply microphones, speakers, wake detection and long-term memory. The application is not named after its wake word or deployment machine.

See [architecture](ARCHITECTURE.md), [requirements](REQUIREMENTS.md) and [validation](VALIDATION.md).

## Install

Python 3.12 and `uv` are required. All application and memory dependencies share one `.venv`.

```sh
uv sync --locked --extra voice --extra memory --extra dev
.venv/bin/playwright install chromium
.venv/bin/python scripts/download_models.py
cp config.example.toml config.toml
```

The example starts a hardware-free management host. For speech configure `profile="voice"`, an absolute instance `root`, `workspace`, model location, input identity and output map. Configure Codex's existing subscription login normally. No separate paid API key is used. Linux voice also needs `pactl` and `pacat`; Bluetooth outputs need BlueZ. The camera is not activated by the application.

```toml
profile = "voice"
root = "/path/to/instance"
workspace = "/path/to/workspace"
model_dir = "/path/to/wake-models"
codex_binary = "/path/to/codex"
assistant_name = "个人助理"
wake_word = "嘟嘟"
source_match = "stable microphone identity"
default_room = "office"
minimum_volume = 80
[speakers]
office = "AA:BB:CC:DD:EE:FF" # or an exact PipeWire sink name
[room_labels]
office = "书房"
```

`minimum_volume` raises readable channels below the floor, preserves louder channels and skips unknown volume. There is no extra gain/limiter. Output loss does not route audio to another room. Native audio uses Chromium WebRTC and optional WebRTC AEC3; the wake model receives the original mic stream.

For a core-only, hardware-free installation use `uv sync --locked`; `profile="headless"` runs management/explicit-trigger validation without voice, models, Codex or memory. Missing capabilities are reported rather than simulated as completed tasks.

## Run and maintain

```sh
export PERSONAL_AGENT_CONFIG="$PWD/config.toml"
.venv/bin/personal-agent run
.venv/bin/personal-agent status
.venv/bin/personal-agent doctor
.venv/bin/personal-agent gesture wave
.venv/bin/personal-agent stop
.venv/bin/personal-agent sleep
.venv/bin/personal-agent cancel
.venv/bin/personal-agent room office
.venv/bin/personal-agent volume 80
```

Say the configured wake word, then speak continuously. Ordinary interruption stops speech without canceling work. “结束对话” returns to standby. Any speech refreshes the 60-second idle timer; active work or playback also keeps the session alive. “取消任务” explicitly requests cancellation. Raw audio is not recorded during normal operation.

Install a user service after validating the foreground run:

```sh
.venv/bin/python scripts/install_service.py --config "$PWD/config.toml"
.venv/bin/personal-agent start
.venv/bin/personal-agent logs
.venv/bin/personal-agent halt
```

The unit is `personal-agent.service`. `--output /tmp/personal-agent.service` renders without changing the host. The installer does not configure Bluetooth codecs or enable user lingering. A headless boot requires the host to keep that user's services running; the current DELL deployment already has this enabled. Wake keyword, display name and unit identity are separate settings.

## Memory

Basic Memory runs locally in a child process using the same environment. First use downloads its multilingual embedding model; prepare it before daily use with `scripts/prepare_memory.py --config config.toml`. It stores Markdown and local indexes below `state/memory/`. Default automatic capture proposes sourced facts/preferences after dialogue or completed tasks, only while idle. It can fail or be interrupted without interrupting speech.

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

This runs formatting, lint, kernel typing and unit contracts, without changing the installed service. Real backend/device tests are explicit scripts and store private reports under ignored `state/acceptance/`. Never run two voice instances against the same audio devices. See `ARCHITECTURE.md` for module boundaries and `AGENTS.md` for editing rules.

The pre-refactor Git tag is `baseline-before-refactor`. Code rollback must preserve the newest task records: stop the service, check out the desired commit, restore its dependencies/service definition and restart against the existing database. Do not restore an old task database after executed actions. New tables are additive. The private pre-cutover config/unit snapshots are under `state/backups/`; they are not in Git.
