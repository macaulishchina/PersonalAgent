"""Offline, route-bound model facts. Metadata is never an acceptance receipt."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
FACTS = {"images", "structured", "tools", "reasoning", "context_window", "output_limit", "reasoning_efforts"}


@lru_cache(maxsize=1)
def public_catalog():
    return json.loads(Path(__file__).with_name("model_catalog.json").read_text(encoding="utf-8"))


def catalog_identity(metadata):
    """Use only the upstream lab/model attribution, never names or vendor lists."""
    canonical = metadata.get("canonical_model_id")
    if (
        not isinstance(canonical, str)
        or len(canonical) > 512
        or any(c.isspace() or ord(c) < 32 for c in canonical)
    ):
        return "", ""
    owner, separator, model = canonical.partition("/")
    return (owner, canonical) if owner and separator and model else ("", "")


def catalog_origin(provider, metadata):
    owner, _ = catalog_identity(metadata)
    return ("original" if provider == owner else "other") if owner else "unknown"


def search_references(query):
    """Rank names across all suppliers; candidates never select or apply metadata."""
    catalog = public_catalog()
    tokens = re.findall(r"[^\W_]+", query.casefold())
    compact = "".join(tokens)
    ranked = []
    if compact:
        for reference, metadata in catalog["models"].items():
            provider, _, model = reference.partition("/")
            normalized = "".join(re.findall(r"[^\W_]+", model.casefold()))
            _, canonical = catalog_identity(metadata)
            canonical_name = "".join(re.findall(r"[^\W_]+", canonical.partition("/")[2].casefold()))
            searchable = (reference + " " + canonical).casefold()
            if normalized == compact:
                rank = 0
            elif compact in normalized or compact == canonical_name:
                rank = 1
            elif compact in canonical_name or all(token in searchable for token in tokens):
                rank = 2
            else:
                continue
            ranked.append((rank, reference, provider, model, metadata))
    ranked.sort(
        key=lambda row: (
            0 if catalog_origin(row[2], row[4]) == "original" else 1,
            row[0],
            row[1],
        )
    )
    return {
        "candidates": [
            {
                "reference": ref,
                "provider": provider,
                "model": model,
                "match": "exact" if rank == 0 else "related",
                "origin": catalog_origin(provider, metadata),
                "originator": catalog_identity(metadata)[0],
                "canonical_model_id": catalog_identity(metadata)[1],
                "capabilities": facts(metadata),
            }
            for rank, ref, provider, model, metadata in ranked
        ],
        "total": len(ranked),
        "source": "models.dev",
        "revision": catalog["revision"],
    }


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
    # invalidate the snapshot. Enablement, credentials and directory metadata
    # do not change the inference route or invalidate accepted model facts.
    route = {k: v for k, v in provider.items() if k not in {"enabled", "key_env", "catalog"}}
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
    # Import declared levels only where the adapter has a matching control.
    # Google's adapter accepts these levels; it owns level/budget wire encoding.
    # A reasoning boolean or token budget must never invent a level list.
    if provider.get("protocol") == "gemini" and "reasoning_efforts" in values:
        values["reasoning_efforts"] = [
            level
            for level in values["reasoning_efforts"]
            if level in {"none", "minimal", "low", "medium", "high"}
        ]
        if not values["reasoning_efforts"]:
            values.pop("reasoning_efforts")
    elif provider.get("protocol") not in {"pi", "openai_responses", "openai_chat", "anthropic"}:
        values.pop("reasoning_efforts", None)
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
    for key in ("images", "structured", "context_window", "output_limit", "reasoning_efforts"):
        if key in snapshot:
            entry.setdefault(key, deepcopy(snapshot[key]))
    entry.setdefault("reasoning_efforts", [])
    entry.setdefault("default_reasoning_effort", legacy_efforts[0] if legacy_efforts else "auto")


def output_bound(entry, requested):
    limit = entry.get("output_limit", entry.get("capabilities", {}).get("output_limit"))
    return min(requested, limit) if limit else requested
