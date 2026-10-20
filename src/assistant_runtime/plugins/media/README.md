# Media acquisition

Optional `media` plugin + independent `media` MCP/CLI. Requires `notifications`.
PanSou searches public candidates; the Aliyun adapter owns share/auth protocol;
OpenList supplies read verification. Plans and AI decisions remain in tasks.
There is no media scheduler, arbitrary-URL downloader, TV controller or new model endpoint.

## Setup

Install the `media` extra into the project's existing `.venv`. Run the official
PanSou service with `docker compose -p personal-agent-media-search -f deploy/media.compose.yaml up -d`.
Its port is loopback-only, resources/logs are bounded and its disposable cache is tmpfs.
The image digest is the tested upstream artifact; upgrade deliberately and reverify.
Search coverage is partial, source labels are untrusted and no result means only
that these sources found nothing. Search remains Aliyun-only; refresh does not enable
additional providers, accounts or sources.

## Search and late results

`search_media(query, limit=20)` returns an immutable `search_id` snapshot. Page with
its `next` as `before`, together with the same `query` and `search_id`. Snapshots
expire after five minutes or bounded cache eviction (32 snapshots); restart from the
first page if expired. Pagination never silently switches to a newer result set.
Head requests reuse results for 30 seconds; identical concurrent queries share one
fetch. Up to three distinct queries fetch concurrently, with at most eight requests
waiting/working. Cancellation releases admission; failed refresh retains old pages.

PanSou returns quickly while slow sources enrich its backend cache. If results are
insufficient, wait `refresh_after_seconds` (currently 8), then call
`search_media(query, refresh=true)` without `before`/`search_id`. This bypasses only
our head cache and reads enriched backend results into a NEW snapshot. It does not
send PanSou's force-rescan flag, which would restart upstream work. Use at most two
follow-ups per search attempt within its time budget; no permanent polling or model
waiting loop. A few title/alias/date queries may run concurrently when needed.

Candidates prefer link-specific titles over channel collection titles, retain the
original context title and publication timestamp, and rank title matches first.
Unmatched candidates remain available because filenames can provide better evidence.
These are untrusted hints, not year/episode verification or proof of video availability.
`source_health` is a bounded best-effort summary of RECENT searches, not this query's
completion evidence. Health failure does not discard results; raw provider error
strings are not forwarded. Zero yield alone does not prove a source is broken.
`complete=false` describes partial public coverage; it does not claim that all
configured sources have or have not finished. Distinguish source errors, invalid
shares and missing matching files. No Aliyun match does not establish absence on
other netdisks or across the Internet.

Enable `media` alongside `notifications`. Minimal instance example:

```toml
[media]
search_url = "http://127.0.0.1:8888"
openlist_url = "http://192.168.1.10:5244"
timeout_seconds = 15
queue_capacity = 128
max_operations = 10000

[media.targets.movies]
drive_id = "RESOURCE_DRIVE_ID"
folder_id = "MOVIES_FOLDER_ID"
openlist_path = "/阿里云盘/影视/电影"

[media.local_targets.movies]
path = "/mnt/P/openlist/media/电影"
staging_path = "/mnt/P/openlist/temp/media-downloads"
mount_path = "/mnt/P"
```

Targets are explicit account/drive/folder bindings. Only configured targets may
receive transfers; arbitrary filesystem or cloud paths cannot be tool arguments.
`scripts/prepare_media.py --config config/config.toml --create-targets` creates/reuses
only `影视/电影` and `影视/电视剧` on the resource drive and prints configuration.
`--apply-config` applies it with a private prior-config backup. It preserves unrelated
settings and refuses to replace existing media configuration. Run before enabling
media, or stop its service; an exclusive credential-owner lease prevents two writers.
An optional `--import-aliyun-auth PRIVATE_FILE` imports an existing aligo Web session
once. Never import an OpenList OAuth refresh token. Credentials must stay out of Git.

