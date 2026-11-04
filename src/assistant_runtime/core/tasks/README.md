# Tasks plugin

Since 0.4.17, agent task creation may explicitly provide `context.snapshot_ref` and
an optional registered UTF-8 `context.additional_input`. Ordinary tasks and children
do not inherit context. Each scheduled run gets an independent copy; continuation
uses its own retained session. See [context snapshots](../../../../docs/guides/task-context-snapshots.md)
for receipt recovery, permissions, compaction, retention and KFC integration.

Public route-constrained tasks and explicit model-default effort are opt-in additions
in 0.4.14. Existing selection/default/child-task behavior remains unchanged. A read-only
`TaskRequest("receipt", {"request_id": UUID})` recovers creation identity without replay.
See [route/default/receipt examples](../../../../docs/guides/model-routes.md).

Trusted local plugin for independent background work through the configured Pi executor and explicitly accepted API models.

Task specs may restrict `tools` to an explicit list of registered tool names. Child tasks inherit the parent's list and may narrow it; expansion is rejected. A background caller may query public `execution_info` only for its own run identity, allowing a trusted integration to recover its business scope without reading PA private storage.
Enable `notifications` and `tasks` in the instance `plugins` list and install the `tasks` extra in the existing `.venv`.
Requires execution, workfiles, notifications and the configured brain capability; conversation is optional. Does not activate the microphone.

Requested media belongs to the producing run. Provider callbacks accumulate up to
four deduplicated registered references; the workfiles media owner validates and
pins them even when completion notifications are disabled. `get_task` exposes each
run's validated references and top-level `attachments` from the newest run on that
page, identified by `attachment_run_id`. An empty newer run never inherits an older
picture. Main conversation tool replies and task details render those references
through the same authenticated media viewer, outside collapsed tool text. Image
preview uses a native modal above the mobile task dialog. Raw pixels do not enter
task JSON/SSE or notification prose. Prose saying "sent" is not delivery evidence.
Recorded task replies can restore missing conversation attachments from their
existing registered references without rerunning tasks or capturing another image.

## 模型配置与推荐

后台任务不要求单独配置模型。省略 `tasks.execution` 或保持两个字段为 `auto`，
使用配置中的 `main_model` 与 `main_reasoning_effort`，而非某个父任务偶然使用的模型。
创建时将具体选型与来源保存到任务及执行快照；修改推荐或主对话不会改写已保存任务。
显式 `tasks.execution` 是所有任务的固定约束。记忆仍使用独立的 `model_api.memory`。

```toml
[tasks]
concurrency = 4

[tasks.model_policy]
review_mode = "shadow" # off / shadow / active；decisions 可不启用
max_upgrades = 1

# 可完全不添加推荐；ID 稳定，名称、模型与场景由实例维护，没有内置顺序。
[tasks.model_policy.recommendations.diagnosis]
name = "复杂诊断"
model = "registered-model-alias"
reasoning_effort = "high"
reason = "适合多假设分析和冲突证据推理"
description = "根因未明、需要比较多个解释并验证时使用"
```

配置界面：**任务与通知 → 安排任务**，可管理默认选型及推荐项。
模型库同时显示主对话标签、引用该模型的推荐名称；悬停可查看推荐理由、场景和深度。
模型引用、重名、深度支持情况在保存前校验；移除仍被推荐引用的模型需要先调整推荐。

`list_task_models` 返回主对话默认、固定约束、已登记模型及其原生深度、推荐项和统一指导。
调用者可传 `execution={recommendation:"diagnosis", reasoning_effort:"high", reason:"..."}`，
或直接选择 `execution.model`。推荐项的深度可覆盖；显式模型与推荐不一致会报错，
不静默忽略其中一项。显式选择需说明理由。主模型根据实际目标、已有流程与验收要求决策，
步骤多和耗时长不等于推理困难。没有推荐也可使用已登记的模型。

### auto 的职责

- 主对话 `auto`：不指定供应商思考强度，由供应商决定。
- 任务省略深度：沿用主对话的具体深度；推荐有具体深度时优先使用推荐。
- 任务有效深度仍为 `auto` 且模型支持调整：发起模型必须在工具调用中给出受支持的具体值。
  缺少时返回可选值，尚未创建或执行任务；框架不臆造跨供应商档位。
- 模型不提供可调深度：使用 `auto`，不会虚构 `none/low` 参数。

### 可选 decisions

Jev 的传输、授权、阈值和判断日志全部属于 `decisions` 插件。任务仅调用公共决策契约；
不读取或复制其配置。未安装、停用、未授权、超时或无可信建议时保留原有有效选型。
`off` 不调用；`shadow` 在初始派发后异步观察；`active` 创建/明确更改选型时只做一次有界复核。
候选来自主对话、推荐及显式选择，按账号支持的原生深度过滤；不内置模型强弱顺序。
`model_source/effort_source="assistant"` 表示可调整的助手推荐；`"user"` 只用于用户明确选择。
用户、固定配置及未标记的显式字段保持保护；偏好文字不产生硬约束。

