# Media acquisition

Optional `media` plugin + independent `media` MCP/CLI. Requires `notifications`.
PanSou searches public candidates; account-scoped Aliyun/Quark adapters own share/auth protocol;
OpenList supplies read verification and an optional reusable filesystem backend. Plans and AI decisions remain in tasks.
There is no media scheduler, arbitrary-URL downloader, TV controller or new model endpoint.

## Setup

Install the `media` extra into the project's existing `.venv`. Run the official
PanSou service with `docker compose -p personal-agent-media-search -f deploy/media.compose.yaml up -d`.
Its port is loopback-only, resources/logs are bounded and its disposable cache is tmpfs.
The image digest is the tested upstream artifact; upgrade deliberately and reverify.
Search coverage is partial, source labels are untrusted and no result means only
that these sources found nothing. Discovery spans supported public platforms; Aliyun and Quark acquisition are implemented; configured accounts determine availability. Refresh does not add providers, accounts or sources.

Media completion facts use the shared notification purpose contract: successful
operations/batches publish `result`, with `report_with_task=true` when delegated.
Pending task reports cover those facts as silent progress; event-only monitoring and
late results still receive result reminders. Standalone successes publish `result`; failed/uncertain operations and expired authentication publish
`attention`. Progress remains queryable in notification group history without unread
badges or duplicate chat reports. Task execution results remain owned by tasks; media
never marks the whole user objective complete from one successful operation.

## Accounts and provider capabilities

`media.accounts.<alias>.provider` selects `aliyun`, `quark`, or an already mounted `openlist` storage. Each target requires
an explicit `account` alias and `openlist_path`; native acquisition providers also require
`folder_id`, and Aliyun additionally
requires `drive_id`. An alias owns `state/media/credentials/<alias>/` and one credential
lease. Aliyun is the default account definition when the accounts table is omitted;
targets still must explicitly bind it. Multiple accounts of one provider are supported.
Share URLs identify the provider; inspection takes an optional account (required when
ambiguous). Transfers use the destination's account and reject cross-provider shares.
Account names are non-secret; tool callers cannot provide endpoints or credentials.

`media_status` returns `accounts`, not one global account. Authentication notices and
resumption only affect the matching account's operations. Inspection cache keys include
account, share, password, parent and page; asset IDs include the full target binding.
Changing a target blocks old in-flight intents instead of redirecting their writes.
Use new aliases for deliberate account replacements; authenticated identity bindings
must never be silently replaced. No automatic provider fallback or cross-cloud copy.

Quark currently supports status/capacity, bounded share inspection, cloud browsing,
configured-root ancestry checks, folder creation, single-file transfer and asynchronous copy observation.
Provide the source `parent_file_id` from inspection when submitting a nested share file;
`folder_id` refers to the destination. A copy task ID is persisted before polling; its
returned destination file ID plus exact name/size establishes completion. Lost receipts
without hashes stay uncertain; same name/size alone never proves ownership. The existing
operation queue owns deadlines, retries, cancellation, restart recovery and notices.

Quark credentials are a private 0600 JSON file at
`state/media/credentials/<alias>/account.json` with a `cookie` string. Set this file
locally, never through chat/MCP/config. Cookie rotations are serialized and atomically
saved. The authenticated cookie UID is privately fingerprinted; changed identity blocks
operations. Full expiry requires the user to update the private Cookie, check status,
and resume the existing operation IDs (restart also checks waiting accounts). No
interactive Quark QR flow is advertised. OpenList owns its separate Cookie; this
adapter neither reads its database nor modifies its mounted-drive authentication.