Login uses `begin_media_login` then `get_media_login(login_id)` for a native QR image.
CLI: `personal-agent --config config/config.toml media login`, followed by `login_status`
with the returned login ID. CLI saves the QR privately at `state/media/login-qr.png`.
Tokens never cross MCP. The session expires; starting again replaces only a terminal
login. An account identity binding prevents accidental account switching.

OpenList uses explicit `state/media/credentials/openlist.json` with either
`{"username":"SERVICE_ACCOUNT","password":"PRIVATE"}` or `{"token":"PRIVATE"}`.
Use a dedicated read-only account. If its user base path is restricted, add
`"base_path":"/阿里云盘/影视"`; the adapter maps configured absolute paths to that root.
It never reads OpenList's database, resets users or borrows its mount OAuth credentials.
Missing/expired OpenList credentials do not undo confirmed cloud acquisitions.

## Local original-file library

Infuse should mount only the existing OpenList local directory (`/本地影视` in this
instance). A cloud save is a separate operation from a local download. Select a file
with `list_media_files`, or use `saved_file_id` from a completed transfer, then call
`download_media(request_id, target, file_id)`. Files inside validated descendants of the configured cloud folder are eligible;
provider ancestry is bounded to 16 levels. Their directory hierarchy is mirrored locally.
Folder downloads and arbitrary signed URLs are not tool input.
The plugin requests the original download URL, never video preview/transcoding output.

Create the configured library/staging directories beforehand. Both must be on the
configured dedicated mount, staging outside the published library. Missing mounts,
symlinks, changed destination bindings and insufficient capacity stop work. The default
free-space reserve is 1 GiB (`download_min_free_bytes`). One owned download worker bounds
concurrency, memory, open files and disk IO; cloud copies and foreground tools continue.

Download receipts have `kind=download`, a separate UUID/operation ID and
`queued → checking → downloading → verifying → completed` phases. Progress reports real
downloaded/total bytes, percentage and transfer rate, normally checkpointed every two
seconds. `verifying` means all bytes arrived and the whole-file checksum is being checked;
100% of bytes alone is not completion. Only SHA-1/SHA-256 originals with known size/hash
are eligible. No hash means explicit refusal rather than a false integrity claim.

Network failures retry with bounded backoff (at most eight consecutive attempts per
explicit run); each attempt obtains a fresh signed URL for the same file identity.
Resume validates exact HTTP Range/total size and metadata. An ignored/incorrect Range
is rejected without appending its response. Provider auth recovery reuses the single
account owner; failed login waits in `waiting_auth`, retaining partial bytes. URLs and
provider auth headers never enter receipts, logs, notifications or the download server.
Slow disk reads/writes/hashing run off the main loop; shutdown drains in-flight IO
before descriptor cleanup and preserves partials for restart. Capacity/path/identity
failures require explicit `resume_media_operation` after correction.

Verified files publish atomically without overwriting. A crash after publication but
before the SQLite acknowledgment rechecks the existing file's full hash and completes
without downloading again. Conflicting files are retained. Cancellation stops within
the current bounded network read (up to 30 seconds), removes owned partials when the
disk is available and never deletes published files. Failed-operation partials remain
private for diagnosis/resume; corrupt completed bytes are discarded. Keep operations
and partials together during backup/cleanup; do not delete receipt state behind a worker.

Local completion commits the receipt and notification outbox together. `local_path`,
checksum evidence, elapsed time and byte totals show local readiness, not TV playback.
Original download success does not imply the provider grants every file/account the
same throughput or download permission. OpenList credentials are optional for this path.

## Ownership and reliability

`MediaRequest` and the bounded local IPC are the only framework seam. MCP only maps
schemas/results, including QR images; SDK work runs in killed/reaped, deadline-bound
child processes in the one project environment. aligo's legacy default config path
is confined to the private worker directory; stdout/stderr never expose SDK logs.
Application protocol reads/writes are bounded; a copy is submitted once without
SDK HTTP retry loops. One account lock/lease serializes refreshes and atomically
saves rotated credentials. OpenList and Aliyun have separate authentication owners.

