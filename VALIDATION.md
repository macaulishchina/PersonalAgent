# Refactor validation — 2026-10-12 (Asia/Shanghai)

This report describes the current plugin-runtime implementation. The original prototype evidence remains in Git tag `baseline-before-refactor`. Detailed reports, device identifiers and recorded test samples stay in ignored `state/acceptance/`.

## Automated gate

`.venv/bin/python scripts/check.py` passes formatting, Ruff, kernel type checks and **98 unit tests**. It includes all 24 original behaviors plus plugin lifecycle/cleanup, bounded subscribers, request deadlines, stale generation rejection, explicit gesture dispatch, reconnect responsiveness, stable session mapping, and memory provenance/revision/tombstone/retry tests, and current wake, audio output and camera contracts.

The exact dependencies are in `uv.lock`; `uv sync --locked --extra voice --extra memory --extra dev` was exercised on this machine. Sherpa's native core is explicitly pinned because its wheel metadata and the universal dependency resolution differed. There is one `.venv`; the temporary second environment was removed.

## Integration evidence

| Check | Observed result |
| --- | --- |
| Hardware-free host | Started with system Python and only `PYTHONPATH=src`, without optional dependencies |
| Simulated gesture | Explicit `wave` dispatched the common Trigger contract |
| Existing subscription | Project App Server authenticated as ChatGPT; no paid API key |
| Native voice loop | Wake sample, question, follow-up, real file creation, stop sample and standby gating passed |
| Cold question | 5.146 s from end of injected question to first detected model voice |
| Warm question | 1.610 s, same measurement |
| Local stop | 0.00246 s to stop/clear local playback; this is not an acoustic speaker-latency measurement |
| Local playback queue | Successful native run retained bounded queues and PCM timing; detailed metrics in private report |
| Basic Memory | Actual local Markdown write/read, Chinese hybrid recall, backend restart, correction and forget passed |
| Codex curation | Existing subscription generated sourced structured Chinese memory candidates in a separate silent context |
| MCP SDK facade | Actual stdio tool calls used the same memory handlers as local management; unapproved `memory.import` and `device.shutdown` actions were rejected before reaching the host |
| Codex → memory tool | Real Codex task invoked the local memory search tool successfully after explicit user authorization; only this project’s `memory` and `device` tools are preapproved, without global permission changes |
| Service migration | `personal-agent.service` enabled/running; old service stopped and disabled |
| Service restart | Same provider thread; task rows unchanged, no replay; temporary memory survived restart then was deleted |
| Live microphone | Capture recent, living-room output ready, volume observed at 80, standby, no active task |

Reports: `refactor-native.json`, `refactor-memory.json`, `refactor-deployment.json`, `refactor-smoke.json`, `refactor-final-deployment.json`, `refactor-checks.log` under `state/acceptance/`.

The first native run created the test file and answered questions but missed the injected stop transcript. A second run, allowing the preceding task-result voice turn to finish, passed the entire sequence. This does not establish perfect interruption recognition or eliminate network/room effects. The native service still has no per-answer audio ID; its within-connection late-audio suppression uses a quiet-boundary heuristic.

## Limits and remaining acceptance

- No physical camera/gesture recognizer was added. The new extension path is demonstrated with an explicit simulated gesture.
- No host reboot or Windows shutdown was performed during this refactor. User systemd restart and independence from the invoking terminal were verified; whole-machine reboot remains an operational acceptance item.
- Real audio acceptance ran on this Linux x86-64 deployment, not an ARM or distribution matrix.
- Memory provenance guards validate source identity and exact evidence; they do not guarantee a model never misinterprets a fact. Semantic indexes can become searchable shortly after the durable Markdown write.
- No actual network outage was induced in the production conversation. Nonblocking reconnection and no-replay state are covered by regression tests and service restart checks.
- Long-term memory forgetting does not erase provider chat history or local transcripts.

## Wake-word follow-up

See [wake evaluation](reports/WAKE_EVALUATION.md) for the 103-condition production-KWS baseline, live timing observation and independently reproduced chunk-phase misses. This evaluation did not change the deployed detector or service configuration.