### 失败与显式模型交接

模型错误直接失败，结果未知则记录未知；不自动换模型或重做操作。
辅助推理同样只调用指定模型一次。调用 ID、共享额度、取消和迟到结果保护继续有效。
旧 `profiles/default_profile/fallbacks` 配置已删除，保存/启动会拒绝这些字段。
升级现有实例时明确移除这些字段，按用途创建推荐；历史结果和操作证据无需重写。

`request_task_model_upgrade` 继续保留安全交接能力，目标改为推荐 ID，不再按四档排名。
必须给出真实推理困难、证据、检查点与唯一 UUID；`reasoning_effort` 可选择具体原生值。
仅作用于当前运行，受 `max_upgrades` 限制。网络错误、欠费和未知操作不能触发交接。
同模型交接须提高具体深度；不同模型是否更合适由调用者根据推荐描述判断。

检查点格式：
```json
{"completed":["verified results and operation IDs"],"next_step":"remaining work",
 "active_operations":[],"uncertain_operations":[]}
```

宿主等待确认完成的执行边界、无活动/未知操作及无后台终端，再归档旧上下文并创建新上下文；
不复制供应商私有推理。用户取消优先；交接确认丢失或重启中断记录未知，不重放。
长期 Goal 不允许自动模型交接；其继续执行仍由任务已有轮次与共享用量保护管理。

## Task definitions and runs

### 派发、阻塞与恢复

任务计划、单次执行和外部操作是三种不同状态。归档只结束计划；不能据此断言
已经发出的操作结束。归档返回 `unfinished_runs`，保留未知执行及其占用。

派发前持久记录 `data.dispatch`：准备、用量绑定、模型检查和会话创建没有发送
Agent 轮次时为 `not_dispatched`；紧邻 `run.start` 之前记为 `unknown`，收到回执后
记为 `dispatched`。准备失败结束为 `failed`，不占据未知资源。缺少历史 session ID
不是未派发证明。取消失败、断连和迟到结果都不触发重做；未知执行不能直接继续。

`resources` 只描述授权范围；新任务使用独立的 `resource_locks` 声明确实需要
调度级互斥的对象。默认不加粗粒度互斥，实际写入仍由业务插件的事务、版本、
幂等键和资源锁保证。同任务仍不允许重叠。旧快照没有 `resource_locks` 时保留
原占用语义，尤其未知记录不能因升级被自动解锁。纠正授权范围不修改互斥锁。

排队执行的 `data.waiting` 区分执行名额与资源等待，包含阻塞任务、执行、资源和
建议动作；按接受顺序派发，解除阻塞后沿用原执行 ID，不另建执行。网页可直接
查看阻塞任务，历史任务的未知执行仍有“核对执行”和人工收尾入口。

操作者使用统一任务入口进行恢复，不直接编辑数据库：

```sh
pa tasks reconcile_run '{"run_id":"RUN_ID"}'
pa tasks history '{"run_id":"RUN_ID","cursor":0,"limit":50}'
pa tasks resolve_run '{"run_id":"RUN_ID","revision":LATEST_REVISION,"confirmed":true,"reason":"核对了哪些外部操作回执、结果及在途工作，为什么可以解除占用"}'
```

核对使用 Pi 原生状态及 execution 所有者的 `task_evidence` 内部接口；失去派发
响应但唯一可关联的规范执行仍可找回身份。回执查询不调用模型、不重放任务。
证据不足保持 `unknown`，并显示需要操作者核实。人工收尾必须先核对、通过
版本检查、明确确认并填写至少十字依据；确认本地工作已停止后记为 `canceled`，
不会声称业务成功，不恢复或重新执行。恢复操作不暴露为模型 MCP 工具。

首次结构化异常（阶段、错误码、类型、调用栈位置）、后续停止、状态变化、核对
和人工依据写入追加式 `job_run_history`，不再以唯一的 `progress` 充当错误证据。
不保存异常局部变量或任意传输正文；可信 `ToolFailure` 保留其安全说明。
CLI 游标可读取完整历史，网页过程只展示元数据，不复制进展正文、参数或凭据。
规范执行摘要有界；超出摘要覆盖时保守提示未完全核对，不能据此自动解除保护。

任务保存的模型已从当前可用目录移除时，在新轮次派发前按当前任务固定配置／
主对话绑定重新评估并保存旧选型与原因。旧模型仍可用时保留其选型；供应商调用
失败不触发换模型。当前绑定也不可用或没有可确定的思考深度时，明确准备失败，
等待修改配置，不任意选择模型、猜测思考深度或调用付费判断服务。

