"""One optional joint judgment; explicit fields and original fallback remain owned here."""

import asyncio
import json
import math
import hashlib

from assistant_runtime.api.contracts import DecisionQuery
from assistant_runtime.api.decisions import decide, fallback, report_decision


def reject(result, reason):
    return {**result, **fallback(reason, result.get("receipt_id"))}


async def select_execution(plugin, requested, objective, prior=None):
    selected = await _select_execution(plugin, requested, objective, prior)
    selection = selected["selection"]
    review = selection.get("model_review", {})
    result = review.get("decision") or selection.get("decision_support", {})
    applied = review.get("applied", result.get("status") == "ok")
    await report_decision(
        plugin.host.runtime,
        result,
        "adopted" if applied else "rejected",
        "validated_model_choice" if applied else "original_model_choice",
    )
    return selected


async def _select_execution(plugin, requested, objective, prior=None):
    requested = plugin.model_policy.normalize(requested)
    if plugin.model_policy.review_mode == "active":
        return await select_active(plugin, requested, objective, prior)
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
                    summary=objective[:800],
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
            advice = reject(advice, "low_confidence")
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
        advice = reject(advice, "invalid_execution_option")
    try:
        selected = await policy.resolve(plugin.executor, proposed, fixed)
    except Exception:
        if proposed is requested:
            raise
        advice = reject(advice, "suggested_execution_unavailable")
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


async def select_active(plugin, requested, objective, prior):
    """One bounded correction before execution; a declaration can only restrict choices."""
    policy, fixed = plugin.model_policy, plugin.settings.execution
    baseline = await policy.resolve(plugin.executor, requested, fixed)
    locks = {}
    origins = {}
    for key, source in (("model", "model_source"), ("reasoning_effort", "effort_source")):
        explicit = requested.get(key) not in (None, "auto") or (
            key == "model" and requested.get("profile") not in (None, "auto")
        )
        origins[key] = "instance_fixed" if fixed[key] != "auto" else requested.get(source, "unspecified")
        if fixed[key] != "auto" or explicit and requested.get(source) != "assistant":
            locks[key] = baseline[key]
    declared = requested.get("assessment", {})
    # Preference prose informs the judgment; only explicit field provenance and
    # instance settings impose locks. Do not infer constraints from wording.
    config = plugin.host.config.decisions
    enabled = (
        config.get("enabled", True)
        and config.get("external_data_consent", False)
        and config.get("mode", "active") == "active"
    )
    result = fallback("protected_selection" if len(locks) == 2 else "disabled")
    selected, candidate = baseline, None
    threshold = (
        config.get("scenes", {})
        .get("model_review", {})
        .get(
            "threshold",
            config.get("thresholds", {}).get("model_review", config.get("default_threshold", 0.6)),
        )
    )
    if enabled and len(locks) < 2:
        try:
            async with asyncio.timeout(config.get("active_timeout_seconds", 3)):
                catalog = await plugin.executor.models()
                constrained = {**locks, "reason": "Respect declared user choices and instance constraints"}
                options = policy.options(catalog, constrained, fixed)
                if not any(o["model"] == baseline["model"] for o in options.values()):
                    options.update(
                        policy.options(catalog, {**constrained, "model": baseline["model"]}, fixed)
                    )
                state = {
                    "objective": objective,
                    "assessment": declared,
                    "prior_execution": prior,
                    "options": options,
                }
                if len(options) <= 1:
                    result = fallback("no_selection_space")
                else:
                    digest = hashlib.sha256(
                        json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
                    result = await decide(
                        plugin.host.runtime,
                        DecisionQuery(
                            "model_review", "task-review:" + digest, state, summary=objective[:800]
                        ),
                    )
                    if result["status"] == "ok":
                        candidate = options.get(result.get("choice"))
                        probability = result.get("probabilities", {}).get(result.get("choice"))
                        if (
                            not candidate
                            or type(probability) not in (int, float)
                            or not math.isfinite(probability)
                            or not threshold <= probability <= 1
                        ):
                            result = reject(result, "invalid_or_low_probability")
                            candidate = None
                        elif any(candidate[k] != v for k, v in locks.items()):
                            result = reject(result, "protected_selection")
                            candidate = None
                        else:
                            proposed = {
                                **requested,
                                "model": candidate["model"],
                                "reasoning_effort": candidate["reasoning_effort"],
                                "reason": (
                                    "Jev 选型；原推荐依据：" + (requested.get("reason") or "实例默认")
                                )[:400],
                            }
                            if candidate["profile"] != "custom":
                                proposed["profile"] = candidate["profile"]
                            selected = await policy.resolve(plugin.executor, proposed, fixed)
                            if any(selected[k] != candidate[k] for k in ("model", "reasoning_effort")):
                                raise ValueError("Resolved model does not match decision")
        except TimeoutError:
            selected, candidate, result = baseline, None, reject(result, "timeout")
        except Exception:
            selected, candidate, result = baseline, None, reject(result, "review_unavailable")
    record = {
        "mode": "active",
        "threshold": threshold,
        "decision": result,
        "original": {k: baseline[k] for k in ("model", "reasoning_effort")},
        "selected": {k: selected[k] for k in ("model", "reasoning_effort")},
        "origins": origins,
        "protected": locks,
        "applied": candidate is not None,
        "changed": any(selected[k] != baseline[k] for k in ("model", "reasoning_effort")),
    }
    if candidate:
        for key in ("model", "reasoning_effort"):
            if key not in locks:
                selected["selection"]["sources"][key] = "jev"
    selected["selection"]["model_review"] = record
    selected["selection"]["agent_request"] = {
        k: requested[k]
        for k in ("profile", "model", "reasoning_effort", "model_source", "effort_source")
        if k in requested
    }
    return selected