Per-account actions in `media_capabilities` are authoritative. Quark supports rename,
move (including rename-on-transfer), and original-file download through the shared
library/download workers. Trash/restore use the same change plans and recovery worker:
`/file/delete` submits one file to the recycle bin, `/file/recycle/list` verifies it,
and `/file/recycle/recover` restores the exact saved recycle-record ID. Both mutations
return asynchronous task receipts; completion requires observing the file state.
The account-wide recycle list stays internal and bounded. Restore accepts only our
recorded deletion, checks the record ID/name/size/original parent, and rejects stale,
expired, changed or incompletely listed records. No permanent-delete endpoint is registered.
The ordinary file-info/list endpoints omit hashes; `/file/download` returns the full
file MD5 alongside the original URL. The adapter checks its identity/name/size/parent
against fresh file-info before exposing normalized metadata to the download worker.
Missing or malformed MD5 stops the download; multipart ETags are not checksums.
MD5 here detects transfer corruption, not adversarial substitution or authenticity.
Quark download Cookies are allowed only on HTTPS `dl-*.drive.quark.cn` hosts, bound
to the exact returned origin. No redirects, credential persistence in operation
receipts, or forwarding to arbitrary URLs. Aliyun remains signed-URL based.
OpenList account permissions and actual playback remain separate evidence.

This configuration format requires explicit conversion before upgrading an existing
instance. Stop media work after reconciling active operations; back up the instance DB
and config. Add `account` to targets and saved destination/root-destination/batch/plan
bindings, recompute operation fingerprints/dedup keys and asset IDs with those bindings,
and update saved references consistently in one transaction. Discard the disposable
inspection cache. Preserve task/operation receipts and evidence; do not reset submitted
operations or recreate cloud writes. Verify against a restored backup before applying.
There is one runtime format, no dual-read legacy path or startup data migration.

## Reusable OpenList filesystem backend

### Synchronizing confirmed cloud changes

Both native and OpenList file backends persist an `openlist_sync` receipt atomically
with each confirmed step (including partial/cancelled plans) or completed acquisition.
Trash, restore, rename, mkdir and acquisition refresh the affected parent; moves refresh
both parents. This tracks assistant-owned operations, not arbitrary changes made by
other clients; those remain subject to OpenList cache expiry or explicit refresh.

The media-owned worker uses `/api/fs/list` with `refresh=true` and a one-item page.
It coalesces same-target directory refreshes across up to 64 due operations, handles
at most 32 directory groups per pass, and debounces for one second. Each directory
has a generation so a late refresh cannot acknowledge a newer change. Pending work
survives restart; eight bounded exponential-backoff attempts end in a visible failure.
No additional cloud mutation is issued on failure, shutdown or retry. Completed
acquisitions retain their separate file-visibility check, without forcing another
directory refresh. Directory refresh success is not content-integrity verification.
Sync receipts and manual sync retries preserve the cloud operation's retry deadline
and attempt count, including plans that have only partially completed.

`get_media_operation`/list results expose `openlist_sync` (`pending`, `synced`, `failed`)
and per-directory attempts, errors and observation times. `verify_media_operation`
requeues synchronization, including old completed or partially completed library
operations, without resubmitting the plan. Logs contain operation IDs and error codes,
not credentials or upstream response bodies. Cloud identity/provider/endpoint changes
block an old sync; correcting an explicit OpenList path for the same cloud root is allowed.

Native folder IDs are resolved through the existing scoped `Library.destination`;
OpenList mount paths are never inferred from native cloud ancestor names. Keep
`targets.*.openlist_path` aligned with the actual mounted root. The shared private
OpenList credentials must match the server user's `base_path`. OpenList v4.2.7 requires
write-content (upload/mkdir) permission for forced refresh, even when the endpoint
only reads a directory. Runtime does not grant permissions or widen account scope.
No admin token or cloud Cookie is borrowed for synchronization.

`test_media_openlist_sync` covers transaction rollback, coalescing, both move parents,
partial completion, restart/cancellation, late refresh generations, retry bounds,
mapping/identity checks and retained post-acquisition visibility evidence.

Acquisition and mounted-file management are separate responsibilities. `provider`
selects the share/auth adapter. `media.file_backend` defaults to `openlist`; each
account can override `file_backend = "native"` or `"openlist"`. Existing Aliyun and
Quark instance/template accounts explicitly use `native`. The implicit default Aliyun
account also remains native. There is no automatic fallback, and no provider-ID/path
conversion. A native acquisition account can use OpenList for its existing-file
management without replacing share inspection/copy/auth. `list_media_files` and
transfer destination IDs remain native acquisition tools; `browse_media_library`,
preview/apply and download select the configured file backend.

