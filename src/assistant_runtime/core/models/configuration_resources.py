"""Static model resource rules; no client, provider login or inference is constructed."""


def registered(root, data):
    from assistant_runtime.core.configuration.resources import Resource

    return [
        Resource("gateway_key", root / "state/model-api/gateway.key", "credential", "模型网关私有口令", True)
    ]


def credential_names(data):
    from .settings import PRESETS

    return {
        p.get("key_env", PRESETS[p["preset"]]["key_env"])
        for p in data.get("model_api", {}).get("providers", {}).values()
        if p.get("preset") in PRESETS
    }
