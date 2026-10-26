"""One optional joint judgment; explicit fields and original fallback remain owned here."""

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
    return await plugin.model_policy.resolve(
        plugin.executor, requested, plugin.settings.execution, plugin.model_defaults()
    )


async def select_active(plugin, requested, objective, prior):
    """One bounded correction before execution; a declaration can only restrict choices."""
    policy, fixed = plugin.model_policy, plugin.settings.execution
    baseline = await policy.resolve(plugin.executor, requested, fixed, plugin.model_defaults())
    locks = {}
    origins = {}
    for key, source in (("model", "model_source"), ("reasoning_effort", "effort_source")):
        explicit = requested.get(key) not in (None, "auto") or (
            key == "model" and bool(requested.get("recommendation"))
        )
        origins[key] = "instance_fixed" if fixed[key] != "auto" else requested.get(source, "unspecified")
        if fixed[key] != "auto" or explicit and requested.get(source) != "assistant":
            locks[key] = baseline[key]
    declared = requested.get("assessment", {})
    # Preference prose informs the judgment; only explicit field provenance and
    # instance settings impose locks. Do not infer constraints from wording.
    result = fallback("protected_selection" if len(locks) == 2 else "decision_service_unavailable")
    selected, candidate = baseline, None
    threshold = None
    if len(locks) < 2 and "decisions" in plugin.host.runtime.plugins:
        try:
            catalog = await plugin.executor.models()
            constrained = {**locks, "reason": "Respect declared user choices and instance constraints"}
            options = policy.options(catalog, constrained, fixed, plugin.model_defaults())
            if not any(o["model"] == baseline["model"] for o in options.values()):
                options.update(
                    policy.options(
                        catalog,
                        {**constrained, "model": baseline["model"]},
                        fixed,
                        plugin.model_defaults(),
                    )
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
                    DecisionQuery("model_review", "task-review:" + digest, state, summary=objective[:800]),
                )
                if result["status"] == "ok":
                    candidate = options.get(result.get("choice"))
                    threshold = result.get("probability_threshold", 0)
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
                            "reason": ("Jev 选型；原推荐依据：" + (requested.get("reason") or "实例默认"))[
                                :400
                            ],
                        }
                        proposed.pop("recommendation", None)
                        if candidate.get("recommendation"):
                            proposed["recommendation"] = candidate["recommendation"]
                        selected = await policy.resolve(
                            plugin.executor, proposed, fixed, plugin.model_defaults()
                        )
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
        for k in ("recommendation", "model", "reasoning_effort", "model_source", "effort_source")
        if k in requested
    }
    return selected
