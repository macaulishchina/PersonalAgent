"""Preview consolidation of equivalent OpenCode connection presets."""

from collections import defaultdict
from copy import deepcopy
import json

from .settings import ModelSettings, OPENCODE_SINGLE_PRESETS


def consolidate_opencode(registry):
    before = ModelSettings.parse(registry)
    converted = deepcopy(registry)
    providers = converted.get("providers", {})
    entries = converted.get("entries", {})
    groups = defaultdict(list)
    for ident, raw in providers.items():
        preset = raw.get("preset")
        if preset not in OPENCODE_SINGLE_PRESETS:
            continue
        family, protocol = OPENCODE_SINGLE_PRESETS[preset]
        effective = before.providers[ident]
        groups[
            (
                family,
                effective["key_env"],
                effective.get("enabled", False),
                json.dumps(effective.get("presentation", {}), sort_keys=True),
            )
        ].append((ident, protocol))

    migrated = []
    for (family, _, enabled, _), sources in groups.items():
        protocols = {protocol: ident for ident, protocol in sources}
        if len(protocols) < 2 or len(protocols) != len(sources) or family in providers:
            continue
        first = providers[sources[0][0]]
        # Preserve the private key reference, route identity and any explicit presentation.
        providers[family] = {
            "preset": family,
            "key_env": before.providers[sources[0][0]]["key_env"],
            "enabled": enabled,
            "route_ids": protocols,
            **({"presentation": first["presentation"]} if "presentation" in first else {}),
        }
        by_name = dict(sources)
        aliases = []
        for alias, entry in entries.items():
            if entry["provider"] in by_name:
                entry["protocol"] = by_name[entry["provider"]]
                entry["provider"] = family
                aliases.append(alias)
        for ident in by_name:
            del providers[ident]
        migrated.append({"connection": family, "replaces": list(by_name), "models": aliases})

    after = ModelSettings.parse(converted)
    for alias, entry in before.entries.items():
        target = after.entries[alias]
        if (
            before.provider_for(entry) != after.provider_for(target)
            or before.route(alias)["revision"] != after.route(alias)["revision"]
        ):
            raise ValueError("Consolidation would change the effective model route")
    retained = [ident for ident, raw in providers.items() if raw.get("preset") in OPENCODE_SINGLE_PRESETS]
    return converted, migrated, retained
