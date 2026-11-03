"""Application-owned working files, not arbitrary filesystem cleanup."""

import asyncio
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from assistant_runtime.api import plugin_manifest


class WorkFiles:
    def __init__(self, state, db, settings=None):
        settings = settings or {}
        if set(settings) - {
            "root",
            "retention_days",
            "artifact_storage_bytes",
            "result_retention_days",
            "result_storage_bytes",
            "result_max_bytes",
        }:
            raise ValueError("Unknown work_files settings")
        self.root = Path(settings.get("root", state / "work")).expanduser().absolute()
        self.artifact_storage_bytes = settings.get("artifact_storage_bytes", 512 * 1024 * 1024)
        if type(self.artifact_storage_bytes) is not int or not 0 <= self.artifact_storage_bytes <= 1024**4:
            raise ValueError("artifact_storage_bytes must be 0..1099511627776")
        self.days = settings.get("retention_days", 7)
        if type(self.days) is not int or not 1 <= self.days <= 365:
            raise ValueError("retention_days must be 1..365")
        from .inputs import InputFiles
        from .media import MediaAttachments

        self.media = MediaAttachments(state, db)
        self.inputs = InputFiles(self.root, db, media=self.media)
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS work_areas (owner TEXT PRIMARY KEY, ended REAL)")
        db.commit()
        from .artifacts import ArtifactFiles

        self.publications = ArtifactFiles(self)
        from .tool_media import ToolMedia

        self.tool_media = ToolMedia(self)
        from .results import ResultFiles

        self.results = ResultFiles(self, settings)
        self.media.on_read = self.results.touch_attachment

    async def request(self, request):
        if request.action.startswith("result_"):
            return await self.results.request(request)
        if request.action == "finalize" or request.action.startswith("artifact_"):
            return await self.publications.request(request)
        return await self.inputs.request(request)

    def area(self, owner):
        if not re.fullmatch(r"(?:tasks|conversations|executions)/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)?", owner):
            raise ValueError("Invalid work area owner")
        base = self.root / owner
        for p in [self.root, *base.parents, base, base / "scratch", base / "artifacts"]:
            if p.is_symlink():
                raise ValueError("Work area cannot traverse symlinks")
        for name in ("scratch", "artifacts"):
            (base / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO work_areas VALUES (?,NULL)", (owner,))
        return base

    def finish(self, owner):
        with self.db:
            self.db.execute("UPDATE work_areas SET ended=? WHERE owner=?", (time.time(), owner))

    def retain(self, owner):
        with self.db:
            self.db.execute("UPDATE work_areas SET ended=NULL WHERE owner=?", (owner,))

    def cleanup(self, apply=False, defer_delete=False):
        candidates = []
        retired = []
        for (owner,) in self.db.execute(
            "SELECT owner FROM work_areas WHERE ended<?", (time.time() - self.days * 86400,)
        ):
            p = self.root / owner / "scratch"
            if p.is_symlink() or any(a.is_symlink() for a in p.parents) or not p.exists():
                continue
            candidates.append(str(p))
            if apply:
                trash = self.root / ".trash"
                if trash.is_symlink():
                    raise ValueError("Work trash cannot be a symlink")
                trash.mkdir(exist_ok=True, mode=0o700)
                destination = trash / uuid.uuid4().hex
                p.rename(destination)
                retired.append(destination)
        if not defer_delete:
            self.purge(retired)
        return {
            "eligible_scratch": candidates,
            "deleted": apply,
            "retention_days": self.days,
            **({"retired": retired} if defer_delete else {}),
        }

    @staticmethod
    def purge(paths):
        for path in paths:
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path)

    async def collect(self, apply=False):
        result = self.cleanup(apply, defer_delete=True)
        retired = result.pop("retired")
        # Expensive recursive deletion runs off the audio/control event loop.
        await asyncio.to_thread(self.purge, retired)
        return result

    async def usage(self):
        owners = [row[0] for row in self.db.execute("SELECT owner FROM work_areas")]

        def measure():
            totals = {"scratch_bytes": 0, "artifact_bytes": 0}
            for owner in owners:
                for category, key in (("scratch", "scratch_bytes"), ("artifacts", "artifact_bytes")):
                    base = self.root / owner / category
                    if base.is_symlink() or any(p.is_symlink() for p in base.parents):
                        continue
                    for folder, _, files in os.walk(base, followlinks=False):
                        for name in files:
                            file = Path(folder) / name
                            try:
                                if not file.is_symlink():
                                    totals[key] += file.stat().st_size
                            except FileNotFoundError:
                                pass
            return totals

        return await asyncio.to_thread(measure)

    def artifacts(self, owner):
        base = self.area(owner) / "artifacts"
        return [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in base.iterdir()
            if p.is_file() and not p.is_symlink()
        ][:100]


class WorkFilesMaintenance:
    """Application-wide retention, independent from the optional tasks plugin."""

    manifest = plugin_manifest(__file__)

    def __init__(self, files):
        self.files = files

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationError
        from assistant_runtime.core.configuration.updates import attributes

        root = Path(config.work_files.get("root", config.state_dir / "work")).expanduser().absolute()
        if root != self.files.root:
            raise ConfigurationError(["work_files", "root"], "migration", "工作文件目录变更需要本地迁移")
        days = config.work_files.get("retention_days", 7)
        if type(days) is not int or not 1 <= days <= 365:
            raise ConfigurationError(["work_files", "retention_days"], "range", "保留时间必须为 1–365 天")
        quota = config.work_files.get("artifact_storage_bytes", 512 * 1024 * 1024)
        if type(quota) is not int or not 0 <= quota <= 1024**4:
            raise ConfigurationError(
                ["work_files", "artifact_storage_bytes"], "range", "Invalid artifact quota"
            )
        from .results import settings

        result_days, result_quota, maximum = settings(config.work_files)
        return attributes(
            [
                (self.files, "days", days),
                (self.files, "artifact_storage_bytes", quota),
                (self.files.results, "days", result_days),
                (self.files.results, "quota", result_quota),
                (self.files.results, "maximum", maximum),
            ]
        )

    async def start(self, context):
        from assistant_runtime.api.files import FileRequest

        context.register(FileRequest, self.files.request)
        context.spawn(self.maintain(), "retention")

    async def maintain(self):
        while True:
            await self.files.collect(True)
            await self.files.publications.collect()
            try:
                await self.files.results.collect()
            except (OSError, ValueError):
                import logging

                logging.getLogger(__name__).warning(
                    "Result cleanup incomplete; will retry next maintenance cycle"
                )
            await asyncio.sleep(3600)
