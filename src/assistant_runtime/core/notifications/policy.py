"""Notification purpose controls interruption, independently of producer/business type."""

KINDS = frozenset({"progress", "result", "attention"})
# Historical untyped notices retain their original reminder behavior.
ALERT_SQL = "coalesce(json_extract(payload,'$.kind'),'attention')!='progress'"


def kind(payload):
    return payload.get("kind", "attention")


def channels(payload, configured):
    if kind(payload) == "progress":
        return []
    return list(configured) + (["voice"] if payload.get("voice") else [])
