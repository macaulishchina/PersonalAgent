# Pi execution adapter

Pi is the only agent harness. The host uses the provider-neutral `api/execution.py` contract; this adapter owns native JSONL, processes, session files and normalized events. Main conversation and task execution use the same implementation. Native subscription accounts are model providers, not separate executors; no Codex execution path is retained.

The conversation registry owns stable public session IDs. `PiBackend.open_conversation`
creates a per-session view with its own active turn, lock, callbacks, question and MCP
refresh state, sharing the root Pi client, execution admission and usage protection.
Native events route by engine session ID; entrances never receive or select that ID.
Repeated turns resume the same native model context; Pi compression and retained chat
history remain separate. Generic instructions include only enabled capabilities;
personal-assistant presentation and instructions remain owned by `core/speech`.

`catalog.py` derives the model definitions from `ModelSettings`. Actual model and thinking level are checked before each new run. Gateway and Pi automatic retries are disabled. Checkpointed model changes belong to tasks. MCP definitions and Skill paths are explicit; main and task contexts have different server-bound identities. MCP discovery returns real schemas without executing tools.

Execution is full-permission; no OS sandbox is claimed. MCP tool policy remains enforced. Voice remains an unavailable placeholder owned by `core/speech/disabled.py`, pending independent acceptance. Pi owns no voice object and has no device-plugin dependency. Interactive MCP OAuth is not implemented; no login URL is fabricated.

Install the pinned runtime with `python -m assistant_runtime.app.cli install-pi` using the application's Python environment. No destination is required: Pi lives in the user's data directory, and a successful installation records its absolute Pi and Node paths. The default `pi_binary="pi"` uses that record without adding Pi to PATH; a destination argument selects and records a custom location, while an explicit `pi_binary` path overrides the record. The wheel includes `extension.ts`. Configure `main_model`, `main_reasoning_effort`, `model_api` and optional task recommendations. Secrets remain in private storage. Derived execution folders are mode 0700 and metadata/config files mode 0600.

New state never imports Codex context. The current plugin consolidation preserves existing Pi data; do not reset it at cutover. New Pi sessions survive clean restart; unconfirmed execution never automatically replays. Each session is closed through plugin ownership.

Tests: `.venv/bin/python scripts/check.py --group execution --group models --group tasks`. Real subprocess tests use a local synthetic Responses endpoint and cover actual tool writes, MCP execution, selection, clarification, cancellation and restart. They do not establish real-provider parity.

On Windows, the native Pi Bash/PowerShell tool definitions use a platform-owned
execution adapter. Each command runs in a Windows Job Object via the application's
Python; cancellation and timeout close the helper's control pipe and wait for the
owned process tree to settle. This does not require `taskkill /T` privileges. The
Windows Bash backend uses native Git Bash/MSYS2, not the WSL launcher, whose Linux
processes cannot be owned by the command's Windows Job Object. PowerShell remains
the Windows default; explicit Bash requires a native Bash installation. The
Linux tool implementation is unchanged. The platform helper is packaged with the
application, so both source and installed-package acceptance must cover it.

`transport.py` authenticates Pi traffic and forwards normalized streaming events through the shared provider adapter. Before dispatch it obtains a durable permit from the shared [ai_usage plugin](../ai_usage/README.md); that plugin owns all counters, policies, storage and statistics. Native compaction passes through the same gate and counts only when a request is actually sent. Missing token telemetry is distinct from a lost execution result. Task continuation, explicit handoff and restart preserve work identity. Pi automatic retries and paid cache warming remain disabled.


Skill selection belongs to the composition root: enabled plugins contribute their
packaged operational skills; private `config/skills/<name>/SKILL.md` entries require
`instance_skills = ["name"]`. The adapter receives explicit paths, shared by main
and task sessions. Pi runs with `-na -np -ns -nc -ne` and an explicit extension,
and explicitly retained native MCP/codemode/llama.cpp extensions plus the public
native tool-search factory composed by the adapter,
so Coding AI `.agents/`, project `AGENTS.md`/`CLAUDE.md`, local prompts and extensions
are not discovered. Instance skill selection is optional, restart-only and validated;
missing files and bundled-name collisions fail startup rather than silently losing
instructions. This controls automatic loading, not arbitrary filesystem permissions.

