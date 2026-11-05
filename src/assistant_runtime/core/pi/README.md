# Pi execution adapter

Optional task context snapshots use pinned SDK `buildSessionProjection` and fresh
native sessions, rather than flattening history through `session.context`. The adapter
records a settled leaf before publishing completion; source system/runtime instructions,
private reasoning and provider response state are excluded from exports. Imported usage
is not a target capacity measurement, so Pi re-estimates it and uses its existing
compaction/usage/cancellation path. See [task context snapshots](../../../../docs/guides/task-context-snapshots.md).

Custom Responses connections omit zero-length assistant text blocks only from the
detached provider-input view. Some bridges emit such an item after tool calls; replaying
it can create an empty assistant message between calls and their tool outputs. Actual
text, tool IDs/results, reasoning state and saved/event history remain unchanged. This
applies to ordinary conversations as well as imported task history; other protocols and
native account routes retain Pi's existing serialization.

Pi is the only agent harness. The host uses the provider-neutral `api/execution.py` contract; this adapter owns native JSONL, processes, session files and normalized events. Main conversation and task execution use the same implementation. Native subscription accounts are model providers, not separate executors; no Codex execution path is retained.

The conversation registry owns stable public session IDs. `PiBackend.open_conversation`
creates a per-session view with its own active turn, lock, callbacks, question and MCP
refresh state, sharing the root Pi client, execution admission and usage protection.
Native events route by engine session ID; entrances never receive or select that ID.
Repeated turns resume the same native model context; Pi compression and retained chat
history remain separate. Generic instructions include only enabled capabilities;
personal-assistant presentation and instructions remain owned by `core/speech`.

`catalog.py` derives the model definitions from `ModelSettings`. Actual model and thinking level are checked before each new run. Nested SDK/Agent retries are disabled; the shared adapter uses Pi’s public request-retry helper with a durable permit for every attempt. Checkpointed model changes belong to tasks. MCP definitions and Skill paths are explicit; main and task contexts have different server-bound identities. MCP discovery returns real schemas without executing tools.

Execution is full-permission; no OS sandbox is claimed. MCP tool policy remains enforced. Voice remains an unavailable placeholder owned by `core/speech/disabled.py`, pending independent acceptance. Pi owns no voice object and has no device-plugin dependency. Interactive MCP OAuth is not implemented; no login URL is fabricated.

Install the pinned runtime with `python -m assistant_runtime.app.cli install-pi` using the application's Python environment. No destination is required: Pi lives in the user's data directory, and a successful installation records its absolute Pi and Node paths. The default `pi_binary="pi"` uses that record without adding Pi to PATH; a destination argument selects and records a custom location, while an explicit `pi_binary` path overrides the record. The wheel includes `extension.ts`. Configure `main_model`, `main_reasoning_effort`, `model_api` and optional task recommendations. Secrets remain in private storage. Derived execution folders are mode 0700 and metadata/config files mode 0600.

New state never imports Codex context. The current plugin consolidation preserves existing Pi data; do not reset it at cutover. New Pi sessions survive clean restart; unconfirmed execution never automatically replays. Each session is closed through plugin ownership.