For another mounted storage, set `provider = "openlist"` and configure targets with
only `account` and `openlist_path`. No bespoke adapter or duplicate cloud Cookie is
needed for these filesystem capabilities. Public-share acquisition does not become
available merely because a storage is mounted. New native providers can use the same
`OpenListFiles` port; it owns only bounded protocol IO. `OpenListLibrary` binds its
path semantics to the existing plans, queue, grants and download worker. No kernel
contract, new scheduler or second operation database is introduced.

Use `media_capabilities.accounts.<alias>.files` for the selected backend, addressing
and supported actions. OpenList cloud browsing uses `path` relative to the target;
its `ol_...` references are explicitly path identities, never cloud IDs. Changes are
`mkdir(path)`, `rename(path,name)`, `move(path,folder)`; use existing preview/apply tools.
Only individual files may be renamed/moved, with extension preserved, no overwrites,
no implicit directory traversal, no cross-target move or automatic parent creation.
Permission and actual driver support are checked by OpenList at execution time;
implementation availability is not proof of every mounted driver's support.

Keep `state/media/credentials/openlist.json` private (0600), containing
`username`, `password`, and `base_path` (the exact OpenList user root, `/` for admin).
A saved token is also accepted. The port checks `/api/me` against that root before
filesystem operations. Use a suitably scoped OpenList account; never borrow its
administrator database or widen an existing playback account automatically. The
account must allow the chosen filesystem operations. Browsing uses normal cached reads,
and downloads need read access plus upstream hashes. Fresh precondition/readback scans
use `refresh=true`, which OpenList v4.2.7 ties to mkdir/upload (write-content) permission;
rename/move-only permissions are insufficient for managed mutations. Missing permission
is reported, never automatically granted. OpenList owns cloud credentials;
the assistant owns only its OpenList login. Expired read authentication retries once;
a rejected write is not replayed in the HTTP adapter. Update private credentials and
explicitly resume the same operation. Startup checks standalone OpenList accounts for
auth recovery. An OpenList outage never switches to the native implementation.

Each plan captures the endpoint, account definition, target and original metadata.
Preview and submission both check conflicts and preconditions. Submission is persisted
before HTTP IO. Success requires a fresh directory readback, not HTTP acceptance or a
queued upstream task. Returned task IDs are preserved without arbitrary upstream
messages/URLs. Lost replies never replay writes: hash-backed rename/move can reconcile
by observed content and old-path absence; without that evidence they remain uncertain.
Rename/move may legitimately change the upstream modification time. It is checked
before submission, but result comparison uses type, size and any available content
hash instead; acknowledgment and source absence are still required without a hash.
Lost mkdir acknowledgment remains uncertain even if a same-name folder exists. These
are observed path states, not stable cloud object identities. Confirmed rename/move
marks the former path absent. External concurrent modifications cannot be made atomic
by the generic OpenList API; conflicts or ambiguous observations require attention.
Paginated listings are bounded live pages, not a consistent filesystem snapshot.

`download_media(target, path, request_id)` and download batches with `items=[{path:...}]`
use the existing local destination, durable progress, Range resume, cancellation,
whole-file validation and atomic publication. Only a configured same-origin `/p/`
proxy URL is used; returned `raw_url`/site URLs and redirects are ignored. OpenList
API tokens and upstream Cookies never enter the download worker or receipts. The
upstream mount should serve original files; a transcode/length/hash mismatch fails.
The mount must permit OpenList proxy downloads. A proxy HTTP 403 is surfaced as
`openlist_proxy_forbidden`, without assuming an expired URL or automatically retrying
a permission/configuration rejection. Check the mount and upstream permissions;
the assistant does not enable proxying or widen permissions on its own.
A valid upstream MD5, SHA-1 or SHA-256 is mandatory. Missing hashes are reported as
`download_checksum_unavailable`, not silently replaced by size-only verification.
Queued path downloads capture metadata to detect replacement before starting.