## Native model accounts

`accounts.mjs` runs only the pinned `pi-ai` provider/auth library and Pi's file credential store. It cannot execute tools or run an agent. The Python adapter owns process bounds, lifecycle and private IPC; each configured account has independent credentials, and upstream file locks serialize refresh across main, auxiliary inference and operator processes. It never loads global Pi/Codex accounts, project extensions or Coding AI skills. Login/refresh credentials remain within the helper; model processes receive only the existing authenticated loopback route.

The execution extension registers native model aliases and forwards Pi-native normalized transcripts/events through the host's metered `/native` transport. It does not emulate Responses for another provider. Account/alias/effort/work identities are bound on the host; request parameters cannot choose an endpoint, account or key. Models auxiliary/memory inference uses the same provider library with no tool declarations and rejects tool output. Provider SDK errors are sanitized before leaving the helper. No request is automatically retried; an unfinished stream remains unknown, while an authoritative HTTP rejection is recorded separately. Credential and process failures do not restart or replay an agent turn.

Provider catalogs are bundled metadata, not account entitlements. Actual images, structured output and tools still need per-route acceptance. ChatGPT's official OAuth route does not accept a request output cap, so `output_limit="model"` is mandatory; the host reserves the catalog maximum. Other provider request caps are checked against the actual constructed payload before send; unsupported encodings fail instead of losing a limit. Run the model-account tests with the models group; they use real Pi/SDK subprocesses and a localhost endpoint, never paid services.

Custom Anthropic connections reuse only the exact model's pinned
`forceAdaptiveThinking` declaration; configured credentials, endpoint, enabled
levels and conversation policy stay independent. Pi encodes adaptive effort or
legacy token budgets for both execution and auxiliary inference. Legacy thinking
fits inside the configured total output ceiling, and insufficient room fails
before HTTP. Explicit `none` uses the adapter's disabled-thinking input; `auto`
removes control fields without choosing a depth. Structured formats and effort
coexist in `output_config`. Real-SDK local tests cover both thinking modes and
all imported Haiku efforts; they do not certify a third-party endpoint.

Foreground conversation startup is separate from transport startup (`foreground=False`
for standalone execution composition). This uses the same client, event dispatcher and
usage transport, creates no foreground session, and rejects foreground submission.
Personal-assistant instructions belong to `core/speech/instructions.py` and are
composed only when the assistant capability is enabled; slim uses generic conversation instructions.
Executor disconnect settles public execution ownership
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
Unknown ordinary model requests retain their ledger entries and reservations, and block
continuation within the same work allowance until reconciled. This accounting uncertainty
does not make a settled agent run an unknown operation: if Pi reports `agent_settled` and
all tools have ended, a generation failure is `failed`, allowing a new explicit user
request in the same conversation without retrying the failed request. Process loss,
unconfirmed submission and outstanding tools still produce `unknown` and block session
reuse. Every started tool is tracked durably, including tools omitted from the UI.
Restart never clears the shared-work guard or replays tool operations. A disconnect or
cancellation cannot be relabeled as a known compaction failure. Logging records compaction
outcome without summary content.

## Conversation message lifecycle

Native assistant messages may contain only reasoning or tool calls. Their start is
not a visible reply: the adapter publishes message activity only on the first
non-whitespace text delta, retaining any leading whitespace. Final text without
deltas still produces its normal completion event. Tool activity and execution
outcomes remain independent; a tool-only model step never creates a reply bubble.
The web projection also suppresses empty assistant reply records from existing
history without deleting evidence, media, tool activity or the turn's failure notice.
The response indicator continues to follow execution state while no text is available.

## Native tool discovery and bounded declarations