Definitions hold authorized objectives, trigger, workspace, model, notification policy
and optional resource lock names. Runs snapshot the definition revision and have stable
IDs, progress, questions, provider thread/turn IDs, results and artifacts. A reminder
uses `execution.kind=reminder` and does not invoke a model. A deterministic sensor check
uses `execution.kind=device_query` with a configured `device_id` and the existing typed
read capability. Other tasks use `kind=agent`.

Triggers:
- `{"kind":"immediate"}`
- `{"kind":"date","at":"2030-01-01T09:00:00+08:00"}`
- `{"kind":"interval","seconds":3600}`
- `{"kind":"cron","expression":"0 9 * * mon-fri","timezone":"Asia/Shanghai"}`
- `{"kind":"event","event":{"source":"device","id":"living_temperature","metric":"temperature","operator":"gt","value":28,"hysteresis":1,"duration_seconds":30,"cooldown_seconds":300}}`
- `{"kind":"event","event":{"source":"task","id":"TASK_ID","value":"completed"}}`

Event checks sample the existing device directory once per second, without polling
hardware or running inference. Missing/stale readings cannot meet conditions. Durable
task completion receipts avoid relying on the lossy generic event bus. Instantaneous
device transitions while the service is stopped cannot be recovered. New event sources
belong in adapters; this version does not expose unauthenticated webhooks.

`long_term=true` uses a persistent Pi Goal with a concrete completion condition in
the prompt. Intermediate turn completion is not goal completion. Arbitrary processes
cannot be transparently checkpointed: save progress/artifacts and external job identity,
and inspect before continuing. Task state is not proof of external-world success.

Automatic subtasks must stay inside the parent's workspace and declared resources;
children cannot create recurring triggers, change other tasks or enable voice delivery.
Depth is limited to three and each definition has at most sixteen direct children.
These are trusted local agents with full local permissions. OS sandboxing is a TODO.
Semantic authorization remains part of the instructions and tool approval policy.

Resource identifiers are internal delegation details, not a second user consent step.
Foreground must resolve the authorized target through its owning capability before
creating work; for media, use `media_capabilities.target_bindings` and its exact account,
provider and `task_resource`. Never infer a provider from a generic target alias.
An existing user request authorizes its necessary scope, not unrelated destructive work.

`repair_task_run_resources(run_id, revision, resources, reason)` lets foreground correct
one execution's complete resource list using that original request. Pause and confirm
running work stopped first. Only queued, paused or waiting runs without an active turn
can change; launching, child, unknown and ended executions fail closed. Run revision is
checked, and before/after bindings plus the reason are persisted in `resource_repairs`
and exposed by execution trace (bounded to 32 corrections). The tool does not resume,
replay actions, change model selection, update future definitions or affect another run.
Future definitions use `update_task`; archived work needs a correctly scoped follow-up
that reconciles existing operation IDs. Background callers cannot repair their own grants.

## Controlled self scheduling

Foreground creation/update may record an explicit user grant on a reusable timed plan:

```json
{"self_schedule":{"allowed_triggers":[{"kind":"interval","seconds":86400}],
 "condition":"After all authorized historical items are covered and pending errors resolved, check new items daily."}}
```

No grant is inferred from prompt text. The default is no self scheduling. The plan must
have user authorization, a timed trigger and `auto_archive=false`. The grant lists
1..16 exact interval/date/cron choices and a bounded authorization condition; immediate
and event triggers are excluded. Children cannot grant or inherit this capability.
Revocation uses foreground `update_task(changes={"self_schedule":null})` with a fresh revision.
Grant/revocation and other edits that do not change the trigger preserve the existing
next occurrence; they do not create an extra execution.

An active provider turn uses `update_own_task_schedule(request_id, revision, trigger,
reason, evidence)`. The host binds identity to that execution, derives its definition
and accepts no task/run target. It checks both the execution's captured grant and the
current plan, the latest revision and a storage-level compare-and-swap. No prompt,
workspace, resources, model, permissions, notifications or current-run snapshot changes.
The agent determines whether the granted condition is met and supplies concrete evidence;
the host enforces the declared choices, not semantic proof that the objective succeeded.
Receipts mark evidence as executor-reported. Tool output, events and memory cannot grant
additional permission. Background `update_task` and management of other tasks stay denied.

Only enabled plans and running, identified provider turns can submit new changes. Any
foreground edit, pause/resume, termination or grant change invalidates an older run's
ability to reschedule, even if it reads the new revision. The next execution captures
the new authorization. An execution may make successive authorized changes using the
version returned by its last change. The currently running work is never restarted.

Each change commits the new schedule and an idempotent receipt in one host SQLite
transaction. UUID retries with identical arguments return the original receipt, including
after execution completion; changed arguments conflict. Retrying an older receipt cannot
restore its old schedule. In-memory scheduling is rebuilt from the current committed
definition. A matching schedule is a no-op error rather than moving its next occurrence;
new interval/cron occurrences are strictly future, and past dates are rejected.
The latest 20 receipts appear in execution trace with a truncation flag. All receipts
remain durable for idempotency and audit; they include before/after schedule, revisions,
identity, time, reason, evidence and the authorization condition.

