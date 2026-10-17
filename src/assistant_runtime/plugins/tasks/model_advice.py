"""One optional joint judgment; explicit fields and original fallback remain owned here."""

import hashlib

from ...kernel.contracts import DecisionQuery
from ...kernel.decisions import decide, fallback


async def select_execution(plugin, requested, objective):
    policy, fixed = plugin.model_policy, plugin.settings.execution
    policy.validate(requested, fixed)
    choose_profile = fixed["model"] == "auto" and all(
        requested.get(key) in (None, "auto") for key in ("model", "profile")
    )
    choose_effort = fixed["reasoning_effort"] == "auto" and requested.get("reasoning_effort") in (
        None,
        "auto",
    )
    config = plugin.host.config.decisions
    if not (choose_profile or choose_effort) or not (
        config.get("enabled", True)
        and config.get("external_data_consent", False)
        and config.get("mode", "active") == "active"
    ):
        return await policy.resolve(plugin.executor, requested, fixed)

    options = {}
    try:
        options = policy.options(await plugin.executor.models(), requested, fixed)
    except Exception:
        advice = fallback("model_catalog_unavailable")
    else:
        if len(options) == 1:
            advice = {"status": "ok", "choice": next(iter(options)), "source": "catalog_only_option"}
        elif not options:
            advice = fallback("no_supported_execution_options")
        else:
            advice = await decide(
                plugin.host.runtime,
                DecisionQuery(
                    "model_profile",
                    "task-model:" + hashlib.sha256(objective.encode()).hexdigest(),
                    {"objective": objective, "options": options},
                ),
            )
    proposed = requested
    candidate = options.get(advice.get("choice")) if advice["status"] == "ok" else None
    fields = advice.get("field_choices")
    if advice["status"] == "ok" and fields is not None:
        # Native questions may agree on only one unresolved field. Validate the
        # partial recommendation against the same supported candidate set.
        profile = fields.get("profile") if choose_profile else None
        effort = fields.get("reasoning_effort") if choose_effort else None
        matching = [
            o
            for o in options.values()
            if (profile is None or o["profile"] == profile)
            and (effort is None or o["reasoning_effort"] == effort)
        ]
        if (profile or effort) and matching:
            candidate = matching[0]
            choose_profile = choose_profile and profile is not None
            choose_effort = choose_effort and effort is not None
        else:
            candidate = None
            advice = fallback("low_confidence", advice.get("receipt_id"))
    if candidate:
        proposed = {
            **requested,
            "reason": requested.get("reason") or "联合选型；目标、已有流程与验收要求见任务说明",
        }
        if choose_profile:
            proposed["profile"] = candidate["profile"]
        if choose_effort:
            proposed["reasoning_effort"] = candidate["reasoning_effort"]
    elif advice["status"] == "ok":
        advice = fallback("invalid_execution_option", advice.get("receipt_id"))
    try:
        selected = await policy.resolve(plugin.executor, proposed, fixed)
    except Exception:
        if proposed is requested:
            raise
        advice = fallback("suggested_execution_unavailable", advice.get("receipt_id"))
        selected = await policy.resolve(plugin.executor, requested, fixed)
        candidate = None
    if candidate:
        source = advice.get("source", "jev")
        for key, chosen in (("model", choose_profile), ("reasoning_effort", choose_effort)):
            if chosen and selected["selection"]["sources"][key] != "instance_fixed":
                selected["selection"]["sources"][key] = source
        advice = {
            **advice,
            "recommended": {
                "profile": candidate["profile"] if choose_profile else None,
                "model": candidate["model"] if choose_profile else None,
                "reasoning_effort": candidate["reasoning_effort"] if choose_effort else None,
            },
        }
    selected["selection"]["agent_request"] = {
        key: requested[key] for key in ("profile", "model", "reasoning_effort") if key in requested
    }
    selected["selection"]["decision_support"] = advice
    return selected