## Equivalent wake phrases

The deployed `wake_words=["嘟嘟","你好嘟嘟"]` uses one KWS stream and one activation path. Tests cover both phrase encodings, one activation followed by audio streaming, equivalent control commands, and invalid configuration. Real local KWS detected both a short historical fixture and an explicitly constructed greeting-plus-wake fixture (not a natural human recording); the long fixture was also detected with the long phrase configured alone. All 103 original replay conditions retained the same miss/extra-hit outcomes. Private reports: `state/wake-benchmark/dual-wake-model.json`, `dual-baseline.json`, `dual-deployment.json`, and `state/acceptance/dual-wake-checks.log`. The previous chunk-phase misses remain an optimization item.

## Expanded wake comparison

[Full comparison](reports/WAKE_MODEL_COMPARISON.md): seven precision/chunk configurations from two model families, four local synthetic voices, 164 main waveforms, 48 new mic/monitor recordings, and a second-generator cross-check. Completed 4550 condition replays. No human recordings were claimed; the unavailable study speaker was excluded. All inference and synthesis were local; production KWS was kept unchanged and the user service recovered. Private evidence is in `state/wake-comparison/` and the development gate in `state/acceptance/wake-suite-checks.log`.

## Confirmed human wake comparison

[Human visual report](reports/WAKE_HUMAN_COMPARISON.html) ([full text](reports/WAKE_HUMAN_COMPARISON.md)): 14 wake utterances and four negative utterances from one owner and one raw camera microphone location, confirmed after complete playback. Four unclear initial negatives were excluded and replaced with confirmed retries. Seven unchanged model weight/decoder configurations each ran 287 conditions (2009 total): complete 16-position phase scans, two amplitude levels, two white-noise levels, repeated-negative and mixed continuous streams, and silence. Each result is checked against the frozen manifest and the previous model hashes. Derived variants and the ten-minute repetition of four negative phrases are explicitly separated from independent human recordings. Cue tones remain; word ends were not annotated, so no word-end or audible wake latency is claimed. Production model/configuration was not changed. Private recordings, JSON/CSV and browser verification live in `state/wake-human-comparison-20261013/`.

## CPU ASR candidates

[Visual report](reports/ASR_CANDIDATE_COMPARISON.html) ([full text](reports/ASR_CANDIDATE_COMPARISON.md)): pinned official Paraformer Chinese streaming and native Transformers Qwen3-ASR 0.6B checkpoints, CPU float32, tested sequentially in the same project environment. Ten scoring profiles replay the same 18 confirmed human recordings (180 scored decodes): two/six Torch threads, 300/600 ms Paraformer chunks, Qwen with/without fixed assistant-name context, and the current KWS with one/two ONNX threads. Strict configured text matching never accepts homophones silently. Full transcripts, prompt-reference CER, inference CPU seconds/wall time/RSS, warm residency, true-time silence and spoken-input pacing are recorded separately. Qwen's gated silence runs Silero with zero ASR calls; it is not native streaming. Current KWS two-thread silence CPU was independently reproduced; one-thread diagnosis retained 9/14 original hits. Short residency tests are not long-term leak validation. No runtime model/configuration changed. Private raw results, wave hashes and CPU/HTML validation live in `state/asr-candidates-20261013/`.

## Wake acknowledgement sound

Replaced the normal 120 ms sine cue with an authored 1.04-second ascending chime. Its active-waveform RMS is 8.71 dB above the old cue, with peak 25558/32767 and zero clipped samples. A regression checks that the longer cue replaces stale speech, fits the bounded playback queue and remains stoppable; the full 61-test development gate passed. Current KWS detected no wake word in the digital cue plus trailing silence. After an idle service restart, one preview used the production `StreamPlayer.cue()` and the existing living-room PipeWire playback bus: all 55680 PCM bytes were written, physical sink volume was unchanged, wake count stayed zero, and microphone capture remained ready. This validates the output path; perceived loudness and sound preference remain user judgement. Private waveforms and evidence are under `state/acceptance/wake-cue/`.

