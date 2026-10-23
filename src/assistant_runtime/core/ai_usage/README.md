# AI usage: metering and budgets

`ai_usage` owns AI-service admission, durable accounting, limits and statistics. It does not authorize tools, manage task goals or implement an agent loop. Pi owns execution and protocol transport; tasks owns identity, schedules and result-delivery rights; models owns the immutable model catalog. This is a mandatory built-in metering dependency, not `Manifest.critical=True`: loss of metering stops new paid dispatch, not device/event/web operation.

## Defaults and identity

One work includes a user request, its delegated tasks and auxiliary calls. The host binds internal MCP processes to their actual Pi session; the IPC resolves its active turn and usage identity, rather than guessing from whichever foreground task is active. Background identities come from the server-bound run. Separate user requests, scheduled occurrences and independent maintenance/extraction jobs get separate work IDs. Child creation, continuation, fallback, model change and restart cannot replenish a work allowance.

Memory owns one deterministic work identity per durable source batch and epoch. Its user/project extraction and reconciliation calls share that allowance across preemption, retries and process restart; a model callback never creates a new allowance inside an existing batch.

The default is **100 external AI requests per work**, warning at 50. The warning is stored and displayed; denial of request 101 creates one durable attention event and pauses related task plans. Devices/file/cloud operations are not AI requests. Normal HTTP deadlines, output capacities, task depth/concurrency and task goal rules retain their existing owners.

Every inference attempt, retry, fallback, Jev judgment, paid search/fetch and actual compaction request consumes one admission. Model metadata health/warmup is not inference. Cache hits and local rejections consume none. Coalesced search uses the initiating work and one physical receipt, with all consumers retaining the same upstream correlation; it is not charged again per waiter. A reserved request whose sending cannot be established counts conservatively. These are framework request counts, not a claim that every attempt was billed by the supplier.

Main/subtask model traffic, models MCP, memory extraction, Jev native calls, and DeepSeek search/fetch all pass through admission. Each adapter retains its own existing retry/fallback policy; there is no additional retry loop here. A usage denial cannot authorize provider fallback or replay. A lost execution stream remains subject to Pi/tasks reconciliation; missing token telemetry alone does not make a verified business action unknown.

## Configuration

Include `ai-usage.toml` from the entry TOML. `[ai_usage]` owns its policy; `[model_api.execution_limits]` is removed. Configuration changes apply on restart and preserve cumulative use. A changed policy is reevaluated without clearing counts; a paused plan still requires explicit recovery. See `config.example/ai-usage.toml`.

`max_requests=100`, `warning_fraction=0.5`. Optional `work_limits` include `max_input_tokens`, `max_output_tokens`, `max_compactions`, `max_seconds`, `max_cost_cny`, `concurrency`, `requests_per_second`; absent means disabled. There is no default daily quota, token cap, compaction cap or overall execution timer. `max_seconds`, when explicitly enabled, denies subsequent admissions after elapsed time; it does not kill an in-flight operation.

Additional `[[ai_usage.rules]]` have a unique `id`, scope `work`, `plan` or `global`, optional `match={work_id, task_id, service, provider, model}`, and `limits`. All matching rules apply; they do not override one another. `plan`/`global` require `window_seconds`, no greater than detail retention. `provider` identifies the registered route, `model` the upstream name. Rate/concurrency denials are transient and do not permanently pause a plan; consumption-limit denials are durable.

Token ceilings reserve the model's declared context capacity and requested output capacity before sending. Unknown input/output bounds fail closed only when the corresponding budget is enabled. Reported input includes cached tokens. Missing telemetry keeps its reservation rather than inventing zero. Therefore an opt-in token/currency budget can be conservative. Prices are explicit CNY per million input/output tokens and/or per request, matched by provider and optional upstream model. Cache discounts are not guessed; configured input price should be an upper bound. Local cost is always labeled **estimated**, with unknown calls counted separately; supplier statements and other applications sharing a key are outside this ledger. An RMB budget without sufficient pricing/reservation evidence refuses the affected request.