Generic remove/copy, permanent deletion, recycle-bin restoration, share transfer,
capacity and interactive cloud login are not advertised as OpenList filesystem actions.
In particular, `/api/fs/remove` may permanently delete files for some drivers. Native
Aliyun/Quark trash/restore remain available under their native backend. Switching a
backend requires reconciling in-flight work first: old plans/downloads fail their
binding check rather than silently operating through the new backend. Config is
restart-applied; this implementation does not migrate historical cloud IDs to paths.

Validation: `test_media_openlist` is part of the media test group. Real HTTP fixtures
cover scope and pagination, malformed/incomplete responses, auth expiry, denied writes,
no-overwrite checks, lost acknowledgments, restart, cancellation, backend changes,
MCP grants, path batches and complete checksum-verified downloads. An isolated real
OpenList v4.2.7 container separately verified mkdir/rename/move/readback. Original proxy Range bytes and denial of writes by a scoped read-only account were
also checked. Its Local driver omitted upstream hashes; the download interface correctly refused to claim
checksum completion. This does not establish every cloud driver's acceptance.

Real-cloud acceptance (2026-10-21, installed OpenList v4.2.7): dedicated synthetic
65,588-byte files were uploaded through OpenList, separately from ordinary media.
AliyundriveOpen and Quark both passed mkdir, list/readback, rename and move; Quark's
eventually visible move was reconciled from its persisted receipt without resubmission.
Aliyun's SHA-1-backed rename also recovered after deliberately discarding the actual
write reply and reopening the operation database. Without a hash, Quark correctly
kept that lost-reply operation uncertain. Cancellation before submission sent no write.
Quark's original proxy returned HTTP 206 and all bytes matched the uploaded fixture,
but its driver exposed no upstream hash, so the shared verified-download workflow
correctly refused enqueueing with `download_checksum_unavailable`. Aliyun exposed
SHA-1, but the existing mount forbade proxying (HTTP 403, `proxy not allowed`); the user
chose to retain that configuration, so full shared-worker cloud download/resume is
not accepted for either mount. These are separate from passing HTTP-fixture download
tests. Production media configuration remained unchanged and both accounts stayed
native. Test file names/parents were restored; dedicated test folders were retained.
Upload here only provisioned acceptance fixtures and is not an advertised media action.
The mounted root was changed externally during testing: an OpenList path must follow
the current mounted root, not blindly append the native cloud folder hierarchy.

## Search and late results

`search_media(query, aliases=[], platforms=null, limit=20)` searches a short title
and up to three explicitly supplied known aliases, then merges a bounded immutable
`search_id` snapshot. No model is invoked to manufacture aliases. Page with `next` as
`before`, keeping the same `query`, `aliases`, `platforms` and `search_id`. Snapshots
expire after five minutes or bounded cache eviction (32 snapshots); restart from the
first page if expired. Pagination never silently switches to a newer result set.
Head requests reuse results for 30 seconds; identical concurrent queries share one
fetch. Up to three distinct queries fetch concurrently, with at most eight requests
waiting/working. Aliases share that same three-query semaphore. Cancellation releases
admission and cancels unfinished sibling queries; failed refresh retains old pages.

PanSou returns quickly while slow sources enrich its backend cache. If results are
insufficient, wait `refresh_after_seconds` (currently 8), then call
`search_media(query, refresh=true)` without `before`/`search_id`. This bypasses only
our head cache and reads enriched backend results into a NEW snapshot. It does not
send PanSou's force-rescan flag, which would restart upstream work. Use at most two
follow-ups per search attempt within its time budget; no permanent polling or model
waiting loop. Aliases run as separate queries, so one long overconstrained string is unnecessary.

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

### Discovery, evidence and reuse

