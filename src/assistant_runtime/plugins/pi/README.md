# Pi execution adapter

Pi is the only agent harness. The host uses the provider-neutral `kernel/execution.py` contract; this adapter owns native JSONL, processes, session files and normalized events. Main conversation and task execution use the same implementation. Native subscription accounts are model providers, not separate executors; no Codex execution path is retained.

`catalog.py` derives the model definitions from `ModelSettings`. Actual model and thinking level are checked before each new run. Gateway and Pi automatic retries are disabled. Checkpointed model changes belong to tasks. MCP definitions and Skill paths are explicit; main and task contexts have different server-bound identities. MCP discovery returns real schemas without executing tools.

Execution is full-permission; no OS sandbox is claimed. MCP tool policy remains enforced. Voice is an unavailable placeholder in `voice.py`, pending independent acceptance. Interactive MCP OAuth is not implemented; no login URL is fabricated.

Install the pinned runtime with `scripts/setup_pi.sh`; the Python wheel includes `extension.ts`. Configure `pi_binary`, `main_model`, `main_reasoning_effort`, `model_api` and task profiles. Secrets remain in private storage. Derived execution folders are mode 0700 and metadata/config files mode 0600.

New state never imports Codex context. Use the explicit state-reset tool before cutover of an existing installation. New Pi sessions survive clean restart; unconfirmed execution never automatically replays. Each session is closed through plugin ownership.

Tests: `.venv/bin/python scripts/check.py --group execution --group models --group tasks`. Real subprocess tests use a local synthetic Responses endpoint and cover actual tool writes, MCP execution, selection, clarification, cancellation and restart. They do not establish real-provider parity.

`transport.py` authenticates Pi traffic and adapts Responses streaming. Before dispatch it obtains a durable permit from the shared [ai_usage plugin](../ai_usage/README.md); that plugin owns all counters, policies, storage and statistics. Native compaction passes through the same gate and counts only when a request is actually sent. Missing token telemetry is distinct from a lost execution result. Task continuation/fallback/upgrade/restart preserve work identity. Pi automatic retries and paid cache warming remain disabled.


Skill selection belongs to the composition root: enabled plugins contribute their
packaged operational skills; private `config/skills/<name>/SKILL.md` entries require
`instance_skills = ["name"]`. The adapter receives explicit paths, shared by main
and task sessions. Pi runs with `-na -np -ns -nc -ne` and an explicit extension,
and explicitly retained native MCP/tool-search/codemode/llama.cpp extensions,
so Coding AI `.agents/`, project `AGENTS.md`/`CLAUDE.md`, local prompts and extensions
are not discovered. Instance skill selection is optional, restart-only and validated;
missing files and bundled-name collisions fail startup rather than silently losing
instructions. This controls automatic loading, not arbitrary filesystem permissions.

## Native model accounts

`accounts.mjs` runs only the pinned `pi-ai` provider/auth library and Pi's file credential store. It cannot execute tools or run an agent. The Python adapter owns process bounds, lifecycle and private IPC; each configured account has independent credentials, and upstream file locks serialize refresh across main, auxiliary inference and operator processes. It never loads global Pi/Codex accounts, project extensions or Coding AI skills. Login/refresh credentials remain within the helper; model processes receive only the existing authenticated loopback route.

The execution extension registers native model aliases and forwards Pi-native normalized transcripts/events through the host's metered `/native` transport. It does not emulate Responses for another provider. Account/alias/effort/work identities are bound on the host; request parameters cannot choose an endpoint, account or key. Models auxiliary/memory inference uses the same provider library with no tool declarations and rejects tool output. Native SDK errors are sanitized before leaving the helper. No request is automatically retried; an unfinished stream remains unknown, while an authoritative HTTP rejection is recorded separately. Credential and process failures do not restart or replay an agent turn.

Provider catalogs are bundled metadata, not account entitlements. Actual images, structured output and tools still need per-route acceptance. ChatGPT's official OAuth route does not accept a request output cap, so `output_limit="model"` is mandatory; the host reserves the catalog maximum. Other provider request caps are checked against the actual constructed payload before send; unsupported encodings fail instead of losing a limit. Run the model-account tests with the models group; they use real Pi/SDK subprocesses and a localhost endpoint, never paid services.
