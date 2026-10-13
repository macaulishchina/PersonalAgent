# Refactor validation — 2026-10-12 (Asia/Shanghai)

This report describes the current plugin-runtime implementation. The original prototype evidence remains in Git tag `baseline-before-refactor`. Detailed reports, device identifiers and recorded test samples stay in ignored `state/acceptance/`.

## Automated gate

`.venv/bin/python scripts/check.py` passes formatting, Ruff, kernel type checks and **55 unit tests**. It includes all 24 original behaviors plus plugin lifecycle/cleanup, bounded subscribers, request deadlines, stale generation rejection, explicit gesture dispatch, reconnect responsiveness, stable session mapping, and memory provenance/revision/tombstone/retry tests, and wake-benchmark accounting for missed/duplicate/late detections.

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
- Review fixed pure benchmark accounting imports that previously required optional `pypinyin`/`psutil` packages. A subprocess test blocks benchmark-only packages and confirms the accounting helpers still import and work.
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