## Storage and failure behavior

Private `state/ai-usage/usage.sqlite3` uses WAL, full synchronous transactions and one dedicated, bounded database worker. Reserve is durable before dispatch, settlement before reporting an observed completion. The event bus is never an admission gate. Call IDs settle idempotently; cancellation cannot undo an unknown submission. Restart recovers outstanding submissions as unknown, retaining their count and reservation. Counters, bindings, allowance adjustments and unresolved/blocked works persist across detail cleanup.
Unknown compaction calls remain counted with their reservations (`unknown_compaction_requests`),
but are excluded from `unknown_execution_requests`: a missing context summary does not imply an
unknown tool action. Pi owns summary recovery; metering never erases an uncertain cost or refills
an allowance to permit continuation. Unknown ordinary execution streams retain their stop rule.

Metadata includes work/caller/task/version/parent call, service/provider/model, attempts, timestamps, upstream ID/status, known/unknown token use, estimated cost, policy/price revision and denial reason. Detailed receipts default to 90 days; older eligible completed/rejected details become compact work counters and daily aggregates. Daily aggregates default to 365 days. Active/unknown or blocked receipts are retained. Compaction and cached input are included rather than concealed in execution summaries.

Body recording is off by default. Explicit diagnostic recording has a 7-day TTL and a 32 KiB cap per body, private file permissions and sensitive-field/common credential redaction. Images/audio/video and authorization fields are excluded. Treat enabled text capture as private diagnostic data: automatic redaction is not a guarantee that arbitrary secrets embedded in natural language can be recognized. Routine CLI/MCP queries never export bodies. No credentials, prompts or raw upstream error strings are written to ordinary logs.

Storage/queue failure denies new affected AI calls. Queue bounds and a dedicated worker preserve event-loop responsiveness. Diagnostics and a deduplicated health notice explain the failure; no paid recovery requests are sent. Already-sent tool actions settle according to their actual evidence. This controls registered framework routes, not unrestricted shell processes or use of the same supplier key elsewhere.

## Query and recovery

```
python -m assistant_runtime.app.cli --config config/config.toml ai-usage check
python -m assistant_runtime.app.cli --config config/config.toml ai-usage status
python -m assistant_runtime.app.cli --config config/config.toml ai-usage calls '{"limit":20}'
python -m assistant_runtime.app.cli --config config/config.toml ai-usage stats '{"group_by":"provider"}'
python -m assistant_runtime.app.cli --config config/config.toml ai-usage stats '{"group_by":"day"}'
python -m assistant_runtime.app.cli --config config/config.toml ai-usage summary '{"work_id":"WORK_ID"}'
python -m assistant_runtime.app.cli --config config/config.toml ai-usage extend '{"work_id":"WORK_ID","requests":100,"reason":"Explicit user approval"}'
```

Extension retains prior use and is audited; it cannot override an unrelated global/token/currency rule. Resolve unknown business operations before resuming execution. A plan stays paused until explicitly resumed using tasks. No MCP tool can extend an allowance or change policy. Model-bound IPC cannot invoke the maintenance extension path. Queries are read-only; a background caller sees only its own task's receipts and shared work budget.

Task details and foreground state display use, warning and blocking. An attention event uses existing conversation ownership when claimed, otherwise the notification inbox; task attention delivery does not duplicate it.

## Acceptance

`check.py --group models --group execution --group decisions --group search --group tasks --group web` covers the adjacent contracts; full checks remain required before deployment. `test_ai_usage` verifies exact 50/100/101 behavior, concurrent reservation, durable identities, cancellation/restart, missing telemetry, optional limits, failure closure, privacy and retention. Real Pi subprocess tests exercise file/tools/MCP, native compaction, shared fallback budgets and mixed main/child identity against synthetic local endpoints. No automatic check sends a paid supplier request or restarts production.
