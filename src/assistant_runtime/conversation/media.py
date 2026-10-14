"""Private image attachments shared by user uploads and provider replies."""

import asyncio
import hashlib
import io
import re
from pathlib import Path
import tempfile
import time
import uuid

MAX_BYTES = 10 * 1024 * 1024


class ImageAttachments:
    def __init__(self, state_dir, db):
        self.folder, self.db = state_dir / "web-uploads", db

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
                    temporary.chmod(0o600)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
        return ident, created

    async def add(self, data, name, *, key=None, attached=False):
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
