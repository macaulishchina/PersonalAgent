# Notifications

Independent durable inbox and per-channel outbox. Enable `notifications` in `plugins`;
`tasks` depends on it. No separate broker, paid API or phone push service is installed.
The host's SQLite database owns notifications independently of Codex thread/session IDs.

```toml
[notifications]
channels = ["chat", "context"]
voice_enabled = true
voice_spacing_seconds = 60
max_attempts = 5
retry_seconds = 5
delivery_timeout = 45
max_notifications = 10000
```

`publish_notification` on the independent `notifications` MCP accepts a stable event key,
title, evidence/body, optional HTTP(S) source URL, normal/important priority, optional
Unix expiry and explicit voice request. Priority never bypasses quiet hours. Background
producer identity comes from its task execution, not model-supplied labels. Dedupe is
scoped to the task definition so repeated checks across runs see one notification.
Different content with the same key fails; corrections use notification ID/revision.
Main-session user corrections may update any notification; background agents may only
correct their own producer's notices and cannot mark them read or retry playback.

Successful publication means the inbox and channel jobs committed in one transaction.
A producer advances its monitoring cursor only after that acknowledgement; retries
reuse the same event key. Do not use run ID, current time or an LLM summary as event
identity. Quiet monitoring uses task `notify_completion=false`, then explicitly publishes
only meaningful new events. This also suppresses automatic failure notifications; explicit event notifications remain available. Task result origin metadata includes measured duration.
No website collector or specific monitoring job is installed by this plugin.

## Purpose and a single feedback path

Every new notification has an explicit `kind`: `progress`, `result`, or `attention`.
The notification owner applies the same policy to every producer; it does not parse
business titles, guess success, or ask a model whether two messages mean the same thing.

| Kind | Durable history | Unread reminder | Chat/context/voice |
| --- | --- | --- | --- |
| progress | Yes, including corrections | No | None |
| result | Yes | Yes | Configured channels; voice only when explicitly requested |
| attention | Yes | Yes | Configured channels; voice only when explicitly requested |

A progress record is not a user read acknowledgement: its read revision is untouched.
It remains visible under All/group history. Progress cannot request important priority
or speech; use attention when a fact needs interruption. `update_notification` preserves
kind when omitted. A progress record may become a result/attention using its same ID and
optimistic revision; retries do not create another notice. An already interrupting
record cannot be demoted to silent progress. Untyped historical records retain their
original reminder behavior; this change does not rewrite old history or replay it.

Task definitions and ordinary running/pause progress stay in task storage and the task
panel, not separate chat cards. Tasks commits a result owner per execution/completion round.
Background-owned results use this outbox; conversation-owned results use the conversation's
internal delivery capability and stable result card, without an unread notification.
A released turn returns the result here; uncertain speech creates a silent notice and
reuses the existing result card. Delivery timeouts alone never transfer ownership.
Both projections use the same execution group; progress queries change neither ownership
nor read state. Tasks publishes one background result per completion round using its durable key;
continuation starts a new round. Waiting-for-user/unknown states publish attention,
even with routine completion notices disabled, deduplicated by run/round/state and the
structured question request. Progress text revisions do not repeat that request.
Execution-ended remains distinct from achieving the user's objective.

Producers own classification. A normal suboperation result may set
`report_with_task=true` (result only, no important priority or voice). The notification
owner derives coverage from the bound run: if execution has not ended and its completion
report is still pending, the event is saved as progress. With execution already ended
(even before its final report is queued), or completion reports disabled/already committed,
the result remains an unread result. This is generic across producers; explicit progress
always stays silent. The first publication's effective kind is retained on retries,
even if the run later finishes or a different run retries the same source event.
Standalone results remain result; failures/uncertainty requiring awareness are attention. Media uses these same generic types, without notification-side media rules.
Late suboperation results stay in that execution's group and do not declare the
overall goal complete or overwrite an execution report. A new important fact must be
published as attention; correcting the same fact uses its existing ID/revision.

Chat is an idempotent projection keyed by notification ID and conversation session,
with a reference to that same notice. Purpose changes update that projection instead
of adding a second card. Task identity is server-bound; only typed task results resolve
registered result attachments. Context and voice obey the same routing policy, existing
quiet-hour rules and independent delivery receipts. No second event store or scheduler.

## Delivery

Each channel has independent pending/inflight/delivered/failed/unknown/expired receipts.
Workers are lifecycle-owned and bounded to one concurrent delivery per channel. They
persist claims before external work, use timeouts and capped exponential retry for
replay-safe transports, and recover interrupted claims on restart. The application
instance lock permits only one host per database. Failed/uncertain delivery does not
re-run the originating task. Inbox limits reject new work explicitly rather than silently
throw away old notices; read history remains visible in the inbox.

- `chat`: idempotent stable message ID in the shared timeline; reconnect uses existing SSE.
- `context`: quiet Codex context injection at an idle boundary. No task/model turn is started.
  Injection has no provider idempotency receipt; an uncertain attempt is shown for explicit retry.
- `voice`: opt-in, idle-only, respects quiet hours and serializes announcements with spacing.
  No remote microphone input. Unknown playback is never automatically replayed. `delivered`
  means the application observed output completing, not that the user heard it.

Expired pending notices are not delivered. Deferred quiet/busy periods consume no retry
attempts. Reading is explicit via UI/MCP; fetch/list/SSE delivery and audio do not mark read.
A revision-checked read cannot accidentally consume a newer correction. Notifications
use explicit user read acknowledgements; result/attention corrections become unread again,
while progress corrections stay silent.
Older dismissed notices migrate to visible read history without replaying deliveries.