`media_capabilities` separates discovery platforms from acquisition providers. Exact
public host/path validation determines platform regardless of backend labels. Discovery
supports Aliyun, Quark, Baidu, 115, 123, UC, Tianyi, Xunlei, PikPak and BTIH magnets.
Other links are rejected, never fetched as arbitrary URLs. Magnets are normalized by
infohash, without tracker URLs. These are format capabilities, not a guarantee each
source covers every platform. Aliyun and Quark shares can be inspected/transferred through configured accounts.
Local downloads currently require a selected file in an Aliyun target.

Candidates deduplicate by platform/share identity, keeping bounded source provenance,
matched aliases and public extraction codes. Conflicting extraction codes are explicitly
flagged rather than silently picked. Different platforms are never merged into one file.
`title_match` is a ranking hint; `verified=false` and `acquisition.availability=unverified`
remain until actual provider/file checks. Inspect year/season/episode before saving.

Each query has `response_received` or `failed`, elapsed time and sanitized error code.
`source_completion=unknown`: this PanSou API provides no per-query source completion
receipt. `counts`, `truncated`, `outcome` and `next_actions` distinguish ambiguous results,
no observed candidates, discovery-only matches and matches on a supported acquisition
provider. All-query failures raise the existing stable error and persist diagnostics in
history. Never translate any of these into “no resource exists”.

`media_search_sources` exposes the enabled PanSou plugin/channel inventory and recent
failure/yield counters, excluding raw error strings. These are PanSou source adapters,
not assistant plugins. Check the actual inventory: a source present in upstream source
code/README may not be compiled into the pinned image, and unknown requested sources
can silently produce empty results. Missing failure entries may mean too few samples.
Maintain `deploy/media.compose.yaml` explicitly: isolate candidate sources, compare
multiple representative titles, check late cache results and network/parser evidence,
then enable verified additions. Do not disable a source merely for zero early yield,
auto-scan every source or introduce a second scraping engine. Upgrades require rechecks.

`media_search_history` returns keyset pages (default 20/max 50) of the latest 200 durable
search summaries, counts, per-query outcomes and up to ten example candidates each;
these examples are not the complete result snapshot. Review history before retrying an
exhausted alias/source combination. Snapshot cursors expire independently of history.
`inspect_media_share` reuses metadata or definitive expired/cancelled/wrong-code results
for 120 seconds, scoped to share + extraction code + folder + page. Use `refresh=true`
to bypass; simultaneous identical refreshes coalesce. Account/network/uncertain failures
are never cached. Up to 128 pages, each <=64 KiB, persist across restart. Passwords are
only part of hashed cache keys; the cache stores sanitized public file metadata.
Candidates expose timestamped `last_inspection` for the same root-page/password scope,
explicitly marking expired evidence; readable does not establish the requested title.
Transfers always check source files again, irrespective of these read caches.

Source acceptance on 2026-10-21 used the pinned image in an isolated container.
`yunso` returned title-matching discovery candidates for 流浪地球, 利刃出鞘3, 普通事故
and Twinless; `susu` additionally found multi-platform entries for 普通事故, 评估/The
Assessment and Twinless, including an Aliyun Twinless link. These are discovery checks,
not verified video contents. Generic “评估” also returned textbooks/exam material;
known original aliases and file identity checks remain necessary. Both sources are
now configured. `duoduo` and `zhizhen` were excluded after their production rolling
windows each reported 20 failed/zero-yield rounds and isolated requests still failed;
other zero-yield sources were retained. Re-evaluate on future upstream/network changes.

Installed-service acceptance on 2026-10-21 discovered all 24 MCP tools. Real queries
for Twinless, 普通事故 + its original alias, 评估 + The Assessment and 利刃出鞘3
returned multi-platform candidates. Alias pairs completed concurrently in about 8s;
head snapshots reused without a second fetch and pages had no overlap. Durable history
keyset pages, exact-scope inspection cache and explicit refresh passed. The Twinless
Aliyun folder returned `Twinless.2025.1080p.VINEnc.mp4`, size 3,834,891,461 bytes;
this verifies readable file metadata, not playback or the film's contents. No transfer
or download was performed. Full gate: 703 tests, zero skips, static checks and frontend
build. After restart the service was standby/audio-ready with unchanged model and USB
voice bindings. These observations do not establish universal source availability.

