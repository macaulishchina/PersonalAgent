"""Carry durable operation ownership to the generic inbox; no grouping heuristics."""

import asyncio
import json

from ...kernel.contracts import NotificationLink, NotificationRequest


def context(plugin, key):
    parts = key.split(":")
    if parts[0] in {"media-complete", "media-attention"} and len(parts) == 2:
        row = plugin.repo.db.execute("SELECT data FROM media_operations WHERE id=?", (parts[1],)).fetchone()
        group = "media:operation:" + parts[1]
    elif parts[0] == "media-batch" and len(parts) == 3:
        row = plugin.repo.db.execute("SELECT data FROM media_batches WHERE id=?", (parts[1],)).fetchone()
        group = "media:batch:" + parts[1]
    elif parts[0] == "media-auth" and len(parts) == 3:
        return None, "media:account:" + parts[1]
    else:
        return None, None
    caller = json.loads(row[0]).get("caller_run") if row else None
    return caller, None if caller else group


async def publish(plugin, payload):
    caller, group = context(plugin, payload["event_key"])
    # A queued old receipt may already have committed with the old foreground attribution.
    # Enrich that same notice instead of publishing a second notification after restart.
    linked = await plugin.host.runtime.invoke(
        NotificationLink("assistant", payload["event_key"], caller, group)
    )
    if linked.get("linked"):
        return linked
    if group:
        payload = {**payload, "group_key": group}
    return await plugin.host.runtime.invoke(NotificationRequest("publish", payload, caller))


async def restore_associations(plugin):
    if plugin.context.get("notification_groups_v1", False):
        return
    # Adapter owns its historical event keys/operation IDs. The notification owner alone
    # mutates associations, preserving original content, read state and delivery receipts.
    for table, patterns in (
        ("media_operations", ("media-complete:{}", "media-attention:{}")),
        (
            "media_batches",
            tuple(
                "media-batch:{}:" + state
                for state in ("completed", "partial", "failed", "cancelled", "uncertain")
            ),
        ),
    ):
        last = ""
        while True:
            rows = plugin.repo.db.execute(
                f"SELECT id FROM {table} WHERE id>? ORDER BY id LIMIT 50", (last,)
            ).fetchall()
            if not rows:
                break
            for (ident,) in rows:
                for pattern in patterns:
                    key = pattern.format(ident)
                    caller, group = context(plugin, key)
                    await plugin.host.runtime.invoke(NotificationLink("assistant", key, caller, group))
            last = rows[-1][0]
            await asyncio.sleep(0)
    plugin.context.set("notification_groups_v1", True)