Tests: `scripts/check.py --test test_task_self_schedule`. Explicit installed-service
acceptance: `scripts/verify_task_self_schedule.py --config config/config.toml`. It creates one
silent synthetic timed plan, verifies its real MCP change and unchanged run snapshot,
then archives it. It uses no physical devices and does not replay failed/unknown work.

## Management

Independent `tasks` MCP exposes create/list/get/update/run, schedule pause/resume,
execution pause/cancel/continue/supplement, terminate/archive and artifact listing.
`device` no longer owns task management. Web task cards and the Task panel use the same
capabilities; CLI examples:

```sh
pa --config config/config.toml tasks list
pa --config config/config.toml tasks models
pa --config config/config.toml tasks get '{"task_id":"ID"}'
pa --config config/config.toml tasks pause '{"task_id":"ID"}'
pa --config config/config.toml tasks supplement '{"run_id":"ID","text":"补充要求"}'
pa --config config/config.toml tasks workfiles
pa --config config/config.toml tasks workfiles '{"apply":true}'
```

Creation/manual runs require a stable UUID request_id; reuse it on uncertain transport
failure. A different payload with the same ID is rejected. Updates require the current
definition revision. Schedule pause leaves active work running; cancel_run affects only
one execution; terminate/archive stops the definition and children. Explicit continuation
is required for stopped or unknown work. Stop playback does not cancel background work.

## Persistence and delivery

Host SQLite owns definitions, next trigger times, occurrence IDs and execution snapshots.
APScheduler calendars are a rebuildable in-memory projection. Due occurrences are
persisted before dispatch. Recurrence coalesces within a configured lateness window;
missed or overlapping occurrences are recorded instead of replaying a backlog.
Unknown side effects are never automatically retried. Startup reads provider history
before reporting completed/failed/unknown work. A running external process is never
silently recreated. Project/resource lock names serialize conflicting background jobs;
foreground work is delegated rather than issuing duplicate operations.

The shared timeline receives one stable card per definition/run. Results update the card
rather than producing a new message on every progress update. Explicit voice delivery
obeys quiet hours, uses a bounded output-only connection in standby and does not send
microphone frames. Ordinary results remain silent. Voice delivery is best effort; no
exactly-once physical playback claim is made. Files are served only by authenticated
artifact routes, not a static workspace server.

## Working files

All contexts receive a managed `state/work/conversations/SESSION` or
`state/work/tasks/TASK/RUN` area with `scratch` and `artifacts`. Background shell temp
environment points to its scratch area. Project changes remain in the requested project.
This directory policy cannot compel arbitrary tools to obey it; it is not a filesystem
sandbox. Registered terminal scratch areas expire after seven days; active, paused or
unknown work is retained. Artifacts never expire under the scratch policy. Cleanup
rejects symlink traversal and does not scan user projects, secrets or model directories.

## Verification

Run `python scripts/check.py` for scheduling, idempotency, cancellation, event freshness,
model choice, concurrent resources, recovery, file boundaries and the existing suites.
`--group tasks --group execution` includes main-conversation defaults, custom recommendations, fixed
overrides/provenance, UUID idempotency, upgrade boundaries, live terminals, cancellation,
lost acknowledgments, restart recovery and late prior-turn events. Provider fixtures do
not establish real API model-switch success; perform explicit scratch-only live
acceptance before claiming deployment verification. No TV/camera interaction is needed.
Run `python scripts/verify_tasks.py --config config/config.toml` against the installed service
for a silent reminder, real Pi artifact, long-term Goal and cancellation. It writes
only to managed task areas and saves private evidence under `state/acceptance/tasks/`.
Do not count fake tests as provider/hardware acceptance. No camera or microphone capture
is needed to validate this plugin.

Task definitions, startup and ordinary progress are available in the task panel and
tool results, without separate chat cards. The notifications owner is the single task
feedback delivery path: typed results per execution/completion round and deduplicated
attention for waiting-user/unknown states. Attention is independent of the routine
completion preference; changing progress while waiting does not repeat a question.
Questions use their structured identity, never a match against task titles. The main
reply can acknowledge delegation without a second automatic “created/enabled” message.

### Result delivery ownership

`create_task(feedback_owner="background")` (default) hands both work and result notification
responsibility to the background. Use `feedback_owner="conversation"` only for immediate,
non-Goal work whose result the current main turn explicitly takes responsibility for.
The originating session/turn/input revision is captured by the server before asynchronous
model selection and persisted atomically with task creation. Clients cannot supply an
identity. This creation choice covers only the first execution/round; later occurrences,
continuations and other tasks are independent. `notify_completion` remains the on/off
policy and `notify_voice` the background voice preference, not ownership switches.