Explicit access-token expiry permits one refresh, session expiry one session repair.
Permission/share errors do not trigger login. Refresh failure blocks further refresh
attempts until a user-confirmed login; pending work enters `waiting_auth` and releases
the worker. One transition-scoped durable notice requests login, not one per task.
QR authorization is main-conversation-only; a wrong-account login leaves the old
credentials intact. Transient/uncertain token rotation is not blindly replayed.
Before refresh, a private durable barrier records a fingerprint of the current token
pair. Restart cannot reuse that pair after an interrupted/failed refresh. An atomically
saved new token pair permits recovery after a lost acknowledgment; SDK device metadata
alone never clears the barrier. Login IO failures become terminal retryable login
states. Credential/QR writes use private atomic files, and shutdown drains owned IO
before releasing the credential lease.

The host SQLite owns `media_operations`, account readiness and an outbox. Source
passwords stay private and are excluded from public operation projections. Operations:

`queued → checking → submitting → verifying → completed`

Also `waiting_auth`, `uncertain`, `failed`, `cancelled`. Completion means the target
contains the expected filename/size and either matching content hash or the provider's
returned destination file ID, not that the TV played it. Aliyun may omit share hashes;
without a copy receipt/hash, a same-name/same-size file alone cannot establish identity.
Receipts retain IDs, bounded stages, elapsed time and distinct OpenList evidence.
No estimated transfer percentage is invented for a server-side cloud copy.

- A canonical UUID `request_id` deduplicates lost transport replies; changed payloads
  using that UUID fail. A source file/target is reused across different UUIDs.
- Tracking additionally deduplicates stable collection + season + episode + destination,
  even when public share URLs change. It does not automatically upgrade/replace quality.
  Confirmed pre-submit failures permit a new request/source; uncertain work retains its claim.
- The target must still be a folder; changed target configuration stops new writes.
  Only individual video files with known sizes/identities may be acquired. Browse
  share folders first. Same-name differing content fails without rename/overwrite/delete.
- A submitted receipt is committed before network IO. Crash/lost response enters
  verification, never copy replay. Missing files are rechecked with bounded backoff;
  after the observation window, uncertainty requires explicit reconciliation.
- Cancellation before submission stops work; after submission it cannot roll back
  the provider. Continue checking and report confirmed saved files. Never delete them.
- Rows/queues/pages are bounded. History uses keyset cursors, 20 by default/max 50;
  a cursor cannot be reused under another collection filter. Target conflict scans
  stop at 1000 entries instead of assuming an incomplete list proved absence.
- Confirmed completion + notification outbox commit in one transaction. Publish retries
  use stable identities and do not repeat copy. Auth/attention notices are deduplicated.
  OpenList read checks and event observations do not initiate AI inference.

## Tasks and permissions

Main conversation uses media tools within user intent. Background reads require an
active task execution; transfer/resume/cancel also require `resources=["media:TARGET"]`.
Background management is limited to that task definition's operations. No interactive
login, target configuration, arbitrary credential input or unrelated account changes.
Task traces already record MCP calls; each operation also records its originating run
ID, linking receipts back to that execution. Pausing/terminating a tracking plan stops
future checks; separately cancel pending media operations when that is also intended.

For tracking, create a normal interval/cron task with the authorized series, year,
season, preferred quality/language and target in its objective. Set
`notify_completion=false`; Media sends verified acquisition and auth/attention notices.
Each run searches, inspects actual files and submits only missing episodes using
stable collection/season/episode fields. Finish the AI run after submitting and retaining
operation IDs; media owns long copy/auth waits. Do not create a permanent waiting Goal,
another watcher, duplicate completion notices or a second progress database in memory.

## Validation