Eligible task clarification waits persist a native leaf/digest/size checkpoint before the
question is published. Restoration reads it without starting Pi or inference. A single
pending input with no other unresolved operation can resume after an explicit answer:
the pinned SDK appends its matching tool result, then the normal metered RPC prompt
continues the same PA turn. A durable answering intent precedes either live response or
restored dispatch; interrupted/uncertain answers never replay. Other incomplete tools,
changed history/bindings and legacy waits cannot be guessed into a resumable checkpoint.
Protocol representation stays in this adapter; tasks owns user actions and execution
owns canonical input receipts/admission. See [task recovery](../tasks/README.md#等待澄清后的重启).

Pi acknowledges `prompt` after preflight, which may include context compaction.
Run submission therefore uses the configured model/compaction transport deadline,
with an outer cleanup margin, rather than the 30-second control-command deadline.
Foreground, tasks and standalone executions use this adapter default; an explicit
caller deadline still applies. Timeout does not authorize replay. Conversation
submission failures are retained as visible messages rather than transient notices only.

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

`transport.py` authenticates Pi traffic and forwards normalized streaming events through the shared provider adapter. Before dispatch it obtains a durable permit from the shared [ai_usage plugin](../ai_usage/README.md); that plugin owns all counters, policies, storage and statistics. Native compaction passes through the same gate and counts only when a request is actually sent. Missing token telemetry is distinct from a lost execution result. Task continuation, explicit handoff and restart preserve work identity. Nested SDK/Agent retries and paid cache warming remain disabled.


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

The execution extension registers native model aliases and forwards Pi-native normalized transcripts/events through the host's metered `/native` transport. It does not emulate Responses for another provider. Account/alias/effort/work identities are bound on the host; request parameters cannot choose an endpoint, account or key. Models auxiliary/memory inference uses the same provider library with no tool declarations and rejects tool output. Provider SDK errors are sanitized before leaving the helper. Safe transient failures may retry through the single bounded request path described below; an unfinished stream retains unknown usage, while an authoritative HTTP rejection is recorded separately. Credential and process failures do not restart or replay an agent turn.

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

Tool text/structured content exceeding `model_api.tool_result_max_chars` (default
6000 characters) is retained by workfiles under a stable execution/call result ID.
This is a model-visible **preview** budget, never a model output limit. Original
native image bytes stay attached and do not consume that text budget. The private
`/tool-result` transport binds the current worker, admission, turn and pending call.
Controlled contexts keep their separate publication boundary.

Pi's `tool_result` hook returns an explicitly labelled head/tail excerpt and owner
outcome evidence; `read_result` reads authorized text ranges or JSON-pointer pages
without running the tool again. Explicit tool allowlists must include `read_result`
to use text externalization; otherwise original text stays available. Readers never
externalize their own output. Missing/expired/corrupt/forbidden are distinct errors.
Ordinary short results remain native, requiring neither a file nor a model round trip.
Unreachable storage leaves original content available with a separate delivery error.
An upstream's own truncation cannot be undone or silently called complete here.

Retained projections are appended once; subsequent reads are new tool messages.
No historical replacement, automatic summary inference or cache warming is introduced.
Result IDs/version digests and deterministic serialization remain stable. Existing Pi
input/cache usage telemetry is unchanged; savings require measurement, not an assumed
cache hit rate. Result reads produce normal bounded tool events; storage usage is
available through `FileRequest(result_usage)`.

Pi retains the original context until a summary completes. Compaction failure is
reported as `context_compaction_failed`, not a successful task and not an unknown tool
action. Only the bounded shared request policy may retry a safe, settled pre-output failure; there is no provider fallback. Explicit continuation
can retry native compaction/use the retained context; normal shared request, compaction,
time and cost budgets still apply. Unknown summary requests remain in the usage ledger
and retain conservative reservations, but do not poison future execution admission.
Unknown ordinary model requests retain their ledger entries and reservations. Only a
confirmed terminal generation error is marked `generation_settled`; its unknown billing
does not block safe continuation. Process loss and unconfirmed requests continue to block
the same work allowance until reconciled. This accounting uncertainty
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

## 统一工具调用边界

主对话、后台任务和 slim 复用相同 Pi 扩展与调用管线。登记目录、可调用集合和
模型可见定义分别维护：授权且已就绪的工具保持 active，`prepareLoadout.hiddenDeclarations`
隐藏暂不需要的声明。已知准确名称的工具无需先搜索、失败再重发。隐藏声明不撤销权限；
隐藏权限仍通过 MCP exposure、执行白名单和调用钩子共同约束。

`tool_search` 复用 Pi 元数据排序。准确名称逐个解析，支持完整名称、来源限定名称和
唯一短名；多个不同工具不是歧义，真正同名才返回来源候选。已显示定义仍能查询。
Pi 注册时提供的原始来源和工具名保留为身份映射；权限与原始定义使用该映射，
不从下划线化或带碰撞后缀的规范名反推原名。
带 namespace 或明确来源的查询只等待对应原生 MCP 连接；通用搜索可等待所有相关源。
原生分词不擅长中文，因此无匹配时提供有界来源目录，使用 namespace 浏览，不臆造工具。
尚未登记的工具不能依据历史定义执行；目录无匹配不证明连接成功或服务没有工具，
连接诊断仍以 Pi 原生 MCP 状态和日志为准，不从工具错误文字猜测连接状态。

配置归 `model_api`：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `max_loaded_tools` | 12 | 当前可见 MCP 定义数量，范围 1..64 |
| `tool_schema_max_bytes` | 65536 | 当前 MCP 声明 JSON UTF-8 字节预算，范围 1024..1048576 |
| `invalid_tool_call_limit` | 3 | 同轮同一无效调用的最大次数，范围 2..32 |

定义按最近选择/使用维护，在工具批次完成后的模型边界淘汰。单个超预算定义完整独占，
记录 oversized；不截断 Schema。淘汰不影响可调用性。历史 schema patch 在模型投影中
替换为当前登记定义，保留调用结果和原始会话证据；重启从冷目录恢复，不恢复全部旧定义。
基础文件和澄清工具不计入 MCP 定义预算；普通业务数据仍需分页、结果文件和 Pi 压缩。

`tool_lifecycle.ts` 只观察和约束 Pi 原生管线，不执行第二套循环。开始事件不代表业务
已经发出。原生参数校验/授权拒绝为 not_dispatched；进入工具处理后没有充分证据的
异常为 unknown。内部插件可通过公共 `ToolFailure/ToolOutcome` 提供明确阶段、字段、
恢复条件和回执。IPC 与 MCP 保留这些信息；第三方自报元数据不冒充内部证据。
成功返回工具结果也不代表异步业务完成，仍以业务操作回执为准。

相同名称、参数指纹、定义和权限版本的确定未执行错误在同一用户轮中缓存，再次出现
不访问后端，默认累计 3 次终止该轮。修改参数或定义后可重新校验。查询成功、普通轮询
不计数。已经返回 unknown 的相同调用在本轮不重复派发，要求查询回执或交给用户处理。
新用户轮重置局部重复检测；跨轮/重启仍由原有执行记录和业务回执判断是否可继续，
不把新轮当成外部写操作的自动重放许可。不提供自动模型回退或付费重试。

可信内置只读工具可使用 Pi 并行批次；写操作及第三方工具使用顺序执行。权限、
幂等性和跨任务资源锁仍归各自所有者；第三方 readOnlyHint 不授予并行/重试权限。
扩展成功加载后必须通过 RPC 命令注册确认；初始化失败不能降级到未受约束的 Pi。

执行记录保存定义/权限版本、调用 ID、阶段、派发证据、重复次数和耗时。validation_ms
是调用开始到本地校验/授权钩子的时间，waiting_ms 是后续原生准备钩子的等待时间，
duration_ms 是 Pi 报告的处理器耗时（包含适配/传输），result_processing_ms 是余下结果处理。
它们不假装能拆解第三方服务内部耗时。默认日志不新增原始参数、正文、图片或密钥。
主对话和后台过程分别显示未执行、被禁止、调用失败和结果待确认；错误详情默认折叠。

验收使用真实锁定 Pi＋本地合成服务：直接隐藏调用一次、来源/多名称/中文目录、
有界定义及淘汰、参数纠正、IPC/MCP 结构化错误、未知操作不重发、无效循环收敛及
权限加载失败，以及慢服务隔离、带连字符的工具身份和原生读并行/写顺序批次。
它们不代表真实供应商或设备验收；部署单独安排。

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
schema size and the current visible/callable counts; declaration order
is stable. Tool removal and authorization always take priority over prefix reuse.

Cache counts come from Pi's provider usage. The pinned SDK normalizes missing cache counts
to zero, so PA conservatively reports zero/absent cache-read and cache-write counts as null
(unknown), and preserves positive reported counts. It cannot distinguish an explicit zero
from SDK default zero without provider-level evidence. Total input/output accounting stays
unchanged; cache counts are subsets, never added again to already normalized totals.
Cache warming and nested SDK/Agent retries stay disabled. Safe request retries use the shared policy below. Stable requests improve eligibility,
but do not guarantee cache hits, routing, retention, or a lower bill.


## 模型请求错误与恢复

`reliability.mjs` 复用 Pi 1.4.0 公开的 `utils/provider-retry`，负责唯一的请求重试循环。
SDK 与 Agent 重试均为零。`attempts.py` 通过已有私有 JSONL 通道逐次准入、结算，
未收到准入或结算确认时不继续请求。主对话、任务、辅助推理、记忆和模型测试复用此路径；
测试或内部未计量的单次适配器调用不启用重试。

默认 `model_api.retry = { max_retries = 2, unknown_retries = 1, max_delay_seconds = 30 }`。
次数表示额外尝试，未知错误计入总上限。退避、抖动、Retry-After 与可取消等待来自 Pi；
供应商等待要求超过上限时停止，不提前重试。所有尝试共用外层总期限与原工作额度。

结构化 HTTP/流式错误先提取安全字段；原始响应正文、密钥和调用参数不作为错误日志。
认证、权限、余额、明确配额和参数错误不原样重试；临时故障可重试；未知错误最多一次。
已交付文字、思考或工具片段、未确认操作、取消与截止时间均阻止自动重发。
错误描述、请求标识、发生阶段、尝试耗时与等待时间进入现有执行/模型记录。
本地 IPC 传递公共 ModelCallError 证据；MCP 复用 ToolOutcomes，将确认失败与未知结果
分别写入标准错误封套，并保留 received_result.failure，不退化为截断异常字符串。

`generation_settled` 只说明本地生成明确结束，不说明供应商未计费。未知费用继续保留预留；
旧记录、进程断连或取消不补造该证据。重启不自动恢复任何请求。

人工恢复使用原会话、已确认工具结果和同一预算，通过服务端校验最近失败检查点、
待确认操作与请求身份后发起新的继续轮次。旧错误保留。会话恢复有持久化唯一目标和
请求幂等；任务恢复沿用已有 continue 路径，额外绑定失败 turn 与操作者请求 ID。
它不是供应商断点续传，也不保证模型不会主动提出新的操作。

验收：`python scripts/check.py --test test_model_reliability` 使用真实 Pi/SDK 和本地合成
HTTP 服务验证重试、逐次计量、取消、部分输出、文件工具结果及人工恢复。
`scripts/verify_web_composer.py` 验证错误详情和重试按钮。合成通过不等于供应商故障实测。

### 工具图片与操作结果分开确认

`tool_media.ts` 在统一 `tool_result` 边界识别原生图片块及已有 JSON 结果包中的
图片。它通过现有鉴权本地传输交给 execution/workfiles，在下一次模型请求前完成
留存；不启动另一个 MCP 操作、执行循环或网络文件下载。传输校验活动执行、轮次
和工具调用；请求不能指定所有者或保存路径。原图块、业务 `isError` 和工具操作回执
保留，附件失败不会改变业务调用结论。非图片结果不增加登记请求。

框架保留的 `details["personal_agent.tool_media"]` 以及简短文字回执说明附件状态、
登记引用、产物路径、字节数及摘要；上游自填同名元数据不能冒充此证据。
状态为 `ready/partial/failed`，只描述文件留存，不表示用户已经看过图片或外部操作
成功。主对话和任务投影复用登记 ID，slim 执行查询也能读取附件及产物。
图片已留存时，无需为“展示给用户”再调用外部保存工具；用户明确要求指定目标时
仍可进行另行导出。存储故障单独报告，禁止为补附件重做原操作。

结果投影将正文留存和图片登记分别说明：仅实际返回图片时携带 `media`，包含
`state`、`received_count` 和 `available_count`。部分或失败不能声称全部图片已交付；
登记表示回复资源可用，不证明用户已看到，也不绑定某种前端附件布局。
纯文字结果没有图片或导出提示。图片原始输入、业务结果及完整回执继续保留。

此改动不调整长文本截断计数和历史上下文压缩策略。登记失败有有界等待，
不自动重试设备或媒体操作；取消、迟到结果不能附到另一轮执行。
`test_tool_media` 通过锁定 Pi＋本地合成 MCP 验证留存先于下一模型请求，
并覆盖普通对话、独立执行、文件故障及来源元数据；不代表真实设备验收。

## Trusted read recovery

A bounded read recovery policy applies to Pi's native read/result-reader/discovery helpers
and host-registered internal MCP tools explicitly declaring read-only and non-destructive
behavior. External MCP hints do not grant this treatment. An explicit owner's unknown
outcome always retains no-replay protection, including when annotations claim read-only.

`model_api.tool_read_attempt_limit` (default 3, range 1..10) bounds actual attempts of the
same read/arguments/definition/permission snapshot in one turn, including the first call.
An owner-confirmed transient failure may be reread up to this limit; an unclassified read
failure permits at most one reread; a known permanent failure is not sent again unchanged.
The framework never starts these retries itself. Corrected arguments/version changes
are distinct calls, successful reads allow normal later polling, and unchanged blocked
calls still end the turn under the existing invalid-call threshold. Exhaustion reports
`repeated_read_failure` with the original safe cause, not an unknown write outcome.
Every model round remains within the existing work allowance and deadline.

The result reader preserves ResultAccessError codes and corrective actions across its
HTTP boundary. A missing JSON path requires corrected arguments; it never requires
reexecuting the producing tool. First inspect the actual structure with format=json and
an empty RFC6901 path, then read its fields or bounded text ranges.