`get_task` includes `run.feedback`: owner, round, version, state and delivery receipt.
It is read-only: querying or inspecting a result never takes over responsibility.
Main may explicitly call `transfer_task_feedback(run_id, round, version, owner)` with
`owner="conversation"` to take responsibility, or `"background"` to hand it off. The
server checks current conversation availability, caller identity and the latest round/version.
Background callers cannot impersonate the main turn. Stale versions must be reread;
committed or delivered results return `transferred=false`, without recalling/reissuing them.

At completion, tasks freezes the delivery payload/owner and commits it before transport.
Conversation writes one stable result card, with task link and registered attachments,
immediately; voice uses the existing output path. No model-generated answer is needed
before delivery. Main can add interpretation, but should not repeat the result verbatim.
Turn completion/interruption, new input, voice exit and restart recovery release outstanding
ownership when delivery checks that turn. There is no elapsed-time ownership rule.

An RPC error/timeout does **not** imply failure to deliver: the same result identity is
retried until its stored receipt or explicit release is known. Inaccessible conversation
capability keeps a visible task-plugin health error, not an unverified opposite-channel
send. Once released, ordinary durable notifications take over. Uncertain speech is not
replayed; a silent inbox notice shares the existing card. Output receipts do not claim the
user heard/read anything. Delivery is bounded/concurrent and does not stall task dispatch.

Implementation: tasks owns `job_result_claims` / `job_result_feedback`; conversation owns
`chat_result_receipts` / turn lifecycle. `ConversationFeedbackRequest` is internal, absent
from IPC/MCP. The Pi adapter has no new protocol/prose acknowledgement heuristics.
`tests/test_task_feedback.py` covers handoffs, conflicts, query independence, terminal races,
turn exit, restart, lost replies, speech uncertainty and attachment projection without
hardware/model calls. Real voice timing is left to everyday evaluation.

Background-owned results enter the notifications outbox before completion acknowledgement;
conversation-owned results require their durable conversation receipt.
`notify_completion=false` keeps routine monitoring runs out of the chat/inbox; agents
explicitly publish new events through notifications MCP. Automatic failure notifications also respect this setting.
The notifications plugin owns quiet-hour deferral and transport recovery.

Execution results retain `started_at`, `finished_at`, and accumulated `duration_seconds`.
Timing starts on dispatch, includes waiting for answers, excludes queue, explicit pause
and unknown-state intervals, and remains fixed during notification delivery/retries.
Continuing a run adds its new execution segments. Old runs without measured starts
have no fabricated duration. Result notifications carry this value in origin metadata.
`create_task(notify_completion=False)` disables automatic end-of-run notifications,
including failure; `update_task(changes={"notify_completion": True})` changes future
run snapshots. `notify_voice` only adds spoken delivery when notifications are enabled.

A scheduled occurrence with state `missed` is displayed as “本次已跳过”: it never
started. New records retain `skip_reason` (`overlap` / `late`), blocking run IDs and
an explanatory progress message. Overlap does not queue a duplicate; recurring tasks
continue with their next scheduled occurrence. Older records without recorded reasons
remain generic. Terminal records never use a pending-execution UI placeholder.

User-facing wording distinguishes plan state from execution state. Pausing a plan
changes future triggers; pausing one execution does not pause that plan. Queued,
starting, waiting for user input, unconfirmed, failed and skipped records have their
own descriptions. An empty completed result is not displayed as pending. Result
notifications describe one execution, not completion of a recurring task. The
notification switch applies to future run snapshots and automatic result notices;
authorized event alerts remain separate. Duration is accumulated execution time,
not queue time or model-only inference latency.

Immediate, non-Goal one-shot work is categorized as `temporary`; scheduled/event/Goal
work is `planned`. One-shot non-Goal tasks (immediate/date) default `auto_archive=true`.
All terminal outcomes (completed/failed/canceled/missed) archive automatically, retaining
results, artifacts and pending notification delivery. Waiting/paused/unknown/running
work remains visible. Automatic archive never invokes cancel/stop; parents wait for
unfinished descendants. Startup reconciles already-ended one-shots, including records
created before this default. `auto_archive=false` is available for explicitly reusable
work; changing trigger/Goal type recomputes defaults unless the request overrides them.
Archived tasks are read-only; follow-up work uses a new task. Web has two tabs: temporary delegations first, then plans. Temporary defaults to
unfinished non-archived work; “包含归档” shows all temporary results. Plans default
to non-archived definitions, including paused and terminated plans; “包含归档”
also shows archived plans. Both checkboxes default off and retain independent states.
Tabs, checkbox and loaded item count share one compact toolbar row.
Plan detail actions are ordered “触发一次”, “暂停计划/恢复计划”, “终止计划”.
The last action uses the existing archive operation: stop future triggers, cancel
unfinished executions/descendants, and retain results as archived. Its first click
arms an inline “确认终止” button; only a second click submits. Confirmation resets
on blur, outside pointer, Escape, panel close, task change or after five seconds.
There is no separate archive menu action in Web.

