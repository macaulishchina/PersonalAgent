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

Request IDs are globally deduplicated; reuse with different contents or another session
is rejected. Ordinary submissions persist before acknowledgment and never steer active
work. A session has one drain worker and at most one active turn. Waiting input is bounded
by `execution.queue_size`; native dispatch shares execution admission and AI usage limits
with direct execution and tasks. Supplement/answer/cancel validate the current request;
answers additionally validate the question identity. Explicit inputs remain in history.

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
