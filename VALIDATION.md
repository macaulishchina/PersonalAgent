# Refactor validation — 2026-10-12 (Asia/Shanghai)

This report describes the current plugin-runtime implementation. The original prototype evidence remains in Git tag `baseline-before-refactor`. Detailed reports, device identifiers and recorded test samples stay in ignored `state/acceptance/`.

## Automated gate

`.venv/bin/python scripts/check.py` passes formatting, Ruff, kernel type checks and **43 unit tests**. It includes all 24 original behaviors plus plugin lifecycle/cleanup, bounded subscribers, request deadlines, stale generation rejection, explicit gesture dispatch, reconnect responsiveness, stable session mapping, and memory provenance/revision/tombstone/retry tests.

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
