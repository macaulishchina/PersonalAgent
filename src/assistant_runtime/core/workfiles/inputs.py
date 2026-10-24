"""Durable, caller-owned snapshots for execution inputs."""

from assistant_runtime.api.system import system

import base64
import hashlib
import io
from pathlib import Path
import re
import uuid

from assistant_runtime.api.ai_usage import current_usage
from assistant_runtime.api.plugins import current_call

MAX_BYTES = 2 * 1024 * 1024


def caller():
    identity, usage = current_call.get(), current_usage.get()
    return identity.work_id if identity else usage.work_id if usage else "operator"


class InputFiles:
    def __init__(self, root, db, *, media=None):
        self.root, self.db, self.media = root / "inputs", db, media
        db.execute(
            "CREATE TABLE IF NOT EXISTS input_grants (id TEXT NOT NULL, owner TEXT NOT NULL, PRIMARY KEY(id,owner))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS input_files "
            "(id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, "
            "kind TEXT NOT NULL, digest TEXT NOT NULL, size INTEGER NOT NULL)"
        )
        db.commit()

    async def request(self, request):
        args = request.arguments
        if request.action == "put":
            if set(args) - {"name", "data", "kind"}:
                raise ValueError("Unknown input file fields")
            name, kind, encoded = args.get("name"), args.get("kind", "file"), args.get("data")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[^/\\\x00-\x1f]{1,128}", name)
                or name in {".", ".."}
                or kind not in {"file", "image"}
            ):
                raise ValueError("Invalid input file name or kind")
            if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                raise ValueError("Input file must be at most 2 MiB")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise ValueError("Invalid input file encoding") from None
            if not raw or len(raw) > MAX_BYTES:
                raise ValueError("Input file must be nonempty and at most 2 MiB")
            if kind == "image":
                self.image_type(raw)
            ident = uuid.uuid4().hex
            path = self.path(ident)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with path.open("xb") as stream:
                    system.private(path)
                    stream.write(raw)
                with self.db:
                    self.db.execute(
                        "INSERT INTO input_files VALUES (?,?,?,?,?,?)",
                        (ident, caller(), name, kind, hashlib.sha256(raw).hexdigest(), len(raw)),
                    )
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return self.get(ident)
        if request.action == "get" and set(args) == {"id"}:
            return self.get(args["id"])
        raise ValueError("Unknown input file operation")

    def path(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise ValueError("Invalid input file reference")
        path = self.root / ident
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise ValueError("Input storage cannot traverse symlinks")
        return path

    def get(self, ident):
        self.path(ident)
        row = self.db.execute("SELECT * FROM input_files WHERE id=?", (ident,)).fetchone()
        if row is None:
            if self.media is None:
                raise ValueError("Input file is unavailable to this caller")
            record = self.media.image_record(ident)
        else:
            record = dict(zip(("id", "owner", "name", "kind", "sha256", "bytes"), row))
        if (
            caller() not in {"operator", record["owner"]}
            and not self.db.execute(
                "SELECT 1 FROM input_grants WHERE id=? AND owner=?", (ident, caller())
            ).fetchone()
        ):
            raise ValueError("Input file is unavailable to this caller")
        return record

    def grant(self, references, owner):
        """Trusted owning module delegates already validated references, never a model operation."""
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO input_grants VALUES (?,?)", [(ref["id"], owner) for ref in references]
            )

    def capture(self, references):
        if not isinstance(references, list) or len(references) > 8:
            raise ValueError("At most eight registered input files are allowed")
        records = [self.get(ident) for ident in references]
        if len({r["id"] for r in records}) != len(records):
            raise ValueError("Duplicate input file references")
        if sum(r["kind"] == "image" for r in records) > 4:
            raise ValueError("At most four input images are allowed")
        return records

    def materialize(self, records, area):
        inputs, descriptions = [], []
        for record in records:
            if record.get("storage") == "media":
                raw = self.media.image_bytes(record["id"])
            else:
                source = self.path(record["id"])
                if source.stat().st_size != record["bytes"]:
                    raise ValueError("Registered input file changed")
                raw = source.read_bytes()
            if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError("Registered input file changed")
            # Do not use the display name as a filesystem path or an instruction.
            path = area / "scratch" / ("input-" + record["id"] + Path(record["name"]).suffix)
            if path.is_symlink():
                raise ValueError("Input destination cannot be a symlink")
            with path.open("xb") as stream:
                system.private(path)
                stream.write(raw)
            if record["kind"] == "image":
                inputs.append(
                    {
                        "type": "image",
                        "data": base64.b64encode(raw).decode(),
                        "mimeType": self.image_type(raw),
                    }
                )
            descriptions.append({"name": record["name"], "path": str(path), "kind": record["kind"]})
        return inputs, descriptions

    @staticmethod
    def image_type(raw):
        try:
            from PIL import Image
        except ImportError:
            raise ValueError("Image inputs require the Pillow dependency") from None
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 20000000:
                    raise ValueError("Unsupported input image")
                mime = Image.MIME[image.format]
                image.verify()
                return mime
        except (OSError, SyntaxError, Image.DecompressionBombError):
            raise ValueError("Invalid input image") from None