The user then requested a fuller timbre: note fundamentals were lowered one octave, the 146.8 Hz body was strengthened, attacks softened and bright upper partials reduced. Duration and peak stayed at 1.04 seconds and 25558; zero clipped samples and no digital KWS wake detection. The 61-test gate passed again, and the deployed living-room preview completed with unchanged volume and no self-wake. Updated sample and evidence: `warm.wav`, `warm-check.json`, `warm-preview.json`, and `warm-checks.log` in the same private folder.


## Lightweight dedicated wake models

[Visual report](reports/LIGHT_WAKE_COMPARISON.html): official FSMN base and Xiaoyun checkpoints, plus two local custom models. A new 720-clip / 20-synthetic-voice corpus is split by voice into 432 training, 144 validation and 144 test clips. Existing human recordings and previous synthetic voices never enter training or checkpoint selection. WeKWS MDTC-small has 35,073 parameters and failed the original human cohort (0/14); fine-tuned FSMN has 756,133 parameters, with 11/14 at the initial 0.5 threshold and 13/14 after independent streaming validation selected 0.265. The more sensitive threshold increases synthetic false wakes; the report preserves both operating points and the failed model.

Four full/complementary native profiles produced 1922 condition replays, including the unchanged human corpus, phase/amplitude/noise variants, synthetic negatives and repeated continuous streams. Twenty/eighty-millisecond chunks are measured separately. The current results reuse the previously inspected human cohort and are not a new blind test. Short residency runs and repeated-negative streams do not establish all-day reliability.

Export checks cover Torch/ONNX MDTC probability parity and exact cached/chunked output parity. FSMN finite streaming windows match full ONNX logits on 532 stable frames from six synthetic validation clips. Three-minute/one-minute silence pacing and separate short quiet/speech replications record process CPU and RSS. Experimental inference imports no Torch. No production model, wake words, volume, systemd unit or audio device configuration was changed; service status remained standby with plugins ready. The existing Python report server remains on port 8765. Private models, source recording hashes, training logs, CSV/JSON evidence and browser checks stay under ignored `models/` and `state/`.

## Synthetic-only FSMN refinement (2026-10-13)

[Second-round visual report](reports/FSMN_REFINEMENT.html): 448 newly synthesized recordings plus the original 720 sources, each with a deterministic acoustic variant. The voice-disjoint splits contain 1440 training, 448 validation and 448 test clips; variants are correlated, not independent speakers. All sources were explicitly synthetic, hashed and checked for cross-split voice/waveform leakage. No human audio entered training or selection.

Three recipes completed: continued full-text CTC (18 epochs, selected epoch 1), official-base full-text CTC (early stop at 6, selected 1), and keyword-focused CTC (early stop at 11, selected 6). The focused objective preserves the two repeated wake syllables and groups unrelated text into background without changing the exported network or upstream decoder. Tests check probability normalization, gradient flow and repeated-syllable targets. Streaming calibration balances FNR and FPR on synthetic validation; a zero-false operating point alone rejected too many wakes. Selection was frozen before human evaluation: focused FSMN, threshold 0.485, 80 ms input. All three streaming/full-output parity checks passed.

Both V1 and the selected candidate replayed the same 851 conditions (1702 total). Human originals improved from 13/14 to 14/14 with 0/4 human negative false wakes for both; expanded synthetic positives improved from 116/128 to 123/128 and synthetic negative false clips fell from 11/320 to 9/320. Original synthetic negatives remain 0/76; repeated-negative streams showed no false events. Historical recordings improved from 1/2 to 2/2; artificial 15 dB white noise from 5/14 to 10/14, while 5 dB remained 0/14. The same repeatedly inspected human cohort is not a new blind test. Near-word and homophone false wakes still occur; no all-day false-alarm or new live-device success is claimed.

The 70-test development gate passed. Evidence, paired CSV, training logs, model hashes and independent 60-second quiet/spoken resource measurements are under `state/fsmn-v2-20261013/`. No production model or device setting was changed.


