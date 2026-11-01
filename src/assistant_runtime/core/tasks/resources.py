"""Foreground-only correction of a stopped execution's resource bindings."""

import time

from .upgrades import pending as pending_upgrade


def repair_resources(plugin, args):
    """Change only this run, with a durable receipt and no implicit continuation."""
    if set(args) != {"run_id", "revision", "resources", "reason"}:
        raise ValueError("Resource repair requires run_id, revision, resources and reason")
    run = plugin.repo.run(args["run_id"])
    if type(args.get("revision")) is not int or args["revision"] != run["revision"]:
        raise ValueError("Execution changed; read its latest revision")
    definition = plugin.repo.definition(run["task_id"])
    if definition["state"] in {"archived", "terminated"}:
        raise ValueError(
            "Task has ended; create a new task with the corrected resources and existing evidence"
        )
    if (
        run["state"] not in {"queued", "paused", "waiting_user"}
        or run["data"].get("turn_active")
        or run["id"] in plugin.launching
        or run["id"] in plugin.upgrading
        or pending_upgrade(run)
    ):
        raise ValueError("Pause the execution and confirm it stopped before correcting resources")
    resources, reason = args.get("resources"), args.get("reason")
    if (
        not isinstance(resources, list)
        or len(resources) > 16
        or any(not isinstance(x, str) or not 0 < len(x.strip()) <= 200 or x != x.strip() for x in resources)
        or len(set(resources)) != len(resources)
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 1000
    ):
        raise ValueError("Expected bounded unique resources and the original user authorization reason")
    spec = run["data"]["spec"]
    # A child may use only authority already granted to its captured parent scope.
    # Foreground repair must not become an alternate child privilege-escalation path.
    if spec.get("parent_id"):
        raise ValueError("Repair the parent delegation and create a correctly scoped child")
    previous = spec.get("resources", [])
    if set(previous) == set(resources):
        return run
    added = set(resources) - set(previous)
    for other in plugin.repo.active_runs():
        if (
            other["id"] != run["id"]
            and (
                other["state"] in {"running", "dispatching", "waiting_user", "unknown"}
                or other["id"] in plugin.launching
            )
            and "resource_locks" not in other["data"]["spec"]
            and added.intersection(other["data"]["spec"].get("resources", []))
        ):
            raise ValueError("Requested resource is busy; wait for its owner before repairing this run")
    history = run["data"].get("resource_repairs", [])
    if len(history) >= 32:
        raise ValueError("Execution resource repair history is full; create a new scoped task")
    plugin.repo.update_run(
        run["id"],
        spec={**spec, "resources": list(resources)},
        resource_repairs=[
            *history,
            {
                "at": time.time(),
                "previous_revision": run["revision"],
                "before": previous,
                "after": list(resources),
                "reason": reason.strip(),
                "authority": "foreground",
            },
        ],
    )
    return plugin.repo.run(run["id"])
