"""Same-volume local library changes through no-follow directory descriptors."""

import hashlib
import os
from pathlib import Path
import stat

from .errors import MediaError
from .library import filename, relative


class LocalTree:
    def __init__(self, binding):
        self.binding = binding
        self.root = self.trash = None

    def open(self):
        mount = Path(self.binding["mount_path"])
        if not mount.is_mount():
            raise MediaError("download_mount_unavailable")
        base = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.device = os.fstat(base).st_dev
            self.root = self.walk(base, Path(self.binding["path"]).relative_to(mount).parts)
            stage = self.walk(base, Path(self.binding["staging_path"]).relative_to(mount).parts)
            try:
                self.trash = self.walk(stage, ["library-trash"], create=True)
            finally:
                os.close(stage)
        finally:
            os.close(base)
        return self

    def walk(self, base, parts, create=False):
        fd = os.dup(base)
        try:
            for part in parts:
                filename(part)
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
                if os.fstat(fd).st_dev != self.device:
                    raise MediaError("download_filesystem_changed")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def parent(self, path, trash=False):
        parts = relative(path)
        if not parts:
            raise ValueError("A file path is required")
        return self.walk(self.trash if trash else self.root, parts[:-1]), parts[-1]

    def info(self, path, trash=False):
        parent, name = self.parent(path, trash)
        try:
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) and not stat.S_ISDIR(value.st_mode):
                raise MediaError("unsafe_media_file")
            if value.st_dev != self.device:
                raise MediaError("download_filesystem_changed")
            return {
                "file_id": hashlib.sha256(f"{value.st_dev}:{value.st_ino}".encode()).hexdigest()[:32],
                "name": name,
                "type": "folder" if stat.S_ISDIR(value.st_mode) else "file",
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
                "relative_path": path,
            }
        finally:
            os.close(parent)

    def absent(self, path, trash=False):
        parent, name = self.parent(path, trash)
        try:
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise MediaError("destination_name_conflict")
        finally:
            os.close(parent)

    def listing(self, path="", marker=""):
        fd = self.walk(self.root, relative(path))
        try:
            names = []
            with os.scandir(fd) as stream:
                for entry in stream:
                    if len(names) >= 10000:
                        raise MediaError("local_directory_listing_limit")
                    names.append(entry.name)
            names = sorted(n for n in names if n > marker)
            items = [self.info("/".join(filter(None, [path, n]))) for n in names[:100]]
            return {
                "items": items,
                "marker": names[99] if len(names) > 100 else None,
                "complete": len(names) <= 100,
                "coverage": {"relative_path": path, "recursive": False},
            }
        finally:
            os.close(fd)

    def change(self, step):
        action = step["action"]
        source = step.get("path")
        destination = step.get("destination")
        if action == "mkdir":
            parent, name = self.parent(destination)
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
            return self.info(destination)
        src_trash = action == "restore"
        dst_trash = action == "trash"
        expected = step["before"]
        try:
            actual = self.info(source, src_trash)
        except FileNotFoundError:
            actual = None
        try:
            present = self.info(destination, dst_trash)
        except FileNotFoundError:
            present = None

        def matches(item):
            return item and all(item[k] == expected[k] for k in ("file_id", "size", "mtime_ns", "type"))

        if actual and not matches(actual):
            raise MediaError("library_precondition_changed")
        if present and not matches(present):
            raise MediaError("destination_name_conflict")
        if not actual and not matches(present):
            raise MediaError("library_write_unconfirmed", uncertain=True)
        if actual:
            src, sn = self.parent(source, src_trash)
            dst, dn = self.parent(destination, dst_trash)
            try:
                if not present:
                    os.link(sn, dn, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
                    os.fsync(dst)
                if not matches(self.info(source, src_trash)) or not matches(
                    self.info(destination, dst_trash)
                ):
                    raise MediaError("library_precondition_changed")
                # A crash between link/unlink leaves both names with the same
                # identity. Reconciliation only removes the captured source name.
                os.unlink(sn, dir_fd=src)
                os.fsync(src)
            finally:
                os.close(src)
                os.close(dst)
        return self.info(destination, dst_trash)

    def close(self):
        for field in ("root", "trash"):
            fd = getattr(self, field)
            if fd is not None:
                os.close(fd)
                setattr(self, field, None)


def local_call(binding, method, *args):
    tree = LocalTree(binding)
    try:
        tree.open()
        return getattr(tree, method)(*args)
    finally:
        tree.close()


class LocalLibrary:
    def __init__(self, library):
        self.library = library
        self.p = library.p
        self.db = library.db

    def binding(self, target):
        self.p.target(target)
        if target not in self.p.settings.local_targets:
            raise ValueError("No local library configured")
        return self.p.settings.local_targets[target]

    def busy(self, target):
        if self.db.execute(
            """SELECT 1 FROM media_operations WHERE state NOT IN ('completed','failed','cancelled')
                AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.intent.kind')='download'
                LIMIT 1""",
            (target,),
        ).fetchone():
            raise MediaError("local_library_busy")

    async def listing(self, target, path="", marker=""):
        from .private import disk

        result = await disk(local_call, self.binding(target), "listing", path, marker)
        for item in result["items"]:
            self.library.remember(target, item, location="local", trashed=False)
        return result

    async def preview(self, args, owner):
        import json
        from .private import disk

        target = args["target"]
        binding = self.binding(target)
        self.busy(target)
        changes = args.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
            raise ValueError("A plan requires 1..100 changes")
        steps = []
        seen = set()
        destinations = set()
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("Expected a change object")
            action = change.get("action")
            fields = {
                "mkdir": {"action", "path"},
                "rename": {"action", "path", "name"},
                "move": {"action", "path", "folder"},
                "trash": {"action", "path"},
                "restore": {"action", "file_id"},
            }
            if not isinstance(action, str) or action not in fields or set(change) != fields[action]:
                raise ValueError("Invalid local change fields")
            step = dict(change)
            if action == "mkdir":
                relative(step["path"])
                before = None
                destination = step["path"]
            elif action == "restore":
                row = self.db.execute(
                    "SELECT data FROM media_assets WHERE id=?",
                    (self.library.asset_id(target, "local_" + step["file_id"]),),
                ).fetchone()
                saved = json.loads(row[0]) if row else {}
                if not saved.get("trashed") or not saved.get("trash_operation"):
                    raise ValueError("Restore requires a recorded local trash operation")
                before = saved["file"]
                step["path"] = before["relative_path"]
                destination = saved["original_path"]
            else:
                before = await disk(local_call, binding, "info", step["path"])
                if before["type"] != "file":
                    raise ValueError("Recursive folder changes are not supported")
                if action == "rename":
                    name = filename(step["name"])
                    if Path(name).suffix.lower() != Path(step["path"]).suffix.lower():
                        raise ValueError("Renaming must preserve extension")
                    destination = str(Path(step["path"]).with_name(name))
                elif action == "move":
                    destination = "/".join([*relative(step["folder"]), before["name"]])
                else:
                    destination = before["file_id"] + Path(before["name"]).suffix
            if step["path"] in seen or destination in destinations:
                raise ValueError("Conflicting local plan entries")
            seen.add(step["path"])
            destinations.add(destination)
            await disk(local_call, binding, "absent", destination, action == "trash")
            steps.append({**step, "before": before, "destination": destination, "state": "pending"})
        return self.library.save_plan(
            owner,
            {
                "target": target,
                "root": self.p.target(target),
                "location": "local",
                "local_binding": binding,
                "steps": steps,
            },
        )

    async def process(self, item):
        from .private import disk

        ident = item["id"]
        intent = item["data"]["intent"]
        target = intent["target"]
        binding = self.binding(target)
        if binding != intent["local_binding"]:
            raise MediaError("target_configuration_changed")
        self.busy(target)
        steps = item["data"]["steps"]
        for step in steps:
            if step["state"] == "completed":
                continue
            if self.p.repo.get(ident)["data"].get("cancel_requested") and step["state"] != "submitted":
                self.p.repo.change(ident, "cancelled", steps=steps, submitted=False)
                return
            if step["state"] == "submitted" and step["action"] == "mkdir":
                # Existing name cannot prove our unacknowledged mkdir created it.
                raise MediaError("mkdir_identity_unconfirmed", uncertain=True)
            step["state"] = "submitted"
            self.p.repo.change(ident, "submitting", steps=steps, submitted=True)
            result = await disk(local_call, binding, "change", step)
            extra = {"location": "local", "trashed": step["action"] == "trash"}
            if step["action"] == "trash":
                extra.update(trash_operation=ident, original_path=step["path"])
            self.library.remember(target, result, **extra)
            # Local availability is a separate observation from cloud availability.
            if step["action"] != "mkdir":
                self.library.update_local_location(
                    target,
                    step["path"],
                    result["relative_path"],
                    step["action"] == "trash",
                    step["action"] == "restore",
                )
            step.update(state="completed", result=result)
            self.p.repo.change(ident, "checking", steps=steps, submitted=False)
        self.p.repo.change(ident, "completed", steps=steps, submitted=False, error=None)