`scripts/check.py --group media` covers durable retry/conflict/pagination, restart
reconciliation, cancellation before/after copy, task scope, native MCP QR images,
refresh serialization, wrong-account preservation and sanitized provider failures.
HTTP fixtures test source-domain filtering and OpenList reauthentication/Range reads.
Download fixtures stream real HTTP bodies to actual local files, testing Range resume,
expired URLs, ignored ranges, corruption, disk safety, shutdown and crash publication.
These fixtures do not establish real provider or television behavior.

Explicit installed-service acceptance (not an automatic parallel test):

```sh
.venv/bin/python scripts/verify_media.py --config config/config.toml --query 流浪地球
# One authorized cloud copy; retain the printed request/operation IDs if interrupted:
.venv/bin/python scripts/verify_media.py --config config/config.toml \
  --share-url https://www.alipan.com/s/SHARE_ID --file-id VIDEO_ID --target movies
# Explicitly download an original already in the configured cloud folder:
.venv/bin/python scripts/verify_media.py --config config/config.toml \
  --download-file-id SAVED_FILE_ID --target movies
```

Real protocol acceptance on 2026-10-16 confirmed account readiness, share file
metadata, capacity, destination metadata and listing through the isolated adapter.
The earlier manual integration confirmed one 13.94 GB MKV save and OpenList HTTP 206
with its MKV header. That manual result alone does not certify this installed plugin,
all search sources, a full film playback, or an ongoing series tracking plan.

Installed-plugin acceptance on the same date discovered all 12 tools over independent
stdio MCP, returned a bounded page of real public search results, and saved the
13,942,852,762-byte movie to the configured movies folder. The destination file ID,
name and size matched the copy receipt; the operation completed in 3.545 seconds.
A real subscription-backed temporary task successfully called `media_status` and
`list_media_files`; its provider tool trace confirmed both calls and the task archived.
The full project gate passed 404 tests without skips plus frontend/static checks.
Installed OpenList verification is awaiting a configured dedicated read-only account;
that credential step and real TV playback are not claimed by this acceptance.

Local-download extension acceptance on 2026-10-16 discovered 13 MCP tools and started
an actual original-file download through the installed service. After 2,620,616 bytes,
a standby service restart resumed the same operation at exactly that file offset.
Cancellation then removed the owned partial without publishing any library file.
Status requests during downloading took approximately 1–2 ms. This provider session
delivered about 25 KB/s, so the 13.94 GB acceptance film was not fully downloaded.
Whole-file completion/hash/publication are covered by real-HTTP local fixtures; a full
cloud film download and TV playback are not claimed. The expanded full gate passed
419 tests, zero skips, plus static checks and the frontend build. Configured local
destinations are under `/mnt/P/openlist/media`; staging is outside that library.


## Library organization and batches

`Library` owns stable cloud object identities, last-observed locations, collection facts
and exact change plans. `LocalLibrary` owns filesystem changes. Provider protocol stays
in Aliyun; scheduling and model decisions stay in tasks. No global library scan starts
on a timer. `browse_media_library` reads one bounded folder page on demand, returning
coverage, observation time and whether the page finishes that folder. This is never a
claim that the whole library or every public source was scanned. `get_media_asset`
returns cached observations; operation receipts keep historical evidence separately.

Start with `media_capabilities`. `preview_media_changes(target, changes, location)`
persists up to 100 exact changes for one hour; `apply_media_changes(request_id, plan_id)`
queues them in the existing media operation repository. Existing explicit user authority
permits immediate apply; ask again only for ambiguity, conflicts or expanded scope.
Use small batches for slow cloud accounts: preview performs real bounded metadata and
conflict checks, so 100 is a ceiling, not a latency guarantee. Execution checks again.