Task and run browsing use cursor pages (`before`, default `limit=20`, maximum 50).
`list_tasks` accepts `view=temporary|planned|all` and a server-side `query`;
Both temporary and planned accept `include_archived` (boolean, default false).
The planned API also supports `active_only=true` for enabled plans with future
triggers, event listening or unfinished executions. Filters are applied in SQLite and bound into the cursor, including
search and state, so filter changes start a fresh page. Run entries are concise summaries. `get_task` returns full run pages and a
separate bounded `active_runs` snapshot, so an older unfinished execution remains
visible after newer skipped occurrences. Pass each response's `before` unchanged
to continue that same query. Web scrolls to fetch older pages; expanding history
permits loading more executions. Polling never refetches all previously loaded
pages, and inactive/hidden views pause polling. The host accepts up to 50 `known`
IDs and returns matching off-page definitions in `updates` plus current execution
summaries, or filtered-out IDs in `removed`. Web prioritizes older unfinished work
in that bounded set so it can complete or archive without a stale list row.
No records are deleted by paging.

Each model run uses its registered `scratch/` as actual working directory and temp
directory; `artifacts/` retains deliverables. The target `workspace` is still included
explicitly for authorized project operations, with existing configured permissions.
Pi execution conversations are archived after each confirmed terminal execution,
independently of whether the recurring plan remains enabled or a definition is archived.
`job_finalizations` stores a per-run/continuation receipt; its owned worker restores
unfinished cleanup after restart and retries archive failures with backoff. It verifies
provider inactivity, a terminal latest turn and no live commands. Waiting/paused/unknown
runs stay available. An explicit continuation restores an archived thread; background
archiving never launches another model turn or changes delivery/read receipts. Existing
ended project executions are swept into this policy; unrelated/foreground threads are
not enumerated. Artifacts and execution records remain available in Web.


## Durable event-driven AI work

This extends `tasks`; there is no separate automation plugin or workflow language.
Business interpretation remains in the authorized AI objective. The existing `events`
capability supplies facts; task inboxes store only references and execution state.
Enable `events` before creating a durable subscription. Existing calendars, device
threshold triggers and task-completion triggers retain their semantics.

```json
{
  "kind": "event",
  "event": {
    "source": "records",
    "source_id": "living_presence",
    "types": ["presence.changed"],
    "subject": "living_room",
    "merge_seconds": 3,
    "cooldown_seconds": 30,
    "max_age_seconds": 300,
    "capacity": 128
  }
}
```

Use `create_task` with this trigger and a user-authorized objective, such as reviewing
current conditions before deciding whether action is needed. `types` is required
(1–16 exact type names); source_id and subject are optional exact filters. These are
routing selectors, not a device-condition expression language. Discover real source
identities/types through events MCP. This example does not install a presence sensor.
Subscriptions default to silent completion (`notify_completion=false`) unless explicitly
chosen otherwise; meaningful alerts use the existing notifications capability.

### Receipt, batching and lifecycle

- Creating, editing or resuming starts at the recorder's current change cursor; it
  never replays earlier history. Pausing/editing/archiving clears pending references
  with recorded reasons. Already assigned runs retain their immutable snapshot;
  pause does not cancel them, while archive/terminate uses existing cancellation.
- Each page of changes, inbox decisions and resulting run insertion share one host
  SQLite transaction. Stable occurrence IDs and the cursor prevent duplicate automatic
  dispatch after a crash or lost hint. The event database is read through `EventRequest`,
  never directly. The lossy bus accelerates reads; a one-second fallback reads the
  durable log. This is not hardware polling. Recorder failure backs off five seconds
  per subscription and leaves unrelated tasks/voice running.
- Only new records labeled `observation` at revision 1 trigger work. Baselines, gaps,
  manual statements, summaries, corrections and deletions do not. Corrections/deletions
  invalidate waiting references; queued runs reread current revisions before dispatch.
  Changes arriving after execution starts do not undo physical effects; the AI must
  query current facts/state before acting.
- A batch closes after `merge_seconds` from its first receipt (default 3, range 0–60),
  not after endless trailing quiet. `cooldown_seconds` is the minimum dispatch spacing
  (default 30, range 0–86400). One active/paused/waiting/unknown execution blocks another
  for that task. New events accumulate independently rather than steering a running turn.
- Event lifetime uses occurrence time or observation-interval end: `max_age_seconds`
  defaults to 300, range 1–86400; merge must be shorter. More than five seconds in the
  future is rejected as clock uncertainty. Expired pending/queued events do not launch
  work. Explicit continuation remains a user decision and keeps its original context.
