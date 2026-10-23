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
