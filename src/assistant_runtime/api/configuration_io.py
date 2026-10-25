"""Cooperative configuration IO and immutable resource reads; standard library only."""

from contextlib import contextmanager
from contextvars import ContextVar
from assistant_runtime.api.system import system
import json
import os
from pathlib import Path

_held = ContextVar("configuration_locks", default=frozenset())
_manifests = {}
_resource_view = ContextVar("configuration_resource_view", default=None)


@contextmanager
def resource_view(manifest):
    view = {"manifest": str(manifest) if manifest is not None else None, "active": True}
    token = _resource_view.set(view)
    try:
        yield
    finally:
        # Tasks started while preparing a module must not keep a stale candidate view forever.
        view["active"] = False
        _resource_view.reset(token)


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
    view = _resource_view.get()
    manifest = (
        view["manifest"] if view and view["active"] else os.environ.get("PERSONAL_AGENT_RESOURCE_MANIFEST")
    )
    if not manifest:
        return Path(path)
    if manifest not in _manifests:
        _manifests[manifest] = json.loads(Path(manifest).read_text(encoding="utf-8"))
    return Path(_manifests[manifest].get(str(Path(path).absolute()), str(path)))
