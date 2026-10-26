"""Static model resource rules; no client, provider login or inference is constructed."""


def credential_names(data):
    from .settings import PRESETS

    return {
        p.get("key_env", PRESETS[p["preset"]]["key_env"])
        for p in data.get("model_api", {}).get("providers", {}).values()
        if p.get("preset") in PRESETS
    }
