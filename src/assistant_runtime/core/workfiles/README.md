# Work files and registered inputs

This core plugin owns working directories, artifact listings, scratch retention and
registered input snapshots. It does not depend on conversation, devices or task scheduling.
Both presets expose the same `FileRequest` capability and `files put|get` CLI.

`put` accepts `name`, base64 `data`, and optional `kind` (`file` or `image`). Each input
is nonempty and at most 2 MiB. The host binds ownership; there is no arbitrary path,
URL, credential or owner parameter. Registration copies the bytes into private instance
storage and records SHA-256, size and a stable reference. `get` returns metadata only.
Plugins must declare `uses=(FileRequest,)` and use their bound context.

Executions accept up to eight references, including at most four images. Submission
checks ownership; dispatch verifies the captured digest and copies the inputs to that
execution's scratch area. Names are display metadata, not destination paths. Changed,
missing or cross-caller inputs cannot silently become different model input. Inputs are
not automatically replayed after restart. Original registered snapshots remain available
until instance storage is explicitly maintained; scratch retention never deletes them.

Generic files require no optional decoder and are available to allowed file tools. Image
registration requires Pillow (included in standard, optional for slim), validates PNG,
JPEG or WebP and bounds pixels to 20 million. Images also require an image-capable model;
they are sent as native image input, never silently converted to text. No audio dependency
is introduced. This is application resource ownership, not filesystem sandboxing.

The existing web/task media registry is now owned here as well. Its files and IDs remain
unchanged. Public execution accepts these image IDs through the same capture/digest
contract; trusted task ownership can grant the producing work access to registered
references. Model arguments cannot create these grants or choose arbitrary filesystem paths.
The 2 MiB upload limit applies to the public base64 registration endpoint; existing Web
image normalization/10 MiB admission remains unchanged.

## Immutable execution publications (development candidate)

`FileRequest` / `pa files` additionally provide:

| Action | Arguments |
| --- | --- |
| `finalize` | `finalize_id` (stable UUID), `execution_id`, `name` (one artifact filename), `expires_at` (UTC Unix seconds) |
| `artifact_get` | `id` |
| `artifact_read` | `id`, optional `offset`, `length` (1..1,048,576 bytes) |
| `artifact_retain` | `id`, later finite `expires_at` |
| `artifact_release` | `id` |

The source is an owned execution's registered artifact directory, never an arbitrary path.
A known terminal outcome is required. Unknown/active executions cannot publish through
this initial interface. The caller uses existing execution ownership; this is not the
future multi-party resource-grant authorization API.

Publication returns an independent stable ID, execution ID, bytes, SHA-256, state and expiry.
A duplicate finalize intent returns the original publication; changed fields conflict.
Changing the source or publishing its name again with another ID does not mutate old bytes.
Reads return base64 data, offset/next_offset and eof, with the fixed total size and digest.
Disconnected archives can resume and verify the final digest; download is not an archival
acknowledgement. Publication expiry is explicit and independent of scratch retention.

The initial snapshot backend is **Windows only**, using a pinned directory and a read handle
that denies concurrent writes/deletion/replacement. Hard links and reparse sources are
rejected. Linux's existing input/work-area functions are unchanged; frozen publication is
explicitly unavailable there until a suitable backend is implemented and accepted.

Each object is bounded to 64 MiB, each read to 1 MiB. `work_files.artifact_storage_bytes`
sets the total publication allowance (default 512 MiB, 0 disables new publications).
Admission conservatively reserves 64 MiB for a publication in progress. Lowering the limit
only affects new admissions; it does not remove existing objects. Release/expiry collection
is serialized with reads and finalization; tombstones preserve idempotency after collection.

Bytes are flushed before the published receipt is committed. Incomplete finalizations found
on restart become `unavailable`; retrying the same intent cannot silently snapshot a changed
source. Read integrity is checked before returning bytes. Host storage corruption is reported,
not served as changed content. This publication API does not sandbox existing full-permission
executions and does not certify arbitrary host/plugin writes as impossible.

Source and CLI integration are tested on Windows Server with synthetic artifacts. Cross-round
resource leases, grant revocation, reliable external archival acknowledgement and the full
context lifecycle remain part of the joint design; this is the publication foundation.