Cloud changes are `mkdir(folder_id,name)`, `rename(file_id,name)`,
`move(file_id,folder_id)`, `trash(file_id)`, `restore(file_id)`. Local changes are
`mkdir(path)`, `rename(path,name)`, `move(path,folder)`, `trash(path)`,
`restore(file_id)`; each object includes `action`. Local paths are relative to the
configured library. Create parents first. One change per file per plan keeps every
precondition unambiguous; folders can be created but recursive folder moves/deletes
are not exposed. No overwriting, extension changes or inferred media identity.

Accepted writes are durably marked before IO. Lost replies reconcile against stable
identity and the intended location, never blindly repeat the write. An interrupted
mkdir without a returned identity stays uncertain; a matching name alone does not
prove ownership. Resume explicitly to recheck an uncertain write. Cancellation stops
later steps and reports already completed steps; it does not roll them back. A partly
applied failed/cancelled plan reports `outcome=partial`. Download reservations prevent
conflicting cloud changes; local management waits for target downloads to end.

Local changes use no-follow directory descriptors, require the configured mount, and
stay on that filesystem. Files move using no-clobber link/unlink with inode/size/mtime
preconditions; crash recovery recognizes the two-name intermediate state. Local trash
is `staging_path/library-trash`, outside the published library, with no automatic purge.
Restore requires our recorded original location and refuses collisions. Cloud restore
also requires our own recorded trash receipt, and remains subject to upstream expiry.
Permanent deletion is not exposed. Trash takes disk space; it is not space reclamation.

`transfer_media` accepts optional validated `folder_id` and desired `name`. The adapter
performs copy then verified rename; cloud acquisition is complete only after organization
succeeds. `download_media` accepts a stable cloud ID and resolves its current parent within
the configured target. It retains original-file Range/hash/atomic-publish guarantees.
`submit_media_batch` groups 1..100 explicitly selected transfer/download items. Its UUID
and per-item derived UUIDs survive disconnect/restart. `get_media_batch` reports item
receipts and completed/partial/failed/cancelled/uncertain/waiting_auth outcomes.
`cancel_media_batch` does not cancel an operation merely reused from another batch.
Media owns waiting; the AI run need not stay alive. A batch emits one summary rather than
per-file completion notices; account-expiry notices remain transition-scoped.

Use stable collection/season/episode for deduplication. Keep existing versions by default;
only an explicit user request warrants a distinct `version` identifier and a nonconflicting
filename. Old copies remain. Do not invent title/year/season/resolution from weak filenames.
Recommended layout is `电视剧/剧名/Season 01/剧名.S01E01.ext`, with year when needed to
resolve ambiguity; films use `电影/片名 (已知年份)/片名 (已知年份).ext`. Unidentified existing
files remain browsable. Historical completed transfer receipts populate collection links
when their exact saved IDs are observed, not by fuzzy filename matching.

`media_collection` persists revisioned completion criteria:
`{title,season,total_episodes,evidence,required_state:"cloud"|"local"}`. Unknown episode
totals are null. Query returns paginated observed copies, missing episodes and pending
acquisitions. `completion_candidate` is based on recorded observations, not a fresh
provider audit. Reverify expected copies and unresolved operations before ending a plan.
Do not infer series completion from no new search results. Configure task
`self_completion={condition:...}` only when authorized, then use `complete_own_task` with
current revision and concrete evidence. That archives future scheduling and preserves the
current run's final response. A completed AI execution alone is not a completed media goal.

CLI uses the same actions: `capabilities`, `library`, `asset`, `preview`, `apply`,
`collection`, `batch`, `batch_get`, `batch_cancel`; pass bounded JSON arguments. MCP/CLI
have no credentials or arbitrary endpoint arguments. Background writes require the
captured `media:TARGET` grant; plan application/cancellation also respects ownership.

Validation: `scripts/check.py --group media --group tasks`. Protocol fixtures and actual
local filesystem tests cover lost write acknowledgments, changed preconditions, restore
conflicts, symlinks, nested downloads, cancellation, aggregate notices and scoped grants.
They are separate from installed-service and cloud-account acceptance.
