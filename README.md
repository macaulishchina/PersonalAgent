# Personal Agent

A local, reactive personal assistant for Linux. The current brain is Codex with the existing ChatGPT subscription: native voice, task execution and persistent task context. Local plugins supply microphones, speakers, wake detection and long-term memory. The application is not named after its wake word or deployment machine.

See [architecture](ARCHITECTURE.md), [requirements](REQUIREMENTS.md), [validation](VALIDATION.md), [wake-word evaluation](reports/WAKE_EVALUATION.md), the [ASR candidate CPU comparison](reports/ASR_CANDIDATE_COMPARISON.html) ([full text](reports/ASR_CANDIDATE_COMPARISON.md)), the [human-voice comparison](reports/WAKE_HUMAN_COMPARISON.html) ([full text](reports/WAKE_HUMAN_COMPARISON.md)), and the [synthetic-voice comparison](reports/WAKE_MODEL_COMPARISON.html) ([full text](reports/WAKE_MODEL_COMPARISON.md)).

## Install

Python 3.12 and `uv` are required. All application and memory dependencies share one `.venv`.

```sh
uv sync --locked --extra voice --extra memory --extra dev
.venv/bin/playwright install chromium
.venv/bin/python scripts/download_models.py --wake-bundle /path/to/fsmn-v2
cp config.example.toml config.toml
```

The example starts a hardware-free management host. For speech configure `profile="voice"`, an absolute instance `root`, `workspace`, model location, input identity and output map. Configure Codex's existing subscription login normally. No separate paid API key is used. Linux voice also needs `pactl` and `pacat`; Bluetooth outputs need BlueZ. The camera is not activated by the application.

```toml
profile = "voice"
root = "/path/to/instance"
workspace = "/path/to/workspace"
model_dir = "/path/to/wake-models"
codex_binary = "/path/to/codex"
codex_model = "gpt-6.1-sol"
codex_reasoning_effort = "high"
realtime_voice = "cove"
assistant_name = "个人助理"
wake_words = ["嘟嘟", "你好嘟嘟"]
source_match = "stable microphone identity"
default_room = "office"
minimum_volume = 80
[speakers]
office = "AA:BB:CC:DD:EE:FF" # or an exact PipeWire sink name
[room_labels]
office = "书房"
```

`codex_model` and `codex_reasoning_effort` explicitly select this project's background task model and reasoning effort. They apply to new/resumed foreground threads, task turns and silent memory maintenance; they do not edit the user's global Codex settings. Use model/effort values supported by the installed Codex and account. `realtime_voice` selects the native voice timbre; these task settings do not select the separate realtime voice model. After editing `config.toml`, run `personal-agent --config config.toml restart`. `status` includes provider-returned model/effort, requested model/effort and configured voice; an unavailable returned field is null rather than guessed.

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


## Lightweight wake-model research

The [lightweight wake report](reports/LIGHT_WAKE_COMPARISON.html) compares official FSMN weights, a synthetic-only trained WeKWS MDTC-small, and a fine-tuned FSMN using the existing confirmed human recordings. It includes native ONNX CPU/RSS measurements, streaming threshold calibration, false wakes, chunk sizes, and reproducible evidence under `state/light-kws-20261013/`. Historical comparison scripts stay offline; the running keyword plugin now loads FSMN V2 exclusively.

Open the report through the workspace Docker static server at `http://110.0.0.82:8045/dell-voice-assistant/reports/LIGHT_WAKE_COMPARISON.html`. Training dependencies belong to the optional `benchmark` extra in the same project environment. No additional environment, audio playback, or paid service is used.

## Local generated artifacts

- `reports/`: generated HTML/Markdown evaluation reports (ignored by Git). Browse `http://110.0.0.82:8045/dell-voice-assistant/reports/` using the workspace static server on fixed port 8045.
- `state/`: private runtime state and experiment results; each benchmark keeps recordings, manifests, CSV/JSON, logs and screenshots in its existing experiment directory. Temporary correspondence belongs in `state/notes/`.
- `models/`: downloaded checkpoints, trained models and training feature caches (ignored by Git).
- `scripts/` and `scripts/templates/`: reproducible benchmark/report source code, kept in version control. Report commands default to `reports/`; `--output` can override where supported.

The workspace static container serves the workspace root on port 8045; project reports retain their `dell-voice-assistant/reports/` URL prefix. Generated artifacts are not part of the source repository.

The [second FSMN refinement report](reports/FSMN_REFINEMENT.html) compares three synthetic-only training recipes, including a keyword-focused CTC objective, against the previous FSMN at 80 ms. Run `.venv/bin/python -m scripts.run_fsmn_refinement --resume` to resume the recorded local experiment, then generate the report with `.venv/bin/python -m scripts.report_fsmn_refinement`. The input recipe, frozen selection and matched evidence are under ignored `state/fsmn-v2-20261013/`; trained checkpoints stay in `models/fsmn-v2-20261013/`. The selected focused V2 model is now the production keyword plugin. Run `.venv/bin/python -m scripts.verify_fsmn_runtime` for actual model replay/parity/bounded-buffer acceptance. Evidence is saved under `state/acceptance/fsmn-v2/`.