Task-result chat delivery resolves registered attachment references through the tasks
projection and the workfiles media owner, including results that finish before a
progress card was shown. It stores no raw media in notification payloads.

Task completion publishes before acknowledging the task revision. A lost acknowledgement
is retried using a stable completion key. Final task cards project the canonical notification,
so task-state updates and inbox delivery do not write competing chat cards. Earlier task history is
not automatically replayed. New explicit continuations receive a new completion event.

## Web and maintenance

Authenticated GET/POST `/api/notifications` supplies pagination, read acknowledgement, receipt
inspection and explicit retry. Existing same-origin protections apply. The composer has
an unread badge and inbox drawer. “引用” beside each notification timestamp attaches the selected notification
to the next user message in the same conversation; it does not send a message by itself.

The inbox groups by a durable association, independent of producer ownership and event
identity. A task execution is one matter by default; separate runs do not collapse
merely because they have the same task title. Foreground events without an explicit
association remain separate. `group_key` (<=200 chars) explicitly correlates a topic or
batch within the publisher's source; optional `group_title` labels it. Keys cannot join
another producer's group. Task callers remain server-bound. Same-event retries across
runs keep the original association and do not create another unread item. Corrections
retain associations; titles, timestamps and model-generated summaries are never join keys.

`groups` returns at most 50 groups (default 20), with an exact member/unread/important
count, representative notice and snapshot timestamp. `group` pages member notices independently
(default 20/max 50); no frontend scan of the whole inbox. Group and member cursors are
scoped, snapshot-bounded keysets. New arrivals appear on refresh, without shifting old
groups across an existing page cursor; corrections remain attached to their original
notice. Groups sort by latest event creation, not acknowledgement time. The unread
filter selects groups containing unread, nonexpired records; details retain the full
history, including read/expired entries. `list` remains the flat event view for callers
that need individual notifications.

Cards prefer unread reminders, then results, over quiet progress. They show a purpose
label, short preview, record counts (not completed-item counts), an expandable timeline and
an explicit group read action. Expansion never marks read. `read_group` requires the
returned group ID and snapshot timestamp; later arrivals/corrections remain unread even
if the group contains more than one page. Background tasks cannot acknowledge groups.
Individual read/reference/retry and inbox-wide unread/read-all retain their semantics.
Expanding or collapsing does not drain pages; additional history loads on user request.

Chat uses the same durable group association, additionally preserving recorded task/run
boundaries. Reports in the loaded conversation merge even when other tasks or dialogue
intervene; progress, results and attention remain separate records inside that collection.
Collections sort by their latest member update; ordinary dialogue keeps its recorded
relative order. Stable association-based UI keys survive loading older history and
corrections. Single reports show their group title too; attention remains visible when
collapsed. Loading history extends the collection, not the unread count. Opening a chat
report does not acknowledge notifications or load the entire inbox.

Grouping is a read projection, not business-state inference or transport coalescing.
Original records, revisions and each channel's receipts remain intact. Neither the selected record nor its type proves that every operation or the overall user
goal has finished. Task execution completion notices say execution ended, not goal achieved.
A producer that knows the complete batch may publish its own evidenced summary; the
notification layer does not parse titles/bodies or call a model to invent one.

Trusted plugins can enrich an old unassociated event using the internal-only
`NotificationLink(source, event_key, caller_run, group_key, group_title)` capability.
It derives task identity from the saved run, allows only unassociated-to-associated or
same-association idempotent links, and never changes payload/read state/channel jobs.
It is deliberately absent from IPC, Web actions and MCP. A link is not authorization to
publish or manage another task. The producer owns evidence of the causal relation.
Existing task origins are indexed on migration; media restores its legacy links from
operation/batch receipts in bounded startup batches through this contract. No title
matching, historical content rewriting or delivery replay occurs.

```
pa --config config/config.toml notifications groups
pa --config config/config.toml notifications group '{"group_id":"GROUP_ID","limit":20}'
pa --config config/config.toml notifications list
pa --config config/config.toml notifications get '{"id":"ID"}'
pa --config config/config.toml notifications read '{"id":"ID","revision":1}'
pa --config config/config.toml notifications retry '{"id":"ID","revision":1,"channel":"chat"}'
```

## Future channels

Implement `channels.Channel`: declare `replay_safe`, implement async `deliver(item)` and
raise `Deferred` only when nothing was sent. Register an adapter with `register_channel`
in the composition phase before startup and select its name in `notifications.channels`.
Use notification ID + revision as the provider idempotency key when supported. Returning
from delivery acknowledges transport success only. Channel-specific credentials and
network protocols belong to the adapter. Mobile push is deliberately not implemented.

The current suite covers durable acceptance, dedupe across task runs, corrections/read
races, independent delivery, retry limits, expiry, quiet deferral, restart and uncertain
playback. Hardware hearing and closed-browser phone push are not claimed by these tests.

Web “全部已读” acknowledges all pages through the server snapshot timestamp. New arrivals or corrections after that snapshot remain unread; only the user can acknowledge.

Task event notifications can be published before their run ends. Inbox reads resolve
execution state/final duration through the read-only `TaskRequest("execution_info")` (one bounded batch per page), without
mutating notification content, revision, read state, or channel receipts. A pending
execution is labeled as such; open Web inboxes refresh only that execution metadata.
`notify_completion` controls automatic end-of-run summaries, not explicitly requested
monitoring events. The Web labels progress, results and attention separately.

`inbox` is a durable-only channel for installations without conversation. Configure
`channels = ["inbox"]` and `voice_enabled = false`; chat/context channels still require
the conversation capability and fail startup if explicitly selected without it.