## FSMN V2 production deployment — 2026-10-13

The keyword plugin now runs only FSMN V2 (model SHA256 `2840475733854f8a751256c31c5cfebb78fb575af3d4117e45d69cac33b44bf9`), threshold 0.485, 80ms input, one ONNX thread. Removed the old production Zipformer code, downloaded model directory and generated keyword file. Research recordings/reports remain private and ignored. No automatic fallback is installed.

- Actual production implementation replays all 18 confirmed recordings: 14/14 positive, 0/4 negative false wakes. Three caller partitions (20ms, 137 samples, entire file) produce the same detections as 80ms. Stable probabilities equal the research implementation (maximum error < 1e-6).
- Accelerated 600-second silent input completes in 32.65 wall seconds. Feature storage stays at no more than 136 frames, posterior history at 100 frames; RSS before/after remains 115.59 MiB in the parity process (two loaded models, not the assistant RSS). A following human wake still fires. Per-step p50 4.26ms / p95 4.59ms is compute time, not word-end wake latency.
- 72 development tests passed, including reset invalidation of in-flight inference and standby privacy. Inference is off the asyncio loop; no Torch/FunASR runtime import or source rewriting. MIT upstream CTC search and license are vendored.
- Restarted the installed user service; status confirms FSMN V2's exact hash, microphone capture, audio route and all plugins ready. Default output remains living room, volume 80.
- Evidence: `state/acceptance/fsmn-v2/{runtime-validation,deployment}.json`. Replay does not claim a new live-room conversation test or 24-hour stability test. Existing synthetic near-word false positives remain documented in the report.


## Pre-commit review — project configuration and packaging

- Project TOML explicitly controls Codex model and reasoning effort for new/resumed threads, direct task turns and memory maintenance. Deployed App Server returned `gpt-6.1-sol` / `high`; configured realtime voice is `cove`. Evidence: `state/acceptance/project-config/deployment.json`.
- At that review, experimental benchmark accounting imports were corrected and covered by a subprocess test. Those experiment helpers and their tests were later removed during one-off script cleanup.
- Full gate: 76 tests pass, Ruff formatting/lint and kernel Mypy pass; `uv lock --check --offline` passes. `uv build --offline --wheel` succeeds and includes the FSMN runtime, authored cue, and upstream MIT license.
- Model weights, recordings, generated reports, local TOML, runtime state and build outputs remain ignored by Git.


## Standard hardware onboarding — 2026-10-13

The microphone and existing speakers now use `devices.<logical_id>` configuration, adapter registration into `DeviceDirectory`, typed `DeviceQuery` calls and timestamped `DeviceObservation` snapshots. Old top-level source/speaker configuration maps are removed. PipeWire validates its own connections; CLI and Codex's read-only device list/inspect share the same capability owner. Passive probes do not send audio or activate voice.

- 88 development tests pass, including duplicate/foreign/disabled registration, missing registration, stale/unknown/error observations, detached snapshots, blocked probes, ambiguous physical identities and multiple outputs sharing one room name.
- Real integration passes: mic registration and actual capture; authored speaker chime independently detected in the physical microphone (normalized correlation 0.777, 3.62-second capture). No raw audio was saved.
- Deliberate Bluetooth disconnection yields unavailable; microphone stays present; reconnection restores the intended output without room substitution.
- An actual isolated Codex thread completes all three MCP calls: list devices, inspect living_microphone, inspect living. Queries traverse the real management socket, shared runtime capability handler and adapter. Temporary state/provider context prevents test conversation/memory contamination.
- The installed service is restored and explicitly restarted. Devices register again, mic capture is recent, audio is ready, FSMN V2 remains selected, provider session is unchanged and existing task rows are unchanged.
- Evidence: `state/acceptance/device-onboarding/{real-devices,production-restart}.json`, generated `reports/DEVICE_ONBOARDING.html`. Live USB unplug/replug, machine reboot and 24-hour stability are not part of this acceptance.

