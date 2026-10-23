"""Small, explicit instance configuration. Secrets never belong in tool arguments."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import re


def endpoint(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("Invalid media service URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or any((parsed.username, parsed.password, parsed.query, parsed.fragment))
    ):
        raise ValueError("Media service URL must be HTTP(S), without credentials/query/fragment")
    return value.rstrip("/")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid media identity")
    return value


@dataclass
class MediaSettings:
    accounts: dict = field(default_factory=lambda: configuration_default(__file__, "accounts"))
    file_backend: str = field(default_factory=lambda: configuration_default(__file__, "file_backend"))
    search_url: str = field(default_factory=lambda: configuration_default(__file__, "search_url"))
    openlist_url: str = field(default_factory=lambda: configuration_default(__file__, "openlist_url"))
    timeout_seconds: int = field(default_factory=lambda: configuration_default(__file__, "timeout_seconds"))
    max_operations: int = field(default_factory=lambda: configuration_default(__file__, "max_operations"))
    queue_capacity: int = field(default_factory=lambda: configuration_default(__file__, "queue_capacity"))
    targets: dict = field(default_factory=lambda: configuration_default(__file__, "targets"))
    local_targets: dict = field(default_factory=lambda: configuration_default(__file__, "local_targets"))
    download_min_free_bytes: int = field(
        default_factory=lambda: configuration_default(__file__, "download_min_free_bytes")
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown media settings")
        result = cls(**data)
        if not isinstance(result.file_backend, str) or result.file_backend not in {"native", "openlist"}:
            raise ValueError("Unknown media file backend")
        result.search_url = endpoint(result.search_url)
        if not isinstance(result.openlist_url, str):
            raise ValueError("OpenList URL must be a string")
        if result.openlist_url:
            result.openlist_url = endpoint(result.openlist_url)
        for name, lo, hi in (
            ("timeout_seconds", 3, 30),
            ("max_operations", 100, 100000),
            ("queue_capacity", 1, 512),
            ("download_min_free_bytes", 0, 1024**4),
        ):
            if type(getattr(result, name)) is not int or not lo <= getattr(result, name) <= hi:
                raise ValueError("Invalid media " + name)
        if not isinstance(result.accounts, dict) or not 1 <= len(result.accounts) <= 8:
            raise ValueError("Media accounts require 1..8 explicit provider bindings")
        for name, account in result.accounts.items():
            identifier(name)
            if (
                not isinstance(account, dict)
                or set(account) - {"provider", "file_backend"}
                or "provider" not in account
                or not isinstance(account["provider"], str)
                or account["provider"] not in {"aliyun", "quark", "openlist"}
            ):
                raise ValueError("Media account requires a supported provider")
            backend = account.get("file_backend", result.file_backend)
            if (
                not isinstance(backend, str)
                or backend not in {"native", "openlist"}
                or (account["provider"] == "openlist" and backend != "openlist")
            ):
                raise ValueError("Invalid media account file backend")
        if not isinstance(result.targets, dict) or len(result.targets) > 32:
            raise ValueError("Media targets must be a table with at most 32 entries")
        for name, target in result.targets.items():
            identifier(name)
            if (
                not isinstance(target, dict)
                or not isinstance(target.get("account"), str)
                or target["account"] not in result.accounts
            ):
                raise ValueError("Media targets require a configured account")
            fields = {"account", "openlist_path"}
            if result.accounts[target["account"]]["provider"] != "openlist":
                fields.add("folder_id")
            if result.accounts[target["account"]]["provider"] == "aliyun":
                fields.add("drive_id")
            if set(target) != fields:
                raise ValueError("Invalid media target fields for provider")
            if "drive_id" in target:
                identifier(target["drive_id"])
            if "folder_id" in target:
                identifier(target["folder_id"])
            path = target["openlist_path"]
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or len(path) > 1000
                or "\\" in path
                or "%" in path
                or any(ord(c) < 32 for c in path)
                or any(part in {".", ".."} for part in path.split("/"))
            ):
                raise ValueError("Invalid media target OpenList path")
        if not isinstance(result.local_targets, dict) or len(result.local_targets) > 32:
            raise ValueError("Invalid local media targets")
        for name, target in result.local_targets.items():
            if (
                name not in result.targets
                or not isinstance(target, dict)
                or set(target) != {"path", "staging_path", "mount_path"}
            ):
                raise ValueError("Local targets require a cloud target, path, staging_path and mount_path")
            for value in target.values():
                if (
                    not isinstance(value, str)
                    or len(value) > 1000
                    or not Path(value).is_absolute()
                    or ".." in Path(value).parts
                ):
                    raise ValueError("Local media paths must be absolute without parent traversal")
            final, stage, mount = (Path(target[k]) for k in ("path", "staging_path", "mount_path"))
            if mount == Path("/") or not final.is_relative_to(mount) or not stage.is_relative_to(mount):
                raise ValueError("Local media and staging must be under a dedicated mounted filesystem")
            if final == stage or final.is_relative_to(stage) or stage.is_relative_to(final):
                raise ValueError("Download staging must be outside the published library")
        return result

    def files_for(self, account):
        return self.accounts[account].get("file_backend", self.file_backend)
