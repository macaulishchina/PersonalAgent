"""Media-owned nested inputs; platform capabilities and path safety stay with adapters."""

from assistant_runtime.mcp.contracts import object_schema, nullable

TEXT = {"type": "string"}


def change(action, *fields):
    return object_schema(
        {"action": {"const": action}, **{name: TEXT for name in fields}}, ["action", *fields]
    )


# anyOf is intentional: restore(file_id) is shared across cloud/local backends.
CHANGES = {
    "type": "array",
    "minItems": 1,
    "maxItems": 100,
    "items": {
        "anyOf": [
            change("mkdir", "folder_id", "name"),
            change("rename", "file_id", "name"),
            change("move", "file_id", "folder_id"),
            change("trash", "file_id"),
            change("trash", "folder_id"),
            change("restore", "file_id"),
            change("mkdir", "path"),
            change("rename", "path", "name"),
            change("move", "path", "folder"),
            change("trash", "path"),
        ]
    },
    "description": "Native cloud trash(file_id) affects one file; trash(folder_id) explicitly selects the entire directory. Local/OpenList use relative paths and their declared capabilities. No permanent deletion.",
}
CHANGES["examples"] = [[{"action": "rename", "file_id": "example-file", "name": "example.mkv"}]]

COLLECTION = object_schema(
    {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "season": {"type": "integer", "minimum": 0, "maximum": 100000},
        "total_episodes": nullable({"type": "integer", "minimum": 1, "maximum": 10000}),
        "evidence": {"type": "string", "minLength": 1, "maxLength": 2000},
        "required_state": {"enum": ["cloud", "local"]},
    },
    ["title", "season", "total_episodes", "evidence", "required_state"],
)

BATCH_ITEMS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 100,
    "items": {
        "anyOf": [
            object_schema({"file_id": TEXT}, ["file_id"]),
            object_schema({"path": TEXT}, ["path"]),
            object_schema(
                {
                    "file_id": TEXT,
                    "share_url": TEXT,
                    "password": TEXT,
                    **{
                        name: nullable(TEXT)
                        for name in ("folder_id", "name", "collection", "version", "parent_file_id")
                    },
                    "season": nullable({"type": "integer"}),
                    "episode": nullable({"type": "integer"}),
                },
                ["file_id"],
            ),
        ]
    },
    "description": "Transfer entries use transfer_media fields except request_id/target. Download entries use exactly file_id (native) or path (OpenList). Action and backend checks remain authoritative.",
}