- Post-deployment resource/query smoke: 10.17 seconds, host-main CPU 5.03% of six-core machine capacity, RSS 139.53 → 139.73 MiB. Repeated directory inspections leave wake count at 0 and mode at standby. This includes local voice/audio processing, excludes provider/memory children and is not a long-duration stability result. Evidence: `resource-smoke.json` and `final-status.json` in the onboarding acceptance folder.


## HomePod standard output integration — 2026-10-13

- Added the optional AirPlay family under the existing device directory and PCM output provider registry, using public pyatv 0.18.0 APIs plus project-local imageio-ffmpeg 0.6.0. The protocol/receiver/UDP settings were taken from the earlier homepod-assistant verification; no firewall or receiver name changes were made.
- Receiver discovery confirms the configured stable RAOP identity and the original network name “家庭影院”. AI list/inspect exposes HomePod's playback/select/volume/stop abilities without exposing private connection data.
- The real standard onboarding script selected HomePod in isolated state, played the authored chime and the previous short Chinese “这是一条语音测试” fixture through the shared player. Actual write count 183360 bytes; physical mic chime correlation 0.701. Speech fixture length 2.659 seconds. Mic capture and audio reference remained local; raw PCM was not saved.
- A real Codex task completed list, microphone inspect and HomePod inspect via MCP/socket/shared capability routing. Closing playback left no FFmpeg process and the subsequent tool report showed the receiver stream closed.
- Initial integration attempts found the encoder executable missing; this was fixed by a declared project dependency rather than installing a second environment or modifying the host. The online receiver alone was not counted as success.
- 94 development tests pass, including provider ownership, external routing with the same microphone, unknown volume preservation, reference cleanup, queue bounds/old audio discard and specific output labels sharing a room.
- Evidence: `state/acceptance/airplay-homepod/real-devices.json`, `state/homepod-onboarding-run.log`. The measured chime location is 7.22 seconds from mic capture start, including preroll/stream setup/receiver buffering; it is not word-end assistant response latency. Fast acoustic interruption, long-duration stability and HomePod echo cancellation experience have not been separately established.

## go2rtc video gateway onboarding

- The installed `living_camera` keeps its logical ID, label, location and physical association, and now uses the `go2rtc` adapter. USB identity/format and acquisition belong to the Docker gateway; the assistant requests snapshots through the unchanged video capability, CLI and device MCP. The old direct V4L2 factory is no longer registered; its inactive source files are retained after automatic approval rejected their deletion.
- Repeated independently decoded, nonblank 1280×720 JPEGs passed through the installed capability socket, with successful-run acquisition times around 0.35–0.48 seconds. Actual Codex list/inspect/capture calls returned native image content; its scene description matched the private preview. The first model attempt returned `serverOverloaded`; the later unchanged-model retry passed.
- Stopping the real Docker gateway made inspect report unavailable and capture return an error rather than earlier pixels. Starting it restored fresh capture. The microphone, selected output and logical session remained intact. A separate assistant service restart preserved the session/output and restored real capture. Physical USB hotplug and whole-machine reboot were not performed.
- A ten-second isolated adapter idle sample used 0.0031 CPU seconds (0.03% of one core), with 40,288 KiB process RSS and a 4 KiB RSS change. This includes the probe process and imported dependencies, not the whole assistant or gateway. Metadata-only observation performed zero acquisitions.
- The complete development gate passed 154 tests, including HTTP authentication, redirect rejection, response bounds, invalid JPEGs, source/generation changes, concurrent acquisition, cancellation, timeout, outage/recovery, shared routing, MCP image delivery, reply-image recovery and deeply nested provider metadata.
- Fresh-install configuration was checked in an isolated container with networking disabled: first-stream addition returned HTTP 200 and persisted the source. The inline empty `streams: {}` configuration that caused an unsaved HTTP 400 result is removed from the example. Installed desktop/mobile browsers passed existing-image preview, reload and connection recovery without another capture or model task.
- Private evidence: `state/acceptance/video/real-devices.json` (Codex), `local-recovery.json` (gateway/service recovery), `idle-adapter.json`, `codex-capacity-first.json` and the explicitly saved `preview.jpg`. No HTML report is generated.

