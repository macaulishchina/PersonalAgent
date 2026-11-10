# Public conversations

Since 0.4.17, `snapshot`, `snapshot_receipt`, and `snapshot_inputs` expose immutable
complete checkpoints and paged source-input coverage. Capture never waits for the
active round or invokes a model; unproven legacy coverage is explicit. See the
[task context snapshot contract](../../../../docs/guides/task-context-snapshots.md).

Since 0.4.14, submit optionally accepts `expected_model={model, revision}` from the
applied public model directory. It guards acceptance and final dispatch without pinning
the session; old submissions keep existing defaults. Receipt/receipts optionally accept
`include_execution=true` for durable route, effort and execution linkage. Omitted fields
preserve the old response shape. See [public model routes](../../../../docs/guides/model-routes.md).

`conversation` is a core plugin requiring `workfiles` and `execution`. It owns durable
logical sessions, entrance bindings, ordered input, messages, media references and result
delivery. Optional assistant presentation lives in `core/speech`; slim never imports it.

Web presentation uses recorded message metadata: `presentation.phase` separates tools /
commentary from final answers, `turn_status` records turn completion, and `body` identifies
a complete body or an excerpt. Result references alone never request an extra result viewer.
Excerpt ranges address the corresponding message inside the retained final text, so reading
one message cannot duplicate another final message. The Web replaces its preview in place,
renders Markdown once, and keeps raw JSON inspection inside tool details. Consecutive process
entries remain open during execution and default closed when the turn settles; manual reopening
survives later updates. Final answers, media and failure notices remain visible outside that fold.

Foreground agents decide whether to delegate. A request that can be completed with the
tools and context of the current turn stays there by default, including multi-step lookup
and synthesis. Independent work, recurring work and explicit background requests use tasks.
Successful one-shot agent results delivered back to the conversation appear as replies with
their body and attachments visible. The link to the task is secondary. Scheduled results and
operational updates retain the compact report presentation. The existing feedback owner
determines which route delivers the result.

Web media presentation is scoped to the recorded reply turn. Tool and answer references
share one gallery after the reply text, deduplicated by registered ID; while running,
received media remains visible. An intervening task notification does not steal the
foreground gallery. Late media, failure and history reload use the same projection.
Original tool attachments, message text, files and permissions are unchanged; the UI
does not copy evidence onto final messages or guess associations from phrases such as
“见附件”. User uploads and task-report media retain their own presentation and ownership.

Creation receipts are durable and isolated by the authenticated caller. The optional
`policy` object accepts an explicit `tools` allowlist, additional `instructions`, and
`capture_memory` (default true). Policy restrictions apply to fresh and resumed turns;
false memory capture excludes this session from automatic global transcript capture.

`ConversationRequest(action, arguments)` supports:

| Action | Arguments |
| --- | --- |
| create / list | title, optional request_id(UUID), policy / before, limit |
| receipt / receipts | session_id, request_id / request_ids (at most 100) |
| open | operator, optional session_id (CLI remembered selection) |
| history / events / state / queue | session_id; optional before / cursor |
| submit | session_id, request_id (UUID), text, optional attachments |
| supplement | session_id, request_id, text |
| answer | session_id, request_id, question_id, text |
| cancel | session_id, optional request_id (default active request only) |
| edit_queued | session_id, request_id, revision, text |
| remove_queued | session_id, request_id, revision |
| reorder_queue | session_id, request_ids, expected_order |

Request IDs are globally deduplicated; reuse with different contents or another session
is rejected. Ordinary submissions persist before acknowledgment and never steer active
work. A session has one drain worker and at most one active turn. Waiting input is not length-bounded, and depends
only on available storage and caller lifetimes; native dispatch shares execution admission and AI usage limits
with direct execution and tasks. Supplement/answer/cancel validate the current request;
answers additionally validate the question identity. Explicit inputs remain in history.

Queue projections include the message identity, text, attachment references and a payload
revision. Editing/removal accepts only still-queued inputs with the observed revision;
it cannot mutate or cancel an input that has started preparing or executing. Editing
keeps attachments, notification context and the original submission fingerprint, so a
lost-ack retry acknowledges the existing input rather than creating or replaying work.
Removal records cancellation for audit. Reordering requires the complete observed pending
order and atomically permutes only that session's pending ordinal slots. The drain rereads
the selected input after waiting for an active turn, so it uses the latest order and text.
At dispatch, the existing user message keeps its identity but receives its sending status
and timeline ordinal before provider events, so history follows actual execution order.

`transcripts.completed_inputs()` exposes bounded user evidence from completed queued
turns across all sessions, including explicit supplements/answers. Memory owns capture
policy and jobs; conversation owns source identity and history. Copied transcript evidence
has a unique message ID and session/request/turn/entrance provenance. Assistant and task
reports, cancelled inputs and uncertain executions are excluded. See [memory](../memory/README.md).

The additive `conversation_views` resource lets trusted declared entrances open a
session-bound view after authenticating and authorizing their client. It does not
alter another entrance's selection. History, events, uploads, attachment reads and
recorded result-reference lookup stay within the view. The ordinary `conversation`
resource keeps its original behavior. Web-owned shortcut grants are documented in
[Web conversation links](../../plugins/web/README.md).
The additive `ConversationAccessRequest` contract lets a trusted authenticated
entrance approve only its bound Web access requests. Approval/login/expiry and
access shutdown remain Web-owned; none changes conversation execution policy or
cancels accepted work. The conversation/Pi request protocol remains unchanged.

Web stays bound to `web`; CLI bindings use `cli:<operator>`. A client's live selection
is local to that client. Model sessions are adapter-owned views of one shared Pi client,
with separate locks, callbacks, context, questions and MCP refresh state. Pi owns context
compression. Stable application identity and retained history survive worker replacement.
`state.context_compacting` reflects the adapter's current session state. The Web shows
“正在整理上下文…” in the existing response indicator, including preflight and reconnect;
it does not create a chat message or infer progress from elapsed time. Completion,
cancellation and worker disconnection clear the transient state.

Task feedback captures source session and binding before dispatch. Text reports remain
deliverable after the initiating turn finishes; voice retains its stricter live binding
checks. Task notifications use persisted origin, never the currently selected entrance.
Attachment reads and submissions validate session ownership, including legacy references.

The additive session-directory migration backs up the SQLite database, runs in a transaction
and stops on ambiguous legacy main identity. It never resets Pi or drops legacy tables.
Startup marks queued inputs interrupted and uncertain running inputs unknown; neither is
replayed. Events use a bounded retained cursor log; an expired cursor requires a history reset.

Tests: `test_conversation_sessions`, `test_conversation_native`, `test_conversation`,
`test_task_feedback`, `test_web`, and existing Pi/execution/usage suites. Native tests use
real Pi processes with a local synthetic model; they do not establish provider acceptance.
