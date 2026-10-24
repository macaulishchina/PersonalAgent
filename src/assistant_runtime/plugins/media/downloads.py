"""Bounded original-file downloads. Private staging and atomic, no-clobber publication."""

from assistant_runtime.api.system import system

import asyncio
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

import aiohttp

from .errors import MediaError
from .private import disk
from .library import filename


class LocalFiles:
    def __init__(self, destination, operation_id, name, minimum_free):
        self.destination, self.name, self.minimum_free = destination, name, minimum_free
        self.partial = operation_id + ".part"
        self.library = self.stage = self.fd = self.final = None

    def open(self):
        if (
            not isinstance(self.name, str)
            or not self.name
            or self.name in {".", ".."}
            or len(self.name.encode()) > 255
            or any(c in self.name for c in "/\\")
            or any(ord(c) < 32 for c in self.name)
        ):
            raise MediaError("unsafe_media_filename")
        mount = Path(self.destination["mount_path"])
        if not mount.is_mount():
            raise MediaError("download_mount_unavailable")
        for key, field in (("path", "library"), ("staging_path", "stage")):
            path = Path(self.destination[key])
            if path.resolve() != path or not path.is_relative_to(mount):
                raise MediaError("download_path_changed")
            descriptor = system.directory(path)
            setattr(self, field, descriptor)
            if descriptor.stat().st_dev != mount.stat().st_dev:
                raise MediaError("download_filesystem_changed")
        for part in self.destination.get("relative_parts", []):
            filename(part)
            try:
                self.library.mkdir(part, 0o755)
            except FileExistsError:
                pass
            child = self.library.child(part)
            if child.stat().st_dev != mount.stat().st_dev:
                child.close()
                raise MediaError("download_filesystem_changed")
            self.library.close()
            self.library = child
        self.fd = self.stage.open_file(self.partial, os.O_RDWR | os.O_CREAT)
        if not stat.S_ISREG(os.fstat(self.fd).st_mode) or os.fstat(self.fd).st_nlink not in {1, 2}:
            raise MediaError("unsafe_download_partial")
        return os.fstat(self.fd).st_size

    def space(self, remaining):
        if self.stage.free_bytes() < remaining + self.minimum_free:
            raise MediaError("local_capacity_exhausted")

    def write(self, block):
        view = memoryview(block)
        while view:
            count = os.write(self.fd, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]

    def checkpoint(self):
        os.fsync(self.fd)

    def final_descriptor(self):
        try:
            fd = self.library.open_file(self.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise MediaError("destination_name_conflict")
        self.final = fd
        return fd

    def close_final(self):
        if self.final is not None:
            os.close(self.final)
            self.final = None

    def publish(self):
        # link is atomic and refuses any pre-existing destination. A crash after
        # link but before the DB commit is recovered by hashing that final file.
        os.fsync(self.fd)
        try:
            self.stage.link(self.partial, self.library, self.name)
        except FileExistsError:
            raise MediaError("destination_name_conflict") from None
        self.library.sync()
        self.remove_partial()

    def remove_partial(self):
        try:
            self.stage.unlink(self.partial)
        except FileNotFoundError:
            pass

    def close(self):
        for field in ("final", "fd", "stage", "library"):
            value = getattr(self, field)
            if value is not None:
                value.close() if field in {"stage", "library"} else os.close(value)
                setattr(self, field, None)


class Downloads:
    def __init__(self, plugin):
        self.plugin = plugin
        self.repo, self.settings = plugin.repo, plugin.settings

    def cancelled(self, ident):
        return self.repo.get(ident)["data"].get("cancel_requested", False)

    async def digest(self, fd, algorithm, ident):
        await disk(os.lseek, fd, 0, os.SEEK_SET)
        digest = hashlib.new(algorithm)
        while True:
            if self.cancelled(ident):
                raise MediaError("download_cancelled")
            block = await disk(os.read, fd, 2 * 1024**2)
            if not block:
                return digest.hexdigest()
            await disk(digest.update, block)

    async def process(self, item):
        ident, intent = item["id"], item["data"]["intent"]
        if (
            self.settings.targets.get(intent["target"])
            != intent.get("root_destination", intent["destination"])
            or self.settings.local_targets.get(intent["target"]) != intent["local_destination"]
        ):
            raise MediaError("target_configuration_changed")
        local_destination = {
            **intent["local_destination"],
            "relative_parts": intent.get("relative_parts", []),
        }
        if "root_destination" in intent:
            dest, parts = await self.plugin.library.destination(
                intent["target"], intent["destination"]["folder_id"]
            )
            if dest != intent["destination"] or parts != intent["relative_parts"]:
                raise MediaError("download_path_changed")
        if self.cancelled(ident):
            previous = item["data"].get("file")
            if previous:
                files = LocalFiles(local_destination, ident, previous["name"], 0)
                try:
                    await disk(files.open)
                    await disk(files.remove_partial)
                finally:
                    await disk(files.close)
            self.repo.change(ident, "cancelled")
            return
        self.repo.change(ident, "checking")
        backend = intent.get("file_backend", "native")
        if backend != self.settings.files_for(intent["destination"]["account"]):
            raise MediaError("target_configuration_changed")
        if backend == "openlist":
            from .openlist_library import OpenListLibrary

            library = OpenListLibrary(self.plugin.library)
            if library.binding(intent["target"]) != intent["backend_binding"]:
                raise MediaError("target_configuration_changed")
            download = await library.files(intent["target"]).download(intent["path"])
        else:
            download = await self.plugin.provider.call(
                "download_url", {**intent["destination"], "file_id": intent["file_id"]}
            )
        metadata = download["file"]
        if (
            metadata.get("type") != "file"
            or metadata.get("category") != "video"
            or type(metadata.get("size")) is not int
            or metadata["size"] <= 0
        ):
            raise MediaError("source_must_be_a_verifiable_video_file")
        algorithm = (metadata.get("content_hash_name") or "").lower()
        expected = (metadata.get("content_hash") or "").lower()
        widths = {"md5": 32, "sha1": 40, "sha256": 64}
        if algorithm not in widths or not re.fullmatch("[0-9a-f]{%d}" % widths[algorithm], expected):
            raise MediaError("download_checksum_unavailable")
        previous = item["data"].get("file")
        if previous and metadata != previous:
            raise MediaError("download_source_changed")
        self.repo.change(ident, "checking", file=metadata)
        files = LocalFiles(local_destination, ident, metadata["name"], self.settings.download_min_free_bytes)
        try:
            offset = await disk(files.open)
            if self.cancelled(ident):
                raise MediaError("download_cancelled")
            final = await disk(files.final_descriptor)
            if final is not None:
                try:
                    if (await disk(os.fstat, final)).st_size != metadata["size"] or await self.digest(
                        final, algorithm, ident
                    ) != expected:
                        raise MediaError("destination_name_conflict")
                finally:
                    await disk(files.close_final)
                await disk(files.remove_partial)
                self.complete(ident, metadata)
                return
            if offset > metadata["size"]:
                raise MediaError("download_partial_invalid")
            if (await disk(os.fstat, files.fd)).st_nlink != 1:
                raise MediaError("unsafe_download_partial")
            await disk(files.space, metadata["size"] - offset)
            await disk(os.lseek, files.fd, offset, os.SEEK_SET)
            if offset < metadata["size"]:
                await self.receive(ident, download, metadata, files, offset)
            self.repo.change(ident, "verifying")
            if await self.digest(files.fd, algorithm, ident) != expected:
                # Never resume corrupted bytes or expose them in the library.
                await disk(files.remove_partial)
                raise MediaError("download_checksum_mismatch")
            if self.cancelled(ident):
                raise MediaError("download_cancelled")
            await disk(files.publish)
            self.complete(ident, metadata)
        except MediaError as exc:
            if exc.code == "download_cancelled" and files.stage is not None:
                await disk(files.remove_partial)
            raise
        finally:
            await disk(files.close)

    def complete(self, ident, metadata):
        item = self.repo.get(ident)
        intent = item["data"]["intent"]
        path = str(
            Path(intent["local_destination"]["path"]).joinpath(
                *intent.get("relative_parts", []), metadata["name"]
            )
        )
        self.repo.change(
            ident,
            "completed",
            verification="content_hash",
            local_path=path,
            progress={
                "downloaded_bytes": metadata["size"],
                "total_bytes": metadata["size"],
                "percent": 100.0,
            },
            error=None,
        )
        self.plugin.library.remember(
            intent["target"], metadata, local_path=path, local_verified=True, trashed=False
        )
        self.plugin.reporter.offer(
            type="media.download_completed",
            subject="media:" + ident,
            event_id="media-download:" + ident,
            data={"operation_id": ident, "target": item["data"]["intent"]["target"]},
        )

    async def receive(self, ident, result, metadata, files, offset):
        url = result.get("url", "")
        parsed = urlsplit(url)
        intent = self.repo.get(ident)["data"]["intent"]
        proxy = False
        if intent.get("file_backend") == "openlist":
            from .openlist_library import OpenListLibrary
            from urllib.parse import quote

            library = OpenListLibrary(self.plugin.library)
            binding = library.binding(intent["target"])
            expected = (
                binding["url"] + "/p" + quote(library.files(intent["target"]).path(intent["path"]), safe="/")
            )
            proxy = url.split("?", 1)[0] == expected and binding == intent["backend_binding"]
            if not proxy:
                raise MediaError("invalid_original_download_url")
        if (
            (parsed.scheme != "https" and not (proxy and parsed.scheme == "http"))
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise MediaError("invalid_original_download_url")
        provider_headers = result.get("headers", {})
        if proxy and provider_headers:
            raise MediaError("invalid_download_headers")
        if (
            not isinstance(provider_headers, dict)
            or set(provider_headers) - {"Referer", "User-Agent", "Cookie"}
            or any(
                not isinstance(value, str)
                or len(value) > (32768 if key == "Cookie" else 2000)
                or "\r" in value
                or "\n" in value
                for key, value in provider_headers.items()
            )
            or (
                "Cookie" in provider_headers and result.get("credential_origin") != f"https://{parsed.netloc}"
            )
        ):
            raise MediaError("invalid_download_headers")
        headers = {
            **provider_headers,
            "Accept-Encoding": "identity",
            "Range": f"bytes={offset}-",
        }
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
        self.progress(ident, offset, metadata["size"], 0)
        started, initial, checkpoint = time.monotonic(), offset, time.monotonic()
        try:
            # Only adapter-origin-bound cookies; no redirects, Authorization or
            # compressed bodies. URLs/headers are never persisted.
            async with self.plugin.download_session.get(
                url, headers=headers, timeout=timeout, allow_redirects=False, auto_decompress=False
            ) as response:
                if proxy and response.status == 403:
                    # The configured OpenList mount may forbid proxying. Its 403
                    # can also originate upstream; do not infer an expired URL or
                    # repeatedly retry a permission/configuration failure.
                    raise MediaError("openlist_proxy_forbidden")
                if response.status in {401, 403, 410}:
                    raise MediaError("download_url_expired")
                if response.status == 429 or response.status >= 500:
                    raise MediaError("provider_unavailable")
                end = metadata["size"] - 1
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise MediaError("invalid_download_encoding")
                if response.status == 206:
                    if response.headers.get("Content-Range") != f"bytes {offset}-{end}/{metadata['size']}":
                        raise MediaError("invalid_download_range")
                elif response.status != 200 or offset:
                    raise MediaError("download_range_unsupported")
                length = response.headers.get("Content-Length")
                if length is not None and length != str(metadata["size"] - offset):
                    raise MediaError("download_size_mismatch")
                async for block in response.content.iter_chunked(1024**2):
                    if self.cancelled(ident):
                        raise MediaError("download_cancelled")
                    if offset + len(block) > metadata["size"]:
                        raise MediaError("download_size_mismatch")
                    await disk(files.write, block)
                    offset += len(block)
                    if time.monotonic() - checkpoint >= 2:
                        await disk(files.checkpoint)
                        await disk(files.space, metadata["size"] - offset)
                        checkpoint = time.monotonic()
                        self.progress(
                            ident,
                            offset,
                            metadata["size"],
                            (offset - initial) / max(0.01, checkpoint - started),
                        )
                await disk(files.checkpoint)
                self.progress(
                    ident,
                    offset,
                    metadata["size"],
                    (offset - initial) / max(0.01, time.monotonic() - started),
                )
                if offset != metadata["size"]:
                    raise MediaError("download_incomplete")
        except (aiohttp.ClientError, TimeoutError):
            raise MediaError("download_network_error") from None

    def progress(self, ident, offset, total, speed):
        self.repo.change(
            ident,
            "downloading",
            progress={
                "downloaded_bytes": offset,
                "total_bytes": total,
                "percent": round(offset * 100 / total, 2),
                "bytes_per_second": round(speed),
            },
        )

    async def worker(self):
        transient = {
            "download_network_error",
            "download_incomplete",
            "download_url_expired",
            "provider_network_error",
            "provider_unavailable",
            "rate_limited",
            "sdk_worker_timeout",
        }
        while True:
            item = self.repo.due("download")
            if not item:
                await asyncio.sleep(1)
                continue
            try:
                await self.process(item)
            except asyncio.CancelledError:
                raise  # Shutdown retains private partials for restart recovery.
            except Exception as exc:
                code = exc.code if isinstance(exc, MediaError) else "local_download_io_error"
                current = self.repo.get(item["id"])
                attempts = current["data"].get("attempts", 0) + 1
                state = "failed"
                if code == "download_cancelled":
                    state = "cancelled"
                elif code == "auth_required":
                    state = "waiting_auth"
                    self.repo.account_state(
                        "auth_required", account=item["data"]["intent"]["destination"]["account"]
                    )
                elif code in transient and attempts < 8:
                    state = "queued"
                self.repo.change(
                    item["id"], state, due=time.time() + min(300, 2**attempts), attempts=attempts, error=code
                )