## Historical direct V4L2 camera onboarding

- The UGREEN USB video endpoint is registered as `living_camera`, separate from its PipeWire microphone, using an instance-configured stable by-id path.
- Installed `devices snapshot` returned fresh, independently decoded 1280×720 JPEGs in roughly 0.34–0.50 seconds. Microphone capture remained recent and the selected output stayed unchanged.
- Explicitly authorized Codex acceptance actually called `device list`, `inspect` and `snapshot`; its completed MCP result contained image content, and its scene description matched the camera preview. No background visual trigger or recorder is enabled.
- Service restart preserved the foreground logical session and output choice; real capture still worked afterward. Physical USB unplug/replug and whole-machine restart were not performed.
- Full development gate passed 104 tests. Ten camera contracts cover identity/role/configuration, explicit acquisition, unknown/unavailable devices, malformed images, concurrent requests, node/generation changes, bounded output, timeout/cancellation cleanup and MCP image content.
- Local acceptance data and one explicitly saved test JPEG remain ignored under `state/acceptance/v4l2-camera/`; no HTML report is generated. Camera-only 5-second idle sample used 0.014% of one core; this is a short sample, not a whole-host power measurement.

## One-off script cleanup

- Removed 37 experiment/probe/report Python scripts, four report templates and six tests that exclusively exercised those removed experiments. No runtime module imported these scripts.
- Retained the six reusable installation/preparation/check/hardware-acceptance entries listed in README. Removed the orphaned benchmark dependency extra and old edge-tts fixture dependency; existing installed packages and the running service were not altered.
- Existing private reports, recordings, acceptance evidence and trained/deployed models were preserved. Historical test counts above describe their original runs; the current runtime contract suite has 83 tests.

## Mem0 subscription-backed memory

- Mem0 1.0.11 owns native ADD/UPDATE/DELETE/NONE decisions, retrieval and persistence. Model calls go to the official signed-in Codex App Server, using independent project memory model/effort and ephemeral tool-free contexts; API-key accounts and unavailable ordinary subscription usage are rejected. No HTTP model proxy or hosted Mem0 account is used.
- Two complete real subscription rounds passed extraction, repeated paraphrase deduplication, automatic correction, worker restart/recall, assistant-report and temporary/hypothetical-input exclusion, explicit revision correction, forget and stale import/job replay. Per-workspace project isolation and an actual Codex MCP memory search passed. The accepted run used 25 bounded model tasks on gpt-6.1-sol/high. An earlier project-stage model timeout was not counted as success; maintenance error handling was improved before the complete rerun.
- Local native Mem0 contracts cover isolated CRUD, scope filtering, receipt idempotency, worker cancellation/cleanup, source evidence and subscription/API-key/quota guards. The complete current suite has 99 tests, including independent task/memory model settings and the separately requested night quiet feature.
- Independent `memory_model="gpt-6-luna"` / `memory_reasoning_effort="low"` passed another two full synthetic-data subscription rounds (25 memory calls), while foreground tasks remained gpt-6.1-sol/high. The service was restarted with the new settings; all 21 cached facts, logical session, selected output and recent microphone capture were preserved. This is functional validation, not a controlled speed or subscription-cost comparison. Evidence: `state/acceptance/mem0/subscription-acceptance.json`, `independent-model-deployment.json`, `state/memory-luna-acceptance.log`, `state/memory-model-check.log`. The prior Sol/high run is retained as `sol-high-subscription-acceptance.json`.
- Historical cloud re-extraction was rejected by automatic approval review. The implemented migration sends no historical content to a model: 21 original-user-sourced records were copied locally; 12 legacy model-report records remain in the complete private Basic Memory/SQLite backup and were not promoted as facts. Old Basic Memory runtime code and dependency were removed.
- Production local remember/read/forget, temporary-marker removal and service restart persistence passed. The foreground logical session, selected output and task rows were preserved; microphone capture remains recent. This verifies the memory capability path, not every natural voice memory dialogue.
- Necessary private evidence is under `state/acceptance/mem0/` (subscription-acceptance.json, migration.json, deployment.json) and `state/mem0-check.log`. No HTML report is produced.