CLI uses `search`, `search_sources`, `search_history`, `inspect` with the same arguments.
The explicit live acceptance script supports `--alias`, `--platform`, `--sources`,
`--history`; no transfer/download occurs without its corresponding write arguments.

Enable `media` alongside `notifications`. Minimal instance example:

```toml
[media]
search_url = "http://127.0.0.1:8888"
openlist_url = "http://192.168.1.10:5244"
timeout_seconds = 15
queue_capacity = 128
max_operations = 10000

[media.accounts.aliyun]
provider = "aliyun"

[media.targets.movies]
account = "aliyun"
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
`openlist_path` independently identifies that target directory in OpenList's mounted
namespace, not its full cloud ancestry. A mount rooted at cloud `影视` may expose
`/夸克网盘/电视剧`; never insert another `影视` component. Descendants append only
the provider-verified path below the configured target. The OpenList account's
`base_path` is a third, separate access boundary; it is checked against `/api/me`.

Every native transfer must pass a fresh saved-file read by ID before completion:
account-bound destination, file ID, parent ID, name, size, type and available source
checksum must agree. The receipt is returned as `cloud_verification` with observation
time and checked fields. Submission acknowledgments and stale list entries alone do
not finish a transfer. This gate is shared by every native provider.

`verify_media_operation` re-reads the cloud file and checks OpenList independently,
without resubmitting a copy. Its cloud evidence can become `unconfirmed` even though
the historical operation completed earlier. Report that distinction if a file moved
or disappeared. Only `openlist.path_verified=true` exposes a verified `path`; failed
checks expose `configured_path` as configuration evidence, never a working link.
Name/size visibility is explicitly weaker than a matching checksum. Mapping changes
can be rechecked against current configuration without invalidating a cloud save.

`scripts/prepare_media.py --config config/config.toml --create-targets
--movies-openlist-path /实际挂载/电影 --series-openlist-path /实际挂载/电视剧` creates/reuses
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
100% of bytes alone is not completion. Only MD5/SHA-1/SHA-256 originals with known size/hash
are eligible. No hash means explicit refusal rather than a false integrity claim.

Network failures retry with bounded backoff (at most eight consecutive attempts per
explicit run); each attempt obtains a fresh signed URL for the same file identity.
Resume validates exact HTTP Range/total size and metadata. An ignored/incorrect Range
is rejected without appending its response. Provider auth recovery reuses the single
account owner; failed login waits in `waiting_auth`, retaining partial bytes. URLs and
provider auth headers never enter receipts, logs, notifications or MCP results.
An adapter may provide origin-bound download Cookies to the internal HTTP worker;
the shared session has no Cookie jar and never follows redirects.
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
Before creating any write task, foreground reads `media_capabilities.target_bindings`
and binds the exact target's `task_resource` after matching provider/account to user
intent. `series` and `quark_series` are distinct configured targets, not synonyms.
Missing bindings are an internal delegation problem: foreground verifies existing user
authorization and uses the tasks repair interface at a stopped boundary, or creates a
scoped follow-up for an ended task. Users should not have to specify resource keys or
repeat authorization. Background code still fails before submitting a write and cannot
self-grant, borrow another target's grant, or infer authorization from prompt text.
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
in the selected account adapter; scheduling and model decisions stay in tasks. No global library scan starts
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
For providers with separate recycle-record identities, capture that identity on trash
verification and bind it into the restore plan. External restore/re-delete cannot silently
redirect an old plan to a newer deletion. The original directory must still exist inside
the configured target; missing parents and name conflicts stop restoration. Provider
fallback folders or automatic renames are not accepted as the requested outcome.
Permanent deletion is not exposed. Local trash takes disk space; it is not space reclamation.

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

Notification association follows the saved operation/batch `caller_run`, even after the
execution ends. Without a parent task, operation/batch IDs provide source-scoped group
keys. The media adapter owns these links; generic notifications owns grouping, paging
and read state. Legacy foreground-attributed notices are linked using durable receipts
through the internal notification capability, without republishing or altering content.

Quark acceptance on 2026-10-21: an isolated protocol probe copied one selected episode
(809,628,675 bytes) in 1.65 seconds, verified the returned task and destination file ID,
and preserved the receipt. The production adapter subsequently read the real account,
capacity, all 20 first-season share entries, exact file metadata and configured-root
ancestry. An isolated host/operation DB recovered that saved task to `completed` using
only `copy_status` and `list` (copy was prohibited), with destination ID/name/size verified.
The correct single-file endpoint is `/file/info?fid=...`; `/file?fids=...` returned an
unrelated list in real testing and must not be used. Protocol fixtures cover account
mismatch, Cookie rotation, incomplete lists, lost copy replies and cancellation.
The later installed-service acceptance discovered all 24 MCP tools with both accounts
ready, inspected all 20 episode entries, created the show/season folders through the
standard preview/apply tools, and transferred the same selected episode into the Quark
series target in 12.24 seconds. Exact destination ID/name/size matched. Existing media
configuration and 40 operation/38 asset records were converted once from a tested private
backup, retaining operation IDs and states. Full gate: 790 tests, zero skips, plus static
checks and frontend build. After restart all plugins were ready and voice was standby /
audio-ready. OpenList verification correctly reported `openlist_target_outside_account_root`
because the configured service account is restricted to the local library; no permission
was broadened. Whole-file Quark download and physical playback remain unaccepted.

Quark download/management extension acceptance (2026-10-21): the original download
endpoint returned a valid MD5 for the 809,628,675-byte sample, and Range reads returned
HTTP 206 with the exact total size. The same URL without Cookie returned 412. Through
the shared downloader, an interrupted real read resumed at byte 10,476,563 and read
another 457,741 bytes. Cancellation removed its partial and published no library file.
Observed throughput was approximately 0.1 MB/s; the full cloud file was not downloaded,
so end-to-end cloud checksum completion and playback remain unaccepted. Real-HTTP local
fixtures cover full MD5 equality/corruption, resume, changed source, origin-bound Cookie
handling and rejected redirects; they do not replace that remaining cloud acceptance.
The shared library preview/apply/readback path renamed and moved the dedicated test
copy, restored its original name/parent and refused a destination-name conflict before
writing. No existing user file was deleted; the private acceptance directories remain.
Full gate: 796 tests, zero skips, plus static checks and frontend build. After backing
up config/DB, the idle installed service restarted with Quark local targets on `/mnt/Q`;
all plugins and both accounts were ready. Installed MCP downloaded 211,987 observed
bytes with MD5 metadata and cancelled the same operation successfully. Initial installed
acceptance found a missing configured staging directory; after creating it, explicit
resume reused the same operation ID. No cloud transfer was repeated.

Quark recycle acceptance (2026-10-21): a dedicated test copy was submitted with
`action_type=2` to `/file/delete`. The account recycle list returned the original
file ID and a distinct `record_id`; `/file/recycle/recover` accepted
`select_mode=2, record_list=[record_id]` and returned an asynchronous task. Immediate
file-info returned 404 while recovery was pending, then the original ID, name, parent,
size and provider MD5 were unchanged. The shared library path separately completed
trash/restore, blocked an introduced name conflict before submission, and recovered
from a simulated lost restore acknowledgment by observation with exactly one restore
request. Test copies were restored; no permanent-delete request was made. Deep/expired
file recovery, unrelated recycle entries and automatic fallback directories are not
part of this capability. Fixtures cover stale record identity, missing/expired entries,
incomplete listings, missing parents, conflicts introduced after preview and lost replies.
Full gate: 800 tests, zero skips, plus static checks and frontend build. The idle
installed service was backed up and restarted; capability discovery exposes Quark
trash/restore through the existing tools, with permanent deletion still disabled.
