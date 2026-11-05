"""Instance-wide counts only; never expose execution identities or content."""


def capacity(service):
    counts = dict(service.records.db.execute("SELECT state,COUNT(*) FROM agent_runs GROUP BY state"))
    leases = tuple(service.admission.leases)
    quarantined = (
        service.records.db.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE state='unknown' AND id IN ("
            + ",".join("?" for _ in leases)
            + ")",
            leases,
        ).fetchone()[0]
        if leases
        else 0
    )
    return {
        "limit": service.admission.slots.limit,
        "occupied": service.admission.slots.active,
        "queued": counts.get("queued", 0) + service.admission.slots.inference_waiting,
        "waiting_user": counts.get("waiting_user", 0),
        "unknown": counts.get("unknown", 0),
        "quarantined": quarantined,
    }