## 语音输入与唤醒优化交接 — 2026-10-18

**状态：语音唤醒优化暂缓，先处理模型调度。用户随后要求审查、收敛测试代码并恢复助手自启与运行；这一新要求取代此前保持服务停止的操作约束。** 本节是恢复入口，以下是本次实测与已对齐的约束，不代表已经部署新的唤醒方案。

### 操作约束

- 独立基准和附件测试期间，`personal-agent.service` 为 `ActiveState=inactive`、`MainPID=0`。用户已另行授权恢复自启并启动；以后重做独立录音前需明确停服，脚本自身仍不自动停启服务。
- 先独立测试和验证协议能力，不因基准测试扩展框架。不要重新训练唤醒模型，也不要为了测试新增付费 API 或自动回退到其他房间设备。
- 保留 MCP01 的 USB 连接。Dell 与设备在电视下，位置固定，不能把远离电视或移动麦克风作为必要方案。
- 实例配置的主音频输入、输出仍遵守进程启动时固定绑定规则。后续审查已备份私有配置，将原来的蓝牙连接更新为已验证的 MCP01 USB 输入、输出，保留逻辑设备 ID；恢复服务使用这组 USB 绑定。
- 后续代码审查撤回了为测试增加的运行时请求、采音探针、唤醒状态和端点指纹；测试逻辑集中在 `scripts/audio_check/`，复用既有设备资源接口。源码没有接入低阈值复核、云端音频判断或新的生产 DSP。

### 已完成的录音基准

- 早期 MCP01 蓝牙录音存在明显断续，观察到蓝牙语音帧校验失败；USB 对照未复现严重断续。用户确认 USB 录音听感良好。证据支持当前使用 USB，但不能仅据此断言所有无线故障的具体成因。
- USB 播放最初很小，排查到设备独立硬件总音量较低；调整硬件音量后用户确认相对正常，没有增加软件增益。
- 完整基准采用客厅中央、客厅远处、厨房、书房四个位置，各做安静与日常电视背景声两种条件，共八段真人录音，每段约 20 秒。每句只说一遍，提示与准备时间要清楚，不要求用户在终端填写测试句或复杂参数。
- 测试句为“嘟嘟，今天上午天气不错，我准备整理书桌，然后看看窗外”。**安静厨房一轮，用户实际漏说了“上午”，不能把转写缺少它算作识别错误。**
- 八段 USB 录音没有发现削波或长段数字零，最大采集交付间隔约 41.73 ms；不据此保证所有位置的语音内容都清晰。
- 当前本地唤醒检测在安静场景命中 3/4，安静书房漏检但本地 ASR 能识别完整测试句；电视背景声下命中 0/4，转写会混入或跟随电视内容。这里的 ASR 是独立基准使用的本地 Paraformer，不能等同于正式 GPT 语音链路。
- 对同一批录音进行离线 WebRTC 降噪对照，没有改善电视背景声下的唤醒命中；更强降噪还损失了部分安静场景命中。不把“加大降噪”视为已验证方案。
- 原始录音及逐项结果保留在私有目录 `state/acceptance/audio-input/offline-usb-rooms-20261018T094805/`，主记录为 `result.json`。不要重新采集这批已经可复用的样本，也不要提交录音。

### 已对齐的唤醒方向，尚未实现

1. 本地模型满足高阈值时，立即接受唤醒。
2. 本地模型检测到候选但未满足高阈值时，若达到较低的召回阈值，再用模型判断是否真的呼叫助手；用户接受这一路径稍有等待。
3. 二次判断的目标是“文字指令加一段真实音频，判断是否唤醒”，不把纯 ASR 转写或正式连续语音会话当作同一项验收。
4. 候选缓冲、排队与确认次数有界；每个候选只确认一次。高置信本地唤醒可抢先完成，迟到的确认不能重复唤醒；失败、超时或不确定时保持待机，并保护唤醒后的第一句话不被等待过程丢弃。

