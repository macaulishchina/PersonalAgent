# 模块历史验收记录

从模块 README 迁出的原有证据，按原日期保存。旧执行器、配置、供应商状态和测试数字不代表当前版本；现行方法见[开发与验证](../development/testing.md)。未重新执行这些验收。

## Configuration - 2026-10-23

Acceptance on 2026-10-23: the final full gate passed **1,022 tests, zero skips**, including
format, lint, types and the chat frontend build. Independent environments verified both
presets from a locked repository install and an installed wheel. A fresh hash-locked
Python environment plus separately lock-installed Pi 1.4.0 passed the four synthetic
host/image/tool/delegation/fallback tests. The installed console also passed desktop/mobile
browser login, credential redaction and frozen reads, external sample configuration/resource
save, confirmed restart and damaged-file repair, with zero JavaScript errors. No production
cutover, paid provider request or real device/media operation was performed.

## Decisions - 2026-10-21

### Acceptance evidence (2026-10-21)

The reviewed implementation passed the full gate: 776 tests with no skips, including format/lint/types and the
frontend build. Isolated Jev synthetic correction/takeover judgments passed after
metadata warmup; a cold request also exercised the unchanged 3-second timeout and
backoff. These few examples are not an accuracy estimate.

A real isolated subscription thread accepted context-only injection both before
and during a turn without creating another turn. Interrupt followed by provider
readback reached a stopped checkpoint. This does not establish that native voice
handoffs expose an approval gate, or that an issued external action can be undone.

After idle restart, a synthetic request through the installed local capability
transport returned `correct` in 258 ms and produced linked request/result/adoption
archive records. Its outcome was explicitly rejected as `synthetic_no_effect`;
no device action or audio playback was requested. Instance policy is 0.6 globally,
0.2 for model review, concurrency 4 and a 3-second active deadline. The existing
unknown historical task was retained, not replayed. By user choice, physical voice correction, supplement and takeover will be assessed
through normal daily use and subsequent log/feedback review; no dedicated voice
acceptance session is scheduled. The existing synthetic results do not establish
real-world voice accuracy.

## PipeWire - 2026-10-15

Framework acceptance (2026-10-15): two independent input streams and two output
streams ran through the registries against virtual PipeWire endpoints. Closing each
tool stream preserved its voice stream and resource lease; final shutdown released
all leases. Pin properties were present and the installed main microphone stream
was uninterrupted. This used no physical audio and does not certify new room
listening or playback features. Live stdio MCP inventory/query verification after
service restart preserved the app/configured voice binding and shared core/conversation.
The full development gate passed 284 tests, including device lifecycle regressions
in `tests/test_device_operations.py`.

## Media search - 2026-10-21

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

## Media transfer and download - 2026-10-16

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

## Quark - 2026-10-21

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
