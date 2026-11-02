# Public conversations

`conversation` is a core plugin requiring `workfiles` and `execution`. It owns durable
logical sessions, entrance bindings, ordered input, messages, media references and result
delivery. Optional assistant presentation lives in `core/speech`; slim never imports it.

`ConversationRequest(action, arguments)` supports:

| Action | Arguments |
| --- | --- |
| create / list | title / before, limit |
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
work. A session has one drain worker and at most one active turn. Waiting input is bounded
by `execution.queue_size`; native dispatch shares execution admission and AI usage limits
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

Web stays bound to `web`; CLI bindings use `cli:<operator>`. A client's live selection
is local to that client. Model sessions are adapter-owned views of one shared Pi client,
with separate locks, callbacks, context, questions and MCP refresh state. Pi owns context
compression. Stable application identity and retained history survive worker replacement.

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
