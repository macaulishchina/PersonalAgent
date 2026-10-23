# Pi execution adapter

Pi is the only agent harness. The host uses the provider-neutral `api/execution.py` contract; this adapter owns native JSONL, processes, session files and normalized events. Main conversation and task execution use the same implementation. Native subscription accounts are model providers, not separate executors; no Codex execution path is retained.

`catalog.py` derives the model definitions from `ModelSettings`. Actual model and thinking level are checked before each new run. Gateway and Pi automatic retries are disabled. Checkpointed model changes belong to tasks. MCP definitions and Skill paths are explicit; main and task contexts have different server-bound identities. MCP discovery returns real schemas without executing tools.

Execution is full-permission; no OS sandbox is claimed. MCP tool policy remains enforced. Voice remains an unavailable placeholder owned by `core/speech/disabled.py`, pending independent acceptance. Pi owns no voice object and has no device-plugin dependency. Interactive MCP OAuth is not implemented; no login URL is fabricated.

Install the pinned runtime with `scripts/setup_pi.sh`; the Python wheel includes `extension.ts`. Configure `pi_binary`, `main_model`, `main_reasoning_effort`, `model_api` and task profiles. Secrets remain in private storage. Derived execution folders are mode 0700 and metadata/config files mode 0600.

New state never imports Codex context. The current plugin consolidation preserves existing Pi data; do not reset it at cutover. New Pi sessions survive clean restart; unconfirmed execution never automatically replays. Each session is closed through plugin ownership.

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

Foreground conversation startup is separate from transport startup (`foreground=False`
for standalone execution composition). This uses the same client, event dispatcher and
usage transport, creates no foreground session, and rejects foreground submission.
Personal-assistant instructions belong to `core/conversation/instructions.py` and are
loaded by the conversation path. Executor disconnect settles public execution ownership
before reporting the transport failure; it never replays queued or uncertain work.

The declared Pi plugin can start without a foreground conversation and without speech
or device services. A real subprocess test composes usage, Pi and public execution via
the same runtime dependency graph, then verifies results and durable usage. Standard
composition still owns foreground callbacks; no second executor is introduced.

## Context size and compaction recovery

`model_api.compaction_timeout_seconds` defaults to 600 seconds (configurable 1..3600),
independent of ordinary `timeout_seconds`. The host enforces the deadline on both
Responses and native-account routes; Pi's outer provider timeout permits this longer
maintenance operation. The provider adapter still owns the only execution loop.

Tool text exceeding `model_api.tool_result_max_chars` (default 6000 characters) is saved
in private `tool-results/*.json` under the execution working directory. Pi receives a
partial preview and a reference to the received content, including structured output.
Image blocks and error status are preserved. The preview explicitly cannot establish
absence or success. Read selected ranges/fields instead of repeatedly dumping entire
files. This operates across tools, not through media-specific rules. If the execution
cannot read local files, or saving fails, the original result is retained; a completed
business action must not appear failed or become unreadable because of presentation.
An upstream's own truncation cannot be undone by this layer.

Pi retains the original context until a summary completes. Compaction failure is
reported as `context_compaction_failed`, not a successful task and not an unknown tool
action. There is no automatic paid retry loop or provider fallback. Explicit continuation
can retry native compaction/use the retained context; normal shared request, compaction,
time and cost budgets still apply. Unknown summary requests remain in the usage ledger
and retain conservative reservations, but do not poison future execution admission.
Unknown ordinary execution requests still block until reconciled; restart never clears
that protection or replays tool operations. A disconnect/cancellation cannot be relabeled
as a known compaction failure. Logging records compaction outcome without summary content.

## Native tool discovery and bounded declarations

All entrances (foreground, background jobs, public execution/slim) use this adapter's
MCP mapping: allowed tools have native Pi `deferred` exposure, disabled/disallowed tools
remain `hidden`. The built-in Pi `tool_search` performs metadata ranking, connects the
needed MCP servers, and activates matches; there is no separate discovery service,
model router or agent loop. Native search uses English metadata terms; queries should
use meaningful server/tool keywords and can be refined when the first matches miss.
The native compact server directory is bounded by Pi and does not include full schemas.