- Each inbox holds at most `capacity` pending references (default 128, range 1–256),
  each execution consumes at most 32, and at most 64 subscriptions may be enabled.
  When full, newer arrivals are counted as overflow; accepted pending work is retained.
  Counts persist; the last 100 decisions are retained, with 20 shown by inbox queries.
  No raw sensor payload is duplicated. An overflow or gap means incomplete evidence.
- Event-log cursor regression is reported as unhealthy; it is never silently reset to
  replay old actions. Explicit pause/resume establishes a new tail after investigation.

### AI context and tracing

An event execution snapshots `trigger_context`: subscription, referenced event IDs and
change cursors, observation times, batch count and previous run identity when present.
Dispatch adds `valid_events` and an invalidated count. The Pi adapter supplies this
as data beside the original objective, with task/run IDs. AI can use existing events,
memory, devices and tasks MCPs to fetch facts, examine previous outcomes and decide
what to do. New events do not expand authorization, change model selection, switch
fixed voice routes, or authorize child recurring tasks.

`get_task` includes the bounded inbox and run contexts. `get_task_event_inbox(task_id)`
shows cursor/health, pending references and received/assigned/expired/overflow/
invalidated/lifecycle-discard counters. `get_task_execution_trace(run_id, after, limit)`
links trigger references to run state, provider thread/turn and tool metadata. CLI:

```sh
pa --config config/config.toml tasks event_inbox '{"task_id":"..."}'
pa --config config/config.toml tasks trace '{"run_id":"...","limit":50}'
```

Trace stores at most 500 entries per run, paged 50 by default, maximum 100. Repeated
provider call IDs/phases deduplicate; truncation is explicit. It retains server/tool,
phase/status and reported device/operation IDs, never complete arguments, shell commands
or tool output. A completed tool call is provider evidence, not proof that hardware
changed. Native operation IDs can be checked through devices MCP; external tools may
lack this guarantee. Unknown executions are reconciled or explicitly continued, never
silently recreated. Raw provider-specific parsing stays inside `core/pi`.

The existing task panel displays the merged trigger count and current pending count;
it keeps its existing paging and controls. Complete trace inspection is MCP/CLI-first.

Tests: `scripts/check.py --group tasks --group events --group execution`. Coverage includes
transaction rollback, duplicate reports, real runtime/SQLite restart, pagination,
coalescing, overflow, expiration, mutation invalidation, paused subscriptions, stale
in-flight reads, unknown executions, and trace redaction/limits. Simulated sensor events
are not evidence that a physical presence sensor has been connected or validated.


## Completing an authorized plan from its own execution

Foreground creation/update may grant `self_completion={"condition":"concrete user-authorized stopping criteria"}`
to a reusable user-authorized timed plan. Children cannot grant themselves this authority.
`complete_own_task(request_id, revision, reason, evidence)` derives the target from its
bound active provider run; it accepts no arbitrary task identity. Both the captured and
current grant must exist, and current revision must match the run or its last controlled
self-change. User edits/revocation/pause invalidate older authority. Archive and UUID
receipt commit together; retries return the receipt without another transition.

Completion stops future scheduling, preserving the current execution for its final
report. It does not call the general archive action that cancels active work. The existing
self-change audit records `action=complete_self` and executor-reported evidence; the task
framework does not independently prove a domain goal. For media watches, verify the
known final episode range, requested storage level and absence of unresolved operations;
no search hits is not completion evidence. Unknown totals require continued tracking or
user clarification. Tests live in `test_task_self_schedule`.

## Execution assessment and evidence

Optional `execution.assessment` supplies bounded task facts for review:

```json
{
  "acceptance": "Read back the destination and verify the intended objects",
  "procedure": "Existing transfer tool; search candidates still need identity checks",
  "uncertainties": ["Two candidates disagree about the object's identity"],
  "user_preference": "Exact explicit model/speed preference, only if the user stated one",
  "prior_run_id": "related execution ID when retrying"
}
```

Omit absent fields rather than inventing evidence. `uncertainties` allows up to four
300-character statements; other text fields are at most 500 characters. The framework
reads the referenced run itself and captures its state, selected model/effort and a
bounded, explicitly labelled executor summary. Background references must belong to
the parent's task or its children and the same workspace. This is reported evidence,
not independent verification of external effects. Source modules continue owning tools
and their operation receipts; there is no media-specific routing logic.

Caller-supplied facts remain assistant statements. Free-form `user_preference` is
unverified; field provenance provides conservative selection locks, not authority.
Supplying fake `source` or `user_verified` fields inside assessment is rejected.

The independent `model_review` question sees task facts and full legal model/effort
pairs, but not the baseline recommendation/reason as separate advice. It does not
create a universal scale between different providers' native efforts. Candidates come
from named recommendations, main defaults and the explicitly selected model, filtered by the live
catalog and instance constraints. The task prompt may itself contain preferences, so
this is not a claim of a statistically blinded experiment.