All entrances (foreground, background jobs, public execution/slim) use this adapter's
MCP mapping: allowed tools have native Pi `deferred` exposure, disabled/disallowed tools
remain `hidden`. The adapter composes Pi's public `createToolSearchExtension` factory
as the single `tool_search` registration. Pi still owns metadata ranking, MCP readiness
and tool activation; there is no separate discovery service, model router or agent loop.
Known full names (`mcp__server__tool`) and qualified names (`server.tool_name`) resolve
exactly, including tools already loaded. A unique short name such as `capture_image`
takes priority over metadata ranking; names shared by multiple servers require
qualification rather than silently selecting one. Unknown qualified names do not
fall through to a different server. Natural-language queries retain Pi's native
English metadata search. Neither path executes a business operation.
The native compact server directory is bounded by Pi and does not include full schemas.

`model_api.max_loaded_tools=12` (1..64) bounds discovered MCP definitions per model
request, separately from the small native execution/clarification tools. Search batches
cannot exceed this limit. Newly loaded and recently used tools take priority; eviction
occurs at the next model boundary, after the preceding parallel tool batch settles.
Evicted tools remain registered and searchable. A new engine session starts fresh;
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

The live session keeps its bounded warm loadout across user turns. Before a new turn,
removed/hidden/out-of-scope identities are excluded; process restart and session resume
start cold. This avoids searching again for recently used tools without accumulating
their schemas. The same behavior applies to conversations, background jobs and slim.

A direct call to an undeclared tool can be rejected by Pi before any execution. Only
Pi's exact missing-tool result with no execution duration is eligible for recovery.
If that exact identity is registered and permitted, the adapter loads it and preserves
the error while explaining that no operation ran; otherwise it requests exact discovery
through `tool_search`, which owns MCP readiness. It never invokes or retries the operation.
Real tool failures, unknown outcomes and denied tools are not relabeled as loading errors.
Discovery logs record session, lookup mode and permitted tool names, without raw queries,
arguments, schemas or credentials. These distinguish exact/metadata/ambiguous/unavailable
lookup and loading after a missing-tool error.

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


Public execution message receipts are correlated at the metered native model-input boundary.
The host supplies an opaque marker for explicit supplements/answers; matching additionally
checks the active run, native role and answer tool-call ID. A successful steer RPC only means
accepted. Consumed receipts include the durable usage call ID and prove input inclusion,
not provider processing or task success. The adapter never resends an uncertain public input.


## Stable independent-execution prefixes (0.4.1)

Public `ExecutionRequest.submit` needs no new fields. Each execution still owns a distinct
Pi session, real cwd, budget identity and scratch/artifact directories. The adapter describes
that cwd as `.` and deliverables as `../artifacts` in model-visible system sections, while
keeping the actual cwd for tools and persisted metadata. Caller text and attachments remain
data in their original order; PA does not join histories or introduce cross-session cache keys.
Other conversation, task and controlled-context prompt policies remain unchanged.

An explicit public-execution whitelist containing 1..min(4, max_loaded_tools) MCP tools
uses Pi's native direct-server readiness before the first request. Only permitted identities
are candidates. Their combined JSON declarations must fit 16 KiB; missing/hidden tools or
larger declarations keep discovery behavior. Larger and implicit inventories stay deferred.
Preloading declares tools; it never invokes them or grants approvals. `tools.updated` reports
schema size and sanitized discovery logs report `preloaded` / `preload_deferred`; declaration order
is stable. Tool removal and authorization always take priority over prefix reuse.

Cache counts come from Pi's provider usage. The pinned SDK normalizes missing cache counts
to zero, so PA conservatively reports zero/absent cache-read and cache-write counts as null
(unknown), and preserves positive reported counts. It cannot distinguish an explicit zero
from SDK default zero without provider-level evidence. Total input/output accounting stays
unchanged; cache counts are subsets, never added again to already normalized totals.
Cache warming and automatic paid retries stay disabled. Stable requests improve eligibility,
but do not guarantee cache hits, routing, retention, or a lower bill.
