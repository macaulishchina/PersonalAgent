# Durable events

Optional same-process plugin, enabled by adding `events` to the instance's plugin list.
Install the `events` extra. It adds an independent MCP and CLI, using `EventRequest`
through the existing runtime. There is no Web panel, automatic notification, task or
memory extraction. The kernel event bus carries only post-commit invalidation hints.

## Configuration and ownership

`[events]`: `quota_bytes=2147483648`, `queue_size=128`, `query_seconds=2.0`.
`state/events/` holds only recording state: SQLite records, revisions, gaps and quota.
The recorder does not import source adapters, handle credentials, poll hardware or
own source configuration. `events` can run without any source-management plugin.

The optional `observations` plugin owns the revisioned non-secret registry and source
lifecycle. Each registered producer owns protocol, subscription or polling, decoding,
state comparison, recovery and read-only verification. See the
[source guide](../observations/README.md). The event MCP remains a convenient facade:
record operations use `EventRequest`; source operations forward to the separate
`ObservationRequest` capability. The recorder never instantiates source drivers.

## Producer interface

Trusted plugins obtain a source-bound handle with `context.event_reporter(source_id)`.
Omitting the source ID binds the plugin name. A source cannot be claimed by two
plugin owners. Handles become unusable when their context generation changes or they
are explicitly closed. There are at most 256 bindings per runtime, including retired
source identities. After unusually many source replacements, restart the service.
This is a trusted-process contract, not a sandbox against arbitrary Python code.

```python
reporter = context.event_reporter("living_room_sensor")
await reporter.report(type="door.opened", subject="front_door", data={"open": True})
admission = reporter.offer(type="sensor.edge", subject="front_door", data={"open": True})
```

`report` returns a committed receipt. `offer` is event-loop local and returns
`Admission(accepted, reason)` immediately: `queued` confirms admission only;
`full`, `invalid`, `unavailable` and `closed` reject it. The bounded queue copies and
validates accepted payloads. Asynchronous write failures remain in event health and
coverage gaps. No silent success, unbounded buffer, hidden fallback database or model
activation. An unavailable recorder leaves the producer free to continue its business;
normal report callers must handle errors, while real-time code uses `offer`.

Default identity is a fresh UUID; default time is the local observation time. To retry,
prepare and retain **both** `event_id` and the same occurrence/observation timestamp
and payload. Repeating only the ID with a newly generated time is a content conflict.
`event_id` maps to the persisted `key`, preserving existing event identities. Optional
fields are `version`, `evidence`, and either `occurred_at` or an observation interval
(`observed_start`, `observed_end`). Do not present receive time as a device timestamp.
Calls have a 15-second deadline; timeout/cancellation can leave an uncertain commit.

Continuous sources additionally use `reporter.event(...)` to build records,
`checkpoint(records, at=...)` to commit one bounded observation atomically, and
`gap(reason, at=...)` to mark interrupted coverage. This optional extension is not
required for ordinary point-event reporting. Producers must establish restart coverage
and recovery baselines; a successful append alone does not assert continuous coverage.
Stop producing first, close the reporter to drain admitted edges, then mark the stopped
gap. Source lifecycle management does this for configured producers.

## Persistence, limits and recovery

One connection-owning writer thread serializes mutations; one read-only connection
thread serves snapshot queries. Admission queues are bounded; excess requests fail
explicitly. SQLite uses WAL and FULL synchronization. A successful append is committed;
caller timeout/disconnect leaves an unknown result, resolved by retrying the same key.
Atomic sampling batches commit gap recovery, baseline/transitions and observation
checkpoint together. In-memory source comparison state advances only after success.
Restart uses a fresh baseline and records the interval since its last checkpoint.

No age-based retention. The configured quota is a storage ceiling, not a promised
payload capacity: half is reserved for WAL and transient pages, with additional small
management/SHM headroom. Inserts/corrections stop before the ceiling, while queries,
deletions and management remain available. Free SQLite pages are reused after deletion;
the database file need not shrink. A bounded reader can temporarily delay WAL truncation;
new writes stop if checkpoint headroom is occupied. Physical media/disk-full failures
can still prevent any write, including deletion; health reports that condition.
Per-event content is at most 16 KiB, snapshots at most 4096 events, router snapshots at
most 2048 clients/1 MiB, ADB replies at most 256 KiB, sources at most 64. SQL progress
deadlines bound execution; queries report errors on deadline rather than false counts.

The audio path only offers an event to a bounded in-memory queue. It never waits for
SQLite. Overflow/storage failure opens a gap and is visible in health. No raw audio is
saved. Process crashes can lose uncommitted edge events; restart coverage marks that
uncertain interval. Quiet hours and fixed voice bindings remain assistant-owned.
Source shutdown stops producers first. Recorder shutdown drains accepted bounded ingress
and storage work; it does not operate collectors.

History uses keyset pagination (default 50, max 100), ordered by event time then stable
ID. Filters: source/type/subject/since/until/evidence/entrance. Poll intervals are stored
explicitly; history ordering uses interval end and time filters match overlapping intervals. `get` revisions are paginated too.
`changes` uses a monotonically increasing cursor for append/correct/delete and returns
the current `head`, allowing consumers to detect a restored/rewound log. History
pages are not a frozen snapshot; incremental consumers should use change cursors.
Query coverage reports observation bounds, ongoing gaps and a bounded historical gap
summary. Inspect `gaps_truncated`. Group statistics bound group count and SQL time;
`complete` indicates all groups returned, `coverage_complete` describes observations.
An explicit query interval extending beyond a source's last checkpoint is incomplete.
Deleted evidence cannot be used to establish completeness; because tombstones intentionally contain no source/content, their presence conservatively prevents a completeness claim (returned as deleted_records_present). Database schema v2 migrates v1 tombstones transactionally without reviving records.

Stable identity is SHA-256(source,key), with original-content fingerprint comparison.
Corrections preserve prior revisions and original fingerprint; same original source
report remains a duplicate after correction. Delete clears all payload/revision fields,
moves its minimal identity to a separate tombstone table so record/index pages can be reused, uses secure_delete, and checkpoints WAL; concurrent readers can temporarily retain old
pages until their bounded transaction ends. Tombstones prevent resurrection. Deleted
records retain only opaque ID, revision and deletion/change metadata. No content is
stored in change cursors. Never reuse a source ID for a different logical device.

## Tools and authorization

CLI: `personal-agent --config <path> events <action> '<JSON>'`.
Actions: capabilities, sources, status, query, get, changes, stats, record, correct,
delete, configure, enable, remove, verify. Independent named MCP tools map directly to
these operations. Main session and background registration share `mcp/catalog.py`.
Manual records/corrections must explicitly be user_statement or assistant_summary;
callers cannot impersonate hardware sources. Background identity comes from its active
execution; source management and editing another source require events_management in
the task resource authorization. This is a trusted local plugin/process boundary,
not a sandbox against an agent with arbitrary local shell access.

Accepted keyword wake count: configured wake source + type=wake.accepted +
evidence=observation + entrance=keyword. CLI/gesture wakes and successful connection
are separate facts. Event content is data, never instructions.

Run `.venv/bin/python scripts/check.py --group events` during development, and the full
gate before deployment. Fixtures are synthetic/redacted. Real acceptance must distinguish
connection/decoding from naturally observed state transitions. Onboarding workflow is
in [the bundled event-management skill](skills/event-management/SKILL.md).
