"""Bounded file operations for managed workspaces; never arbitrary host paths."""

import hashlib
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager

from assistant_runtime.api.system import system
from assistant_runtime.platforms.directory import Directory, component
from .authority import uid

MAX_FILE = 4 * 1024 * 1024
MAX_READ = 128 * 1024


class ControlledFiles:
    def __init__(self, service):
        self.service = service
        self.base = service.files.root / "controlled"

    @staticmethod
    def available():
        if sys.platform != "win32":
            raise RuntimeError("isolation_unavailable: controlled resources require the Windows backend")

    def root(self, workspace):
        self.available()
        return self.base / uid(workspace)

    def ensure(self, workspace):
        self.available()
        self.service.files.root.mkdir(parents=True, exist_ok=True)
        pin = Directory(self.service.files.root)
        try:
            try:
                pin.mkdir("controlled")
                system.private(self.base, directory=True)
            except FileExistsError:
                pass
            base = pin.child("controlled")
            try:
                ident = uid(workspace)
                try:
                    base.mkdir(ident)
                    system.private(self.base / ident, directory=True)
                except FileExistsError:
                    pass
                root = base.child(ident)
                try:
                    for name in ("source", "work", "checks", "runs"):
                        try:
                            root.mkdir(name)
                        except FileExistsError:
                            pass
                        child = root.child(name)
                        child.close()
                finally:
                    root.close()
            finally:
                base.close()
        finally:
            pin.close()
        return self.root(workspace)

    def execution_area(self, workspace, execution):
        root = self.ensure(workspace)
        runs = Directory(root / "runs")
        try:
            ident = uid(execution)
            try:
                runs.mkdir(ident)
            except FileExistsError:
                pass
            run = runs.child(ident)
            try:
                for part in ("temporary", "artifacts", "inputs"):
                    try:
                        run.mkdir(part)
                    except FileExistsError:
                        pass
                    child = run.child(part)
                    child.close()
            finally:
                run.close()
        finally:
            runs.close()
        return root / "runs" / ident

    @contextmanager
    def parent(self, root, relative, *, create=False):
        if not isinstance(relative, str) or len(relative.encode()) > 1024:
            raise ValueError("Invalid workspace relative path")
        parts = relative.split("/")
        if not 1 <= len(parts) <= 16:
            raise ValueError("Workspace path has too many components")
        for part in parts:
            component(part)
        with ExitStack() as stack:
            directory = Directory(root)
            stack.callback(directory.close)
            for part in parts[:-1]:
                if create:
                    try:
                        directory.mkdir(part)
                    except FileExistsError:
                        pass
                directory = directory.child(part)
                stack.callback(directory.close)
            yield directory, parts[-1]

    def area(self, workspace, area, execution=None):
        root = self.root(workspace)
        if area in {"source", "work"}:
            return root / area
        if area in {"artifacts", "inputs"} and execution:
            return root / "runs" / uid(execution) / area
        raise ValueError("Unsupported workspace area")

    @staticmethod
    def regular(stream):
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise ValueError("Managed files must be regular, not hard-linked, and at most 4 MiB")
        return info

    def read(self, root, path, *, authorized=lambda: True, offset=0, length=MAX_READ):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_READ:
            raise ValueError("Invalid bounded file read")
        if not authorized():
            raise PermissionError("Resource authorization expired or revoked")
        with self.parent(root, path) as (directory, name):
            with os.fdopen(directory.open_file(name, os.O_RDONLY, frozen=True), "rb") as stream:
                info = self.regular(stream)
                data = stream.read(MAX_FILE + 1)
                if len(data) != info.st_size or offset > len(data):
                    raise ValueError("Managed file size or offset changed")
                # Byte cursors always end on a UTF-8 boundary. Reject invalid
                # starting offsets instead of silently replacing source bytes.
                data.decode("utf-8")
                data[:offset].decode("utf-8")
                end = min(offset + length, len(data))
                while end < len(data) and data[end] & 0xC0 == 0x80:
                    end += 1
                chunk = data[offset:end]
                return dict(
                    text=chunk.decode("utf-8"),
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    offset=offset,
                    next_offset=offset + len(chunk),
                    eof=offset + len(chunk) == len(data),
                )

    def write(self, root, path, text, expected_sha256, *, authorized=lambda: True):
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE:
            raise ValueError("File content must be UTF-8 text of at most 4 MiB")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
        ):
            raise ValueError("Expected SHA-256 or null for a new file")
        if not authorized():
            raise PermissionError("Resource authorization expired or revoked")
        with self.parent(root, path, create=True) as (directory, name):
            flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if expected_sha256 is None else 0)
            with os.fdopen(directory.open_file(name, flags, exclusive=True), "r+b") as stream:
                self.regular(stream)
                previous = stream.read(MAX_FILE + 1)
                if expected_sha256 is not None and hashlib.sha256(previous).hexdigest() != expected_sha256:
                    raise ValueError("Managed file changed; expected SHA-256 does not match")
                if not authorized():
                    raise PermissionError("Resource authorization expired or revoked")
                data = text.encode()
                stream.seek(0)
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def purge(self, workspace):
        """Explicit collection of an already released, fenced managed workspace."""
        root = self.root(workspace)
        if not root.exists():
            return

        def empty(directory):
            with directory.names() as entries:
                names = [entry.name for entry in entries]
            for name in names:
                info = directory.stat(name)
                if stat.S_ISDIR(info.st_mode):
                    child = directory.child(name)
                    try:
                        empty(child)
                    finally:
                        child.close()
                    directory.rmdir(name)
                else:
                    if info.st_nlink != 1:
                        raise ValueError("Collection refuses hard-linked managed files")
                    directory.unlink(name)

        parent = Directory(root.parent)
        try:
            child = parent.child(root.name)
            try:
                empty(child)
            finally:
                child.close()
            parent.rmdir(root.name)
        finally:
            parent.close()

    def prepare(self, principal, args):
        if set(args) != {"workspace_id", "area", "path", "text", "expected_sha256"}:
            raise ValueError(
                "Workspace preparation requires workspace_id, area, path, text and expected_sha256"
            )
        workspace = self.service.contexts.owned(principal, "workspace", args["workspace_id"])
        if (
            workspace["data"]["released"]
            or self.service.contexts.view(workspace)["expired"]
            or workspace["data"]["active_execution"]
        ):
            raise RuntimeError("Workspace is unavailable, active or unconfirmed")
        if args["area"] == "source":
            for (raw,) in self.service.records.db.execute(
                "SELECT data FROM execution_resources WHERE kind='context'"
            ):
                import json

                if json.loads(raw)["binding"]["workspace_id"] == workspace["id"]:
                    raise ValueError(
                        "Source baseline is frozen after creating a context; create a new workspace"
                    )
        self.ensure(workspace["id"])
        return self.write(
            self.area(workspace["id"], args["area"]), args["path"], args["text"], args["expected_sha256"]
        )