当前 FSMN V2 的高阈值为 0.485，但现有 CTC 解码还会剪枝，并主要向外输出完整关键词命中。安静书房样本没有形成有效候选，电视背景声也并非都有低分完整关键词候选。因此恢复工作时需要先核对候选产生与召回能力，**只给现有输出增加一个低阈值不能保证召回这些漏检**；不得通过用户已否定的训练方案绕过这个问题。

### GPT 订阅直接音频附件测试

使用上述安静书房的 20 秒真实 WAV；提交时使用中性文件名，没有向模型提供本地转写、预期答案或位置标签。使用已有 ChatGPT 订阅登录、独立临时线程、禁用工具的上下文，没有修改框架或实例配置。

| 模型 | 提交方式 | 实测结果 |
| --- | --- | --- |
| `gpt-6.1-sol` | `localAudio` WAV 附件 | 客户端接受；模型报告无法听到音频，结果不确定 |
| `gpt-6.1-sol` | `audio` 内嵌 WAV data URL | 同上 |
| `gpt-6-astra` | `localAudio` WAV 附件 | 同上；回显内容类型包含 `localAudio` |

每次约 6–8 秒；这是调用耗时，不是有效唤醒判断耗时。普通输入协议确实定义了 `audio` / `localAudio`，不能再表述成“客户端完全没有音频附件类型”；但客户端接受附件不构成模型理解音频的证据。本机模型目录仅宣告文字、图片输入，三个请求都明确报告音频不可访问，因此没有继续耗费请求做正反样本准确率测试。

结论只限于**这次测试的 Codex 普通订阅附件通道和模型**，不能推广为“GPT 订阅的所有语音入口都不支持音频”。项目已有的订阅原生实时语音通道属于另一个入口，其独立唤醒复核能力尚未验收。

- 三次私有证据：`state/acceptance/audio-wake-subscription/{20261018T104116Z,20261018T104210Z,20261018T104326Z}/probe.json`。
- 一次性探测脚本：`/tmp/probe-gpt-subscription-audio.py`；临时目录可能被清理，以已保存证据为准。脚本没有进入源代码管理。
- 三次记录均确认源码指纹未变、服务停止。没有配置自动启用的云端唤醒复核器。

### 回到本任务时

先读取本节与上述私有结果，确认用户要恢复语音工作；优先复用录音，如需新录音再明确停服。若继续验证订阅能力，应先做最小原生语音协议探测，明确它与普通附件测试的区别，并验证真实音频感知、无工具副作用、超时与退出。能力未证实前不扩展框架；能力证实后再评估候选召回、误唤醒、首句保留及等待延迟，最后决定是否实现。

### 独立测试代码收敛审查

- 撤回测试专用的内核请求、管理 socket 分支、运行时采音统计、关键词计数、首句观测与连接指纹。本次提交不修改正式运行的 Python 代码；测试仅复用既有设备注册表与 PipeWire 提供者。
- 保留本地中文语音引导、四个房间的安静／日常背景声对照、普通朗读负例、唤醒接首句正例、现有 FSMN 离线检测和按需保存 WAV。原生语音连接、文本转写与首句可懂度不冒充自动验收通过。
- 修复提示音关闭前未排空造成截断的风险、提示缓存发布后临时目录清理失败、异常子进程输出的 UTF-8 解码，以及录音断流后等待超时才发现错误的问题。中断保留已完成记录，仅释放本脚本的资源，不自动恢复服务。
- 设备分组检查 101 项通过；最终全量检查通过格式、lint、类型、前端构建及 568 项测试，0 跳过。之后没有修改源码、测试或依赖。
- 两秒真实 USB 采音就绪检查：32,000 采样，削波 0%，最长交付间隔 68.52 ms；无原始音频保存。它不是新一轮真人朗读或唤醒质量验收。证据为 `state/acceptance/audio-input/standalone-20261018T110208657157Z/result.json`。
- 旧未提交实现及私有实例配置的审查前备份保留于 `state/acceptance/audio-review/20261018T105506Z/`，不进入 Git。