`model_api.max_loaded_tools=12` (1..64) bounds discovered MCP definitions per model
request, separately from the small native execution/clarification tools. Search batches
cannot exceed this limit. Newly loaded and recently used tools take priority; eviction
occurs at the next model boundary, after the preceding parallel tool batch settles.
Evicted tools remain registered and searchable. A new top-level run starts fresh;
resume does not eagerly restore every previously used MCP definition. A missing match
or disconnected server is an explicit tool result, never an excuse to expose all tools.

Pi's public `context_with_system` hook projects only current tool declarations into
requests, because durable historical schema patches would otherwise keep evicted
schemas in context. User messages, tool calls/results and system sections are retained;
the original transcript remains intact. Existing schema metadata is preserved. Native
accounts and Responses routes receive the same provider-neutral projection. Tool search
itself uses ordinary function calling; it requires no vendor-specific tool-search API.

Explicit execution `tools` remains the authorization ceiling. Discovery is a host-added
infrastructure tool only when that scope contains MCP capabilities. It cannot search
hidden tools or activate another server outside the captured scope. Denied tools are
hidden before discovery, and every actual call retains existing caller identity, task
scope, approvals and policy validation. Pure native-file execution/empty tool selections
do not gain MCP access. Tool searches do not execute business operations, automatically
retry tools, or grant approvals. Model turns still consume the same shared AI allowance.

`tools.updated` events and sanitized logs record the active tool names, catalog size,
evicted names and declared schema character count (not estimated tokens). No tool
arguments, results or credentials enter those logs. This bounds tool-definition growth;
ordinary task data still needs pagination, file references and native compaction.

## Native prompt-cache reuse and timings

Native account calls use Pi's normal `cacheRetention="short"` and retain the
host-bound execution session identity for provider-supported cache affinity.
This reuses computation from already requested input; native cache warming and
automatic retries remain disabled. Do not force `none` to disable warming: these
are separate mechanisms. OAuth/API compatibility and supported cache parameters
remain owned by Pi's provider library; the adapter does not inject unsupported
retention fields into ChatGPT requests or promise a cache hit.

The helper measures request preparation (including native auth/lazy loading),
response headers, first content/text and stream completion using one monotonic
clock. Execution logs join these numeric timings with the usage call ID and
reported input/cache-read tokens. Auxiliary inference receipts include the same
`transport_timings`. Request size is bytes, not model tokens. No bodies, images,
headers or credentials are recorded. The interval to response headers includes
connection, upload and upstream waiting; it cannot alone identify any one of them.

Real ChatGPT synthetic acceptance on 2026-10-23 used one shared bounded allowance,
11 model requests, no tools and no real conversations/photos. A repeated 18,749-token
text input had zero reported cache reads with the former `none` adapter policy;
native `short` reuse reached 18,560 cached tokens. A separate 73,150-token text
input reached 72,960 cached tokens and completed in 4.537 seconds when repeated.
Synthetic large-PNG requests reached 73,856/74,221 cache reads yet still took
50.874–107.622 seconds; a follow-up retained 73,856/74,240. JPEG variants reached
73,856/74,201 and took 9.680–17.764 seconds. These are small, non-interleaved samples,
not latency guarantees or proof that caching alone fixes the production slowdown.
Image encoding and upstream variability remain separate concerns. Production was
not restarted, real images were not changed, and private receipts remain outside Git.

The real-SDK/local-server regression verifies stable cache/session parameters,
cache-read telemetry without double counting, monotonic timing order, and exactly
the requested calls with no warming or replay. Live supplier acceptance remains
explicit and is never part of automatic tests.

Real Pi/local-provider tests cover no upfront MCP schemas, scoped discovery and calls,
repeated search/eviction/reload, bounded historical schema projection, oversized searches,
forged out-of-scope calls, restart, foreground-to-task delegation, native-account mixed
execution and installed plugins through slim's public execution. They do not establish
new paid-provider parity.
