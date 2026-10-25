"""Offline, route-bound model facts. Metadata is never an acceptance receipt."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
FACTS = {"images", "structured", "tools", "reasoning", "context_window", "output_limit", "reasoning_efforts"}


@lru_cache(maxsize=1)
def public_catalog():
    return json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))


def facts(raw):
    """Allowlist and bound external metadata; omitted fields stay unknown."""
    value = {}
    for key in ("images", "structured", "tools", "reasoning"):
        if type(raw.get(key)) is bool:
            value[key] = raw[key]
    for key in ("context_window", "output_limit"):
        if type(raw.get(key)) is int and 1 <= raw[key] <= 10000000:
            value[key] = raw[key]
    efforts = raw.get("reasoning_efforts")
    if isinstance(efforts, list) and efforts and all(e in LEVELS for e in efforts):
        value["reasoning_efforts"] = list(dict.fromkeys(efforts))
    return value


def binding(provider, model, account=None):
    # Credentials never enter metadata. Changes of endpoint, protocol or native identity
    # invalidate the snapshot; enabling a connection does not change its identity.
    route = {k: v for k, v in provider.items() if k not in {"enabled", "key_env"}}
    native = {k: v for k, v in (account or {}).items() if k in {"provider", "auth"}}
    return hashlib.sha256(json.dumps([route, model, native], sort_keys=True).encode()).hexdigest()


def supplier(provider, account=None):
    if provider.get("protocol") == "pi":
        return (account or {}).get("provider", "")
    from .settings import PRESETS

    names = {"openai": "openai", "claude": "anthropic", "gemini": "google", "deepseek": "deepseek"}
    for preset, name in names.items():
        spec = PRESETS[preset]
        if all(provider.get(k) == spec[k] for k in ("protocol", "base_url")):
            return name
    return ""


def profile(provider, model, *, account=None, native=None, remote=None, reference=""):
    """Build a reviewable snapshot; public reasoning booleans never invent effort levels."""
    catalog = public_catalog()
    owner = supplier(provider, account)
    lookup = reference or (owner + "/" + model if owner else "")
    values = deepcopy(catalog["models"].get(lookup, {}))
    source = "models.dev" if values else "unknown"
    if native:
        values.update(facts(native))
        source = "Pi 1.4.0"
    if remote and provider.get("protocol") != "pi":
        values.update(facts(remote))
        source = "provider"
    # A native Pi adapter's levels describe its own translations. They must not
    # be copied to a generic gateway even when the upstream model has the same ID.
    if provider.get("protocol") != "pi":
        values.pop("reasoning_efforts", None)
        if provider.get("protocol") in {"openai_responses", "openai_chat"} and remote:
            declared = facts(remote).get("reasoning_efforts")
            if declared:
                values["reasoning_efforts"] = declared
    return {
        "binding": binding(provider, model, account),
        "source": source,
        "revision": catalog["revision"] if source == "models.dev" else source,
        "reference": lookup,
        **facts(values),
    }


def resolve(entry, provider, account=None):
    """Resolve once into the immutable settings snapshot, preserving explicit overrides."""
    legacy_efforts = entry.get("reasoning_efforts")
    snapshot = entry.get("capabilities")
    if snapshot is None:
        snapshot = profile(provider, entry["model"], account=account)
    else:
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) - FACTS - {"binding", "source", "revision", "reference"}
            or any(
                not isinstance(snapshot.get(k), str) or len(snapshot[k]) > 512
                for k in ("binding", "source", "revision", "reference")
            )
            or {k: v for k, v in snapshot.items() if k in FACTS} != facts(snapshot)
        ):
            raise ValueError("Invalid model capability snapshot")
        if snapshot["binding"] != binding(provider, entry["model"], account):
            raise ValueError("Model connection changed; refresh capabilities or remove its stale snapshot")
    entry["capabilities"] = snapshot
    for key in ("images", "structured", "context_window", "reasoning_efforts"):
        if key in snapshot:
            entry.setdefault(key, deepcopy(snapshot[key]))
    entry.setdefault("reasoning_efforts", [])
    entry.setdefault("default_reasoning_effort", legacy_efforts[0] if legacy_efforts else "auto")


def output_bound(entry, requested):
    limit = entry.get("capabilities", {}).get("output_limit")
    return min(requested, limit) if limit else requested