In shadow mode, one owned worker and a 16-entry queue bound review work; launch never waits for it.
The worker has a four-second total deadline and keeps the existing three-second Jev
active deadline by default. Saturation, unavailable support, fixed choices and errors
produce explicit skip/fallback records. Restart marks queued/running reviews interrupted
without replay. Identical facts/candidates use a stable digest to reuse existing decision
receipts; cache eviction can cause a later review, not an execution replay. No daily
call cap is added. Credentials/raw media remain prohibited and inputs retain the
16-KiB decisions limit; oversized inputs fall back, rather than silently losing facts.

Inspect `run.data.model_review` using task get or `get_task_execution_trace`. It includes
initial selection/source, candidate recommendation, receipt ID, disagreement, timing and
adoption state (`applied=false` in shadow). Original facts live in `spec.execution.assessment` and
`spec.execution.selection.prior_execution`. Compare these alongside model-selection
segments, tool trace, duration and verified task outcomes. Disagreement is not evidence
that Jev was correct; provider-reported completion is not a quality label. Active correction is explicitly opt-in; deployment of a low threshold is not evidence
of improved accuracy. Explicit handoffs retain their evidence and checkpoint checks.


Decision adoption now records its own observation after model policy validation.
The decisions service owns the global threshold (0.6); model-review override is 0.2
in the example/instance configuration. Raw provider confidence is not measured task
success. Selection receipts preserve the per-call decision ID for later execution
comparison; event-inbox stale decisions are rejected explicitly before dispatch.

### Web execution timeline

Each run has a collapsed **执行过程** disclosure. It reads the existing trace through
`GET /api/tasks/{run_id}/trace?after=0&limit=100` (same Web authentication). Only activity
metadata, ownership IDs, pagination and coverage flags cross this route; prompts, model
selection evidence, provider thread IDs and tool bodies are not included. Call start/end
records are merged by `call_id`, retaining parallel calls and turn-specific identities.
The displayed duration is the interval between received records, not external-device
verification. A completed run never converts a missing tool-end record into success.

One selected trace loads lazily, drains ordered bounded pages, then checks for new records
every three seconds while unfinished and visible. Collapsing/closing or switching tasks
aborts in-flight requests; history stays inspectable without background polling. Empty
and truncated histories are explicit. No older progress text or hidden reasoning is
reconstructed. Existing tasks trace storage and its 500-event limit are unchanged.

A headless task installation can add `tasks` and `notifications` to slim and configure
`notifications.channels = ["inbox"]`, `voice_enabled = false`. No conversation, device
or audio object is required. Install the tasks extra explicitly for APScheduler.
Task plans own scheduling, goals, upgrades and feedback; each attempt references the
public execution receipt through `data.execution_id`, also exposed as `execution` by get.
Continuation and explicit handoffs create new attempt receipts while retaining the same
work allowance and existing verified operation checkpoints.

## 工具参数与部分更新

`tool_contracts.py` 维护 execution、trigger、模型升级检查点和 update changes 的结构。
MCP 声明、Pydantic 输入校验及任务运行时共享 Schema；通用校验使用 `mcp/contracts.py`。
动态模型目录、资源和授权规则
仍由任务模块检查。验证失败在创建/更新前结束，返回 `ToolFailure` 的字段路径及修正条件。

`update_task.changes` 是递归字段补丁：未传字段保留、null 清除可选项、列表整体替换。
改变 trigger/execution.kind 时重建对应变体，避免保留另一种变体的字段；组合后重新校验。
用户明确选型保护、revision 冲突检查和运行中任务快照不变。仅修改标题或触发器不重新
决定执行模型。示例：`{"execution":{"assessment":{"uncertainties":["待核实文件"]}}}`
会保留 execution.reason 和 assessment.acceptance。
明确改选 recommendation 时，会清除由旧推荐解析出的 model（同时显式提交 model 时仍
要求二者一致）；直接更换 model 时移除旧推荐标签。原有用户选型只有明确的 user 来源
才能替换，不把补丁中省略来源视为新的授权。未指定或 null 的模型/思考参数继续使用
现有默认选择语义，不因新增 Schema 而要求调用者必须选型。


模型请求失败保留 `failure` 结构化原因，任务详情与主对话使用同一展示组件。
操作者的 `continue` 可附带 `retry_request_id`（稳定 UUID）和 `retry_of`（失败 turn）；
服务端验证最近失败检查点、待确认操作及任务状态，保存幂等记录后沿用原执行继续。
`PiJobs` 向 Pi 提交同一个恢复目标，避免检查后目标变化。该入口不向模型新增权限，
不重放原始任务，也不重置用量。未知结果须先核对，归档任务不能用恢复按钮重新启动。
