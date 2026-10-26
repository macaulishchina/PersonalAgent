# Memory

Memory uses upstream Mem0, local FastEmbed and the configured `model_api.memory` binding.
The plugin owns jobs, capture policy, revision/tombstone metadata and a ready recall snapshot.
Conversation owns transcript evidence. There is no separate extraction agent or vector store.

## Operator debugging

The configuration page's collapsed memory debug panel lists the latest 20 saved records,
or matches a keyword literally in their title/text (case-insensitive). It uses the memory
owner's ready snapshot through the authenticated read-only `query` configuration action.
Scope filtering excludes other workspaces and tombstones. Results include ID, revision,
time and source evidence; browsing does not call models, change configuration or extract
new memories. The snapshot is refreshed after successful memory writes/capture. Unready
memory reports an explicit diagnostic rather than an empty successful result.

## Automatic capture

- Initialize a persisted chat cutoff once at installation/first enable; do not backfill old
  history. Keep it across restart. Changing auto-capture policy moves the cutoff at the
  configuration boundary; rollback restores it. Disabled input is not later backfilled.
- Poll completed user inputs from every session, including supplement/answer messages.
  Exclude assistant/tool/task output, attachments, pending, interrupted and unknown turns.
- Group by session and request, at most 64 messages and 32,000 characters per batch.
  Persist attributed transcript copies, job and cursor in one SQLite transaction. The
  unique source message ID prevents duplicate enqueue after retries or process restart.
  Legacy voice input shares the transcript cursor and cannot re-enqueue copied chat input.
- Retain session ID, message ID, request ID, turn ID, message entrance and original request
  entrance in validated fact evidence. Supplements/answers are marked `operator`; their
  request entrance identifies the originating Web/CLI turn. User memories are shared in
  the instance; project memories use the workspace captured with the batch.
- Dispatch only when all conversation backends and queues are idle, as well as the voice
  foreground. Each loop sleeps five seconds after work. Preemption and failed operations
  reuse the durable job/work allowance; Mem0 completed capture receipts avoid re-extraction
  when the parent restarts before acknowledging completion. A failed/incomplete operation
  may retry; this is not an exactly-once guarantee for a remote inference interrupted before
  its result was recorded. Do not advertise a failure as completed extraction.
- Existing retry backoff (30 to 300 seconds, at most five failed attempts), source validation,
  stale-job rejection and deletion tombstones remain in force. Manual changes invalidate
  older jobs and uncollected input. Worker failures surface through plugin health.

## Targeted acceptance

Run `test_memory`, `test_mem0`, and `test_memory_conversations` with `src` and `tests` on
`PYTHONPATH`. Set `PA_TEST_MEMORY_CACHE` to the prepared local embedding cache. Without it,
real-engine tests explicitly skip. `test_memory_conversations` covers atomic rollback,
provenance, cancelled/unknown input, bounded batches, policy rollback and admission while
another CLI session is busy. Its native case uses HTTP Web, CLI IPC, real Pi and Mem0 with
synthetic model responses in an isolated instance, including restart before enqueue and
restart after Mem0 commit. It makes no real-provider acceptance claim.
