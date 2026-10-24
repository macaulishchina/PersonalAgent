"""Cooperative configuration IO and immutable resource reads; standard library only."""

from contextlib import contextmanager
from contextvars import ContextVar
from assistant_runtime.api.system import system
import json
import os
from pathlib import Path

_held = ContextVar("configuration_locks", default=frozenset())
_manifests = {}


@contextmanager
def configuration_lock(root):
    root = Path(root).resolve()
    if str(root) in _held.get():
        yield
        return
    state = root / "state/configuration"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "lock").open("a+b") as stream:
        system.private(stream.name)
        system.lock(stream)
        token = _held.set(_held.get() | {str(root)})
        try:
            yield
        finally:
            _held.reset(token)


def configuration_path(path):
    manifest = os.environ.get("PERSONAL_AGENT_RESOURCE_MANIFEST")
    if not manifest:
        return Path(path)
    if manifest not in _manifests:
        _manifests[manifest] = json.loads(Path(manifest).read_text(encoding="utf-8"))
    return Path(_manifests[manifest].get(str(Path(path).absolute()), str(path)))
