"""Versioned TOML documents. Private rollback journal precedes every replacement."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from assistant_runtime.api.system import system
import hashlib
import json
import os
from pathlib import Path
import re
import time
import tomllib
import uuid

MASK = "••••••••"
SECRET = re.compile(r"(^|_)(password|secret|cookie|token|api_key|access_key|refresh_token)$", re.I)


def sensitive(key):
    return bool(SECRET.search(key))


def redact(value):
    if isinstance(value, dict):
        if set(value) == {"configured"} and type(value["configured"]) is bool:
            return value
        return {
            k: MASK
            if sensitive(k)
            and v
            and not (isinstance(v, dict) and set(v) == {"configured"} and type(v["configured"]) is bool)
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Conflict(ValueError):
    """The caller edited an obsolete file generation."""


def atomic(path, data):
    system.atomic_write(path, data, prefix=".config-")


@dataclass
class Snapshot:
    version: str
    contents: dict[str, bytes]
    resources: dict[str, bytes | None] = field(default_factory=dict)


def snapshot_version(contents, resources):
    digest = hashlib.sha256()
    for name, content in sorted(contents.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    for ident, content in sorted(resources.items()):
        digest.update(
            ident.encode() + b"\0resource\0" + (content if content is not None else b"\0absent") + b"\0"
        )
    return digest.hexdigest()


class ConfigurationFiles:
    def __init__(self, entry, state, validate, *, retain=20, resources=None):
        self.entry = Path(entry).expanduser().absolute()
        self.directory = self.entry.parent
        self.state = Path(state)
        self.validate = validate
        self.retain = retain
        self.resources = resources
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        system.private(self.state, directory=True)

    @contextmanager
    def locked(self):
        if self.resources:
            from assistant_runtime.api.configuration_io import configuration_lock

            with configuration_lock(self.resources.root):
                self.recover()
                yield
            return
        with (self.state / "lock").open("a+b") as stream:
            system.private(stream.name)
            system.lock(stream)
            self.recover()
            yield

    def path(self, name):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("Configuration path must stay in its directory")
        target = self.directory / relative
        if any(p.is_symlink() for p in [self.directory, *target.parents, target]):
            raise ValueError("Configuration symlinks are not editable")
        if not target.resolve().is_relative_to(self.directory.resolve()):
            raise ValueError("Configuration path escapes directory")
        return target

    def snapshot(self):
        entry_name = self.entry.name
        raw = self.path(entry_name).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("Configuration file exceeds 1 MiB")
        data = tomllib.loads(raw.decode())
        includes = data.get("include", [])
        if not isinstance(includes, list) or len(includes) > 32:
            raise ValueError("Invalid configuration includes")
        contents = {entry_name: raw}
        for name in includes:
            if not isinstance(name, str) or name in contents:
                raise ValueError("Invalid or duplicate included configuration")
            content = self.path(name).read_bytes()
            if len(content) > 1024 * 1024:
                raise ValueError("Configuration file exceeds 1 MiB")
            if "include" in tomllib.loads(content.decode()):
                raise ValueError("Recursive configuration includes are unsupported")
            contents[name] = content
        digest = hashlib.sha256()
        for name, content in sorted(contents.items()):
            digest.update(name.encode() + b"\0" + content + b"\0")
        resources = self.resources.snapshot(contents) if self.resources else {}
        for ident, content in sorted(resources.items()):
            digest.update(
                ident.encode() + b"\0resource\0" + (content if content is not None else b"\0absent") + b"\0"
            )
        index = self.state / "registered-files.json"
        names = list(contents)
        if not index.exists() or json.loads(index.read_text(encoding="utf-8")) != names:
            atomic(index, json.dumps(names).encode())
        return Snapshot(digest.hexdigest(), contents, resources)

    def read(self):
        with self.locked():
            snapshot = self.snapshot()
            return {
                "version": snapshot.version,
                "files": {
                    name: redact(tomllib.loads(raw.decode())) for name, raw in snapshot.contents.items()
                },
            }

    def fields(self):
        from .fields import present

        with self.locked():
            return present(
                self.snapshot(), self.entry.name, root=self.resources.root if self.resources else None
            )

    def patch(self, version, patches, *, actor="administrator", resource_changes=None, _preview=False):
        from .fields import patch_documents

        return self.save(
            version,
            lambda snapshot: patch_documents(snapshot, self.entry.name, patches)
            if patches
            else {n: tomllib.loads(raw.decode()) for n, raw in snapshot.contents.items()},
            actor=actor,
            resource_changes=resource_changes,
            _preview=_preview,
        )

    def _candidate(self, snapshot, changes):
        import tomlkit

        if not isinstance(changes, dict) or not changes:
            raise ValueError("Expected changed configuration documents")
        if set(changes) - snapshot.contents.keys():
            raise ValueError("Only registered configuration files can be edited")
        contents = dict(snapshot.contents)
        fields = []
        for name, replacement in changes.items():
            if not isinstance(replacement, dict):
                raise ValueError("Configuration document must be an object")
            old = tomllib.loads(contents[name].decode())
            for key in ("root", "model_dir", "pi_binary"):
                if replacement.get(key) != old.get(key):
                    raise ValueError(key + " requires local installation or migration")
            if name == self.entry.name and replacement.get("include", []) != old.get("include", []):
                raise ValueError("Changing include layout requires local configuration maintenance")
            doc = tomlkit.parse(contents[name].decode())

            def update(table, before, after, prefix=()):
                for key in set(before) - set(after):
                    del table[key]
                    fields.append(".".join((*prefix, key)))
                for key, value in after.items():
                    if key in before and before[key] == value:
                        continue
                    if key in before and isinstance(before[key], dict) and isinstance(value, dict):
                        update(table[key], before[key], value, (*prefix, key))
                    else:
                        table[key] = value
                        fields.append(".".join((*prefix, key)))

            update(doc, old, replacement)
            content = tomlkit.dumps(doc).encode()
            if len(content) > 1024 * 1024:
                raise ValueError("Configuration file exceeds 1 MiB")
            contents[name] = content
        return contents, sorted(set(fields))

    def save(self, version, changes, *, actor="local", resource_changes=None, _repair=False, _preview=False):
        with self.locked():
            snapshot = self._repair_snapshot() if _repair else self.snapshot()
            if version != snapshot.version:
                raise Conflict("Configuration changed; reload before saving")
            changes = changes(snapshot) if callable(changes) else changes
            if _repair:
                if not isinstance(changes, dict) or not changes or set(changes) - snapshot.contents.keys():
                    raise ValueError("Repair is limited to registered files")
                contents = {**snapshot.contents, **changes}
                fields = ["repair:" + name for name in changes]
                for raw in contents.values():
                    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024:
                        raise ValueError("Invalid repair content")
                    tomllib.loads(raw.decode())
                include = tomllib.loads(contents[self.entry.name].decode()).get("include", [])
                if set(include) | {self.entry.name} != set(contents):
                    raise ValueError("Repair cannot change the registered include layout")
            else:
                contents, fields = self._candidate(snapshot, changes)
            resource_values = (
                self.resources.snapshot(contents) if _repair and self.resources else dict(snapshot.resources)
            )
            registered = self.resources.registered(contents) if self.resources else {}
            if resource_changes and not self.resources:
                raise ValueError("No resource adapter is registered")
            if resource_changes is not None and (
                not isinstance(resource_changes, list) or len(resource_changes) > 128
            ):
                raise ValueError("Invalid resource operations")
            touched = set()
            for operation in resource_changes or []:
                if not isinstance(operation, dict):
                    raise ValueError("Invalid resource operation")
                ident = operation.get("id")
                if ident not in registered:
                    raise ValueError("Resource is not registered")
                resource = registered[ident]
                if ident not in resource_values:
                    path = self.resources.path(resource)
                    resource_values[ident] = path.read_bytes() if path.is_file() else None
                resource_values[ident] = (
                    None
                    if actor.startswith("restore:") and operation.get("op") == "remove_resource"
                    else self.resources.prepare(resource, resource_values[ident], operation)
                )
                touched.add(ident)
                fields.extend(self.resources.audit_fields(resource, operation))
            if self.resources and not _repair:
                self.resources.prepare_defaults(contents, resource_values, touched)
            self.validate_all(contents, resource_values, touched)
            if (self._repair_snapshot() if _repair else self.snapshot()).version != version:
                raise Conflict("Configuration changed during validation")
            if _preview:
                return Snapshot(snapshot_version(contents, resource_values), contents, resource_values)
            if contents == snapshot.contents and resource_values == snapshot.resources:
                return {"version": version, "changed": False}
            ident = uuid.uuid4().hex
            backup = self.state / "versions" / ident
            backup.parent.mkdir(exist_ok=True, mode=0o700)
            system.private(backup.parent, directory=True)
            backup.mkdir(mode=0o700)
            for name, content in snapshot.contents.items():
                atomic(backup / name, content)
            resource_paths = {}
            backup_registered = {
                **(self.resources.registered(snapshot.contents) if self.resources and not _repair else {}),
                **registered,
            }
            for resource_id in snapshot.resources.keys() | touched:
                resource = backup_registered[resource_id]
                path = self.resources.path(resource)
                resource_paths[resource_id] = str(path.relative_to(self.resources.root))
                original = snapshot.resources.get(resource_id)
                if resource_id not in snapshot.resources:
                    original = path.read_bytes() if path.is_file() else None
                if original is not None:
                    atomic(backup / "resources" / resource_id, original)
            metadata = {
                "id": ident,
                "version": version,
                "files": list(snapshot.contents),
                "time": time.time(),
                "fields": sorted(set(fields) | {"resource:" + ident for ident in touched}),
                "resource_paths": resource_paths,
                "resource_present": [key for key in resource_paths if (backup / "resources" / key).exists()],
                "actor": actor[:80],
            }
            atomic(backup / "receipt.json", json.dumps(metadata).encode())
            atomic(self.state / "transaction.json", json.dumps({"backup": ident}).encode())
            try:
                for name, content in contents.items():
                    atomic(self.path(name), content)
                for ident in touched:
                    path = self.resources.path(registered[ident])
                    if resource_values[ident] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic(path, resource_values[ident])
                result = self.snapshot().version
                self._audit({**metadata, "result": "saved", "saved_version": result})
                # Commit point: recovery rolls back until this marker is durably removed.
                self._clear_journal()
            except BaseException:
                self.recover()
                raise
            self.prune()
            return {"version": result, "changed": True, "fields": fields, "state": "pending_restart"}

    def validate_all(self, contents, values, touched=()):
        config = self.validate(contents)
        from .owners import validate_changed

        if config is not None:
            validate_changed(contents, {name: self.path(name).read_bytes() for name in contents})
        if self.resources:
            for ident, resource in self.resources.registered(contents).items():
                raw = values.get(ident)
                if raw is not None or ident in touched:
                    self.resources.validate(resource, raw)
            self.resources.validate_candidate(config, contents, values)
        return config

    def _clear_journal(self):
        (self.state / "transaction.json").unlink(missing_ok=True)
        system.sync_directory(self.state)

    def recover(self):
        journal = self.state / "transaction.json"
        if not journal.exists():
            return
        ident = json.loads(journal.read_text(encoding="utf-8"))["backup"]
        backup = self._backup(ident)
        metadata = json.loads((backup / "receipt.json").read_text(encoding="utf-8"))
        for name in metadata["files"]:
            atomic(self.path(name), (backup / name).read_bytes())
        if metadata.get("resource_paths"):
            if self.resources is None:
                raise ValueError("Resource recovery adapter unavailable")
            from .resources import Resource

            for key, relative in metadata["resource_paths"].items():
                path = self.resources.path(Resource(key, self.resources.root / relative, "text", key))
                if key in metadata["resource_present"]:
                    atomic(path, (backup / "resources" / key).read_bytes())
                else:
                    path.unlink(missing_ok=True)
        self._audit({"id": ident, "time": time.time(), "result": "rolled_back"})
        self._clear_journal()

    def _backup(self, ident):
        if not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident):
            raise ValueError("Invalid configuration backup identifier")
        return self.state / "versions" / ident

    def history(self):
        with self.locked():
            return [
                json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(
                    (self.state / "versions").glob("*/receipt.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            ]

    def restore(self, ident, version):
        # Reuse save for conflict detection, validation, backup and audit.
        backup = self._backup(ident)
        receipt = json.loads((backup / "receipt.json").read_text(encoding="utf-8"))
        documents = {
            name: tomllib.loads((backup / name).read_text(encoding="utf-8")) for name in receipt["files"]
        }
        operations = []
        if self.resources:
            registered = self.resources.registered({n: (backup / n).read_bytes() for n in receipt["files"]})
            for key in receipt.get("resource_paths", {}):
                if key not in registered:
                    raise ValueError("Backup resource is no longer registered")
                raw = (
                    (backup / "resources" / key).read_bytes()
                    if key in receipt.get("resource_present", [])
                    else None
                )
                if registered[key].kind == "asset" or (registered[key].kind == "management" and raw is None):
                    continue
                if registered[key].kind == "dotenv":
                    from dotenv import dotenv_values
                    from io import StringIO

                    old = dotenv_values(stream=StringIO((raw or b"").decode()), interpolate=False)
                    current = dotenv_values(registered[key].path, interpolate=False)
                    operations.extend(
                        {"id": key, "op": "replace_secret", "key": name, "value": value}
                        for name, value in old.items()
                    )
                    operations.extend(
                        {"id": key, "op": "clear_secret", "key": name} for name in current.keys() - old.keys()
                    )
                elif raw is not None:
                    operations.append(
                        {
                            "id": key,
                            "op": "replace_secret" if registered[key].sensitive else "set",
                            "value": raw.decode(),
                        }
                    )
                else:
                    operations.append({"id": key, "op": "remove_resource"})
        return self.save(version, documents, actor="restore:" + ident, resource_changes=operations)

    def _audit(self, record):
        path = self.state / "audit.jsonl"
        with path.open("ab") as stream:
            system.private(path)
            stream.write(json.dumps(record, ensure_ascii=False).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def prune(self):
        import shutil

        protected = set()
        for name in ("applied.json", "confirmed.json"):
            applied = self.state / name
            if applied.exists():
                version = json.loads(applied.read_text(encoding="utf-8")).get("version")
                protected.update(
                    p.parent.name
                    for p in (self.state / "versions").glob("*/receipt.json")
                    if json.loads(p.read_text(encoding="utf-8"))["version"] == version
                )
        backups = sorted(
            (self.state / "versions").glob("*/receipt.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for p in backups[self.retain :]:
            if p.parent.name not in protected:
                shutil.rmtree(p.parent)

    def _repair_snapshot(self):
        index = self.state / "registered-files.json"
        if index.exists():
            names = json.loads(index.read_text(encoding="utf-8"))
        else:
            names = [self.entry.name]
            try:
                names += tomllib.loads(self.entry.read_text(encoding="utf-8")).get("include", [])
            except (ValueError, OSError):
                pass
        if not isinstance(names, list) or len(names) > 33 or self.entry.name not in names:
            raise ValueError("Invalid registered repair manifest")
        contents = {}
        digest = hashlib.sha256()
        for name in sorted(set(names)):
            raw = self.path(name).read_bytes() if self.path(name).exists() else b""
            if len(raw) > 1024 * 1024:
                raise ValueError("Repair file exceeds bound")
            contents[name] = raw
            digest.update(name.encode() + b"\0" + raw + b"\0")
        return Snapshot(digest.hexdigest(), contents)

    def repair_inventory(self):
        with self.locked():
            snapshot = self._repair_snapshot()
            # Damaged files may contain secrets, so never return their raw contents.
            return {"version": snapshot.version, "files": list(snapshot.contents), "replacement_only": True}

    def repair(self, version, replacements):
        if not isinstance(replacements, dict) or any(not isinstance(v, str) for v in replacements.values()):
            raise ValueError("Repair requires file text replacements")
        return self.save(
            version, {n: v.encode() for n, v in replacements.items()}, actor="repair", _repair=True
        )
