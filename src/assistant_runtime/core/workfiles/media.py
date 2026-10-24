"""Private chat media, shared by uploads, requested captures and recorded clips."""

from assistant_runtime.api.system import system

import asyncio
import hashlib
import io
import re
from pathlib import Path
import tempfile
import time
import uuid

MAX_BYTES = 10 * 1024 * 1024


class MediaAttachments:
    def __init__(self, state_dir, db):
        self.folder, self.db = state_dir / "web-uploads", db
        self.recordings = state_dir / "video-recordings"
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_uploads (id TEXT PRIMARY KEY, filename TEXT NOT NULL, "
            "media_type TEXT NOT NULL, created REAL NOT NULL, attached INTEGER NOT NULL DEFAULT 0)"
        )
        db.commit()

    def resolve(self, references):
        media = []
        if not isinstance(references, list):
            return media
        for reference in references[:4]:
            ident = reference.get("id") if isinstance(reference, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
                continue
            row = self.db.execute(
                "SELECT filename,media_type FROM chat_uploads WHERE id=?", (ident,)
            ).fetchone()
            if row and (self.folder / ident).is_file():
                with self.db:
                    self.db.execute("UPDATE chat_uploads SET attached=1 WHERE id=?", (ident,))
                media.append({"id": ident, "name": row[0], "type": row[1]})
        return media

    def prepare(self, data, key):
        from PIL import Image, ImageOps

        if not data or len(data) > MAX_BYTES:
            raise ValueError("单张图片不能超过 10 MB")
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ("JPEG", "PNG", "WEBP") or image.width * image.height > 20_000_000:
                raise ValueError("请使用不超过 2000 万像素的 JPEG、PNG 或 WebP 图片")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((2048, 2048))
            image = image.convert("RGB")
            normalized = io.BytesIO()
            image.save(normalized, format="JPEG", quality=90)
        ident = hashlib.sha256(key.encode() + b"\0" + data).hexdigest()[:32] if key else uuid.uuid4().hex
        self.folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = self.folder / ident
        created = not path.exists()
        if created:
            with tempfile.NamedTemporaryFile(dir=self.folder, delete=False) as f:
                temporary = Path(f.name)
                try:
                    f.write(normalized.getvalue())
                    system.private(temporary)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        return ident, created

    async def add_image(self, data, name, *, key=None, attached=False):
        # Finish the bounded worker before owned cleanup; never leave a late write
        # racing service restart or the expiration sweep.
        worker = asyncio.create_task(asyncio.to_thread(self.prepare, bytes(data), key))
        try:
            ident, created = await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                ident, created = await worker
                if created:
                    (self.folder / ident).unlink(missing_ok=True)
            except Exception:
                pass
            raise
        filename = Path(name or "图片").name[:120]
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO chat_uploads VALUES(?,?,?,?,?)",
                    (ident, filename, "image/jpeg", time.time(), int(attached)),
                )
                if attached:
                    self.db.execute("UPDATE chat_uploads SET attached=1 WHERE id=?", (ident,))
        except Exception:
            if created:
                (self.folder / ident).unlink(missing_ok=True)
            raise
        return {"id": ident, "name": filename, "type": "image/jpeg"}

    async def add_video(self, clip, name):
        """Adopt only a trusted, completed local recording; never a model file path."""
        from assistant_runtime.api.contracts import MAX_VIDEO_BYTES

        source = clip.path
        if (
            source.is_symlink()
            or source.resolve().parent != self.recordings.resolve()
            or not re.fullmatch(r"[a-f0-9]{32}\.mp4", source.name)
            or not source.is_file()
            or source.stat().st_size != clip.byte_count
            or not 0 < clip.byte_count <= MAX_VIDEO_BYTES
            or clip.mime_type != "video/mp4"
        ):
            raise ValueError("Invalid video recording source")
        ident = source.stem
        self.folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination = self.folder / ident
        worker = asyncio.create_task(asyncio.to_thread(source.replace, destination))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            destination.unlink(missing_ok=True)
            raise
        filename = Path(name).name[:120]
        try:
            system.private(destination)
            with self.db:
                self.db.execute(
                    "INSERT INTO chat_uploads VALUES(?,?,?,?,?)",
                    (ident, filename, "video/mp4", time.time(), 0),
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {"id": ident, "name": filename, "type": "video/mp4"}

    def discard_video(self, clip):
        source = clip.path
        if (
            not source.is_symlink()
            and source.resolve().parent == self.recordings.resolve()
            and re.fullmatch(r"[a-f0-9]{32}\.mp4", source.name)
        ):
            source.unlink(missing_ok=True)

    def image_record(self, ident):
        references = self.resolve([{"id": ident}])
        if not references or not references[0]["type"].startswith("image/"):
            raise ValueError("Registered image is unavailable")
        path = self.folder / ident
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Registered image cannot traverse symlinks")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Registered image is too large")
        raw = path.read_bytes()
        return {
            "id": ident,
            "owner": "operator",
            "name": references[0]["name"],
            "kind": "image",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "storage": "media",
        }

    def image_bytes(self, ident):
        self.image_record(ident)
        return (self.folder / ident).read_bytes()
