"""Task-owned recommendations and deterministic selection; no provider inference."""

from dataclasses import dataclass, field
import re
import time

from assistant_runtime.api.configuration import configuration_default

GUIDANCE = (
    "Omit execution.model to use the configured main conversation model. "
    "Omitted effort inherits the main conversation's concrete effort. "
    "For a task, auto means the initiating model must choose a supported concrete effort; "
    "if both task and main effort are auto, supply reasoning_effort explicitly. "
    "Models without configurable reasoning use auto. Main conversation auto remains provider-controlled. "
    "Recommendations are optional named suggestions, not ordered tiers or fallback models. "
    "Choose using their reason/description and the task's acceptance checks; task length alone is not difficulty. "
    "Explain explicit choices in execution.reason. Declare model_source/effort_source as assistant for "
    "your recommendations, user only for explicit user choices. Missing source labels never imply user intent. "
    "Optional decision review may adjust assistant recommendations, never user or instance-fixed choices. "
    "Provider errors fail the execution; no automatic alternative model or operation replay."
)


@dataclass
class ModelPolicy:
    max_upgrades: int = field(
        default_factory=lambda: configuration_default(__file__, "model_policy")["max_upgrades"]
    )
    review_mode: str = field(
        default_factory=lambda: configuration_default(__file__, "model_policy")["review_mode"]
    )
    recommendations: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {"max_upgrades", "review_mode", "recommendations"}:
            raise ValueError("Unknown task model policy settings; use named recommendations")
        result = cls(**data)
        if result.review_mode not in ("off", "shadow", "active"):
            raise ValueError("Model review supports off, shadow or active")
        if type(result.max_upgrades) is not int or not 0 <= result.max_upgrades <= 3:
            raise ValueError("Task model handoff limit must be 0..3")
        if not isinstance(result.recommendations, dict) or len(result.recommendations) > 32:
            raise ValueError("Task recommendations must be a named object with at most 32 items")
        clean, names = {}, set()
        for ident, row in result.recommendations.items():
            if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", ident):
                raise ValueError("Recommendation ID must be a stable lowercase identifier")
            if not isinstance(row, dict) or set(row) - {
                "name",
                "model",
                "reasoning_effort",
                "reason",
                "description",
            }:
                raise ValueError("Unknown task recommendation fields")
            for key, limit in (("name", 80), ("model", 160), ("reason", 400), ("description", 800)):
                if not isinstance(row.get(key), str) or not 1 <= len(row[key].strip()) <= limit:
                    raise ValueError("Recommendation requires bounded nonempty " + key)
            effort = row.get("reasoning_effort", "auto")
            if not isinstance(effort, str) or not 1 <= len(effort.strip()) <= 160 or row["model"] == "auto":
                raise ValueError("Recommendation requires a concrete model and valid effort")
            if row["name"].strip() in names:
                raise ValueError("Recommendation names must be unique")
            names.add(row["name"].strip())
            clean[ident] = {**row, "name": row["name"].strip(), "reasoning_effort": effort}
        result.recommendations = clean
        return result

    def describe(self, models, main, fixed):
        catalog = {m["model"]: m for m in models}
        return {
            "version": 2,
            "defaults": {
                k: fixed[k] if fixed[k] != "auto" else main[k] for k in ("model", "reasoning_effort")
            },
            "max_upgrades": self.max_upgrades,
            "review_mode": self.review_mode,
            "guidance": GUIDANCE,
            "recommendations": {
                ident: {
                    **row,
                    "available": row["model"] in catalog
                    and catalog[row["model"]].get("available", True)
                    and (
                        row["reasoning_effort"] == "auto"
                        or row["reasoning_effort"] in supported(catalog[row["model"]])
                    ),
                }
                for ident, row in self.recommendations.items()
            },
        }

    @staticmethod
    def normalize(requested):
        requested = dict(requested)
        for choice, source in (("model", "model_source"), ("reasoning_effort", "effort_source")):
            value = requested.get(source)
            if value is not None and value not in ("assistant", "user"):
                raise ValueError("Invalid " + source)
            explicit = requested.get(choice) not in (None, "auto") or (
                choice == "model" and bool(requested.get("recommendation"))
            )
            if value == "user" and not explicit:
                raise ValueError(source + " requires an explicit choice")
            if value is None or not explicit:
                requested.pop(source, None)
        return requested

    def validate(self, requested, fixed):
        requested = self.normalize(requested)
        ident = requested.get("recommendation")
        if ident is not None and (not isinstance(ident, str) or ident not in self.recommendations):
            raise ValueError("Unknown task recommendation")
        if ident and requested.get("model") not in (None, "auto", self.recommendations[ident]["model"]):
            raise ValueError("Explicit model conflicts with recommendation")
        reason = requested.get("reason")
        if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 400):
            raise ValueError("Task model reason must be 1..400 characters")
        if (
            any(
                requested.get(k) not in (None, "auto")
                for k in ("model", "reasoning_effort", "recommendation")
            )
            and not reason
            and all(v == "auto" for v in fixed.values())
        ):
            from assistant_runtime.api.tool_calls import invalid_arguments

            raise invalid_arguments(
                "Explain task uncertainty and verification in execution.reason", "execution.reason"
            )
        for key in ("model", "reasoning_effort"):
            value = requested.get(key)
            if value is not None and (not isinstance(value, str) or not 1 <= len(value.strip()) <= 160):
                raise ValueError("Invalid task " + key)
        return ident

    def options(self, models, requested, fixed, main):
        """Bounded, concrete candidates from declared recommendations and the baseline."""
        self.validate(requested, fixed)
        catalog = {m["model"]: m for m in models}
        model = fixed["model"] if fixed["model"] != "auto" else requested.get("model")
        effort = (
            fixed["reasoning_effort"]
            if fixed["reasoning_effort"] != "auto"
            else requested.get("reasoning_effort")
        )
        rec = requested.get("recommendation")
        if rec:
            model = model if model not in (None, "auto") else self.recommendations[rec]["model"]
        rows = [
            (
                None,
                {
                    "name": "主对话默认",
                    "model": main["model"],
                    "reason": "沿用主对话配置",
                    "description": "未指定任务模型时使用",
                },
            ),
            *self.recommendations.items(),
        ]
        if model not in (None, "auto") and model not in [r["model"] for _, r in rows]:
            rows.append(
                (
                    None,
                    {
                        "name": "显式模型",
                        "model": model,
                        "reason": "调用者明确选择",
                        "description": "保留当前选择",
                    },
                )
            )
        result, seen = {}, {}
        for ident, row in rows:
            if model not in (None, "auto", row["model"]):
                continue
            entry = catalog.get(row["model"])
            if not entry:
                continue
            efforts = [e for e in supported(entry) if e != "auto"] or ["auto"]
            for value in efforts:
                if effort not in (None, "auto", value):
                    continue
                pair = (row["model"], value)
                score = 3 if ident and row.get("reasoning_effort") == value else 1 if ident else 0
                if pair in seen and seen[pair][1] >= score:
                    continue
                if pair in seen:
                    result.pop(seen[pair][0])
                option_id = (ident or ("main" if row["model"] == main["model"] else "selected")) + "_" + value
                seen[pair] = (option_id, score)
                result[option_id] = {
                    "recommendation": ident,
                    "model": row["model"],
                    "reasoning_effort": value,
                    "name": row["name"],
                    "reason": row["reason"],
                    "description": row["description"],
                }
        return result

    async def resolve(self, executor, requested, fixed, main):
        requested = self.normalize(requested)
        ident = self.validate(requested, fixed)
        row = self.recommendations.get(ident, {})
        choices, sources = {}, {}
        for key in ("model", "reasoning_effort"):
            if fixed[key] != "auto":
                choices[key], sources[key] = fixed[key], "instance_fixed"
            elif requested.get(key) not in (None, "auto"):
                choices[key], sources[key] = requested[key], "agent"
            elif row.get(key) not in (None, "auto"):
                choices[key], sources[key] = row[key], "recommendation"
            else:
                choices[key], sources[key] = main[key], "main_conversation"
        if choices["model"] in (None, "auto", "unconfigured", ""):
            raise ValueError(
                "Task model is not configured; configure the main model or choose execution.model"
            )
        if choices["reasoning_effort"] == "auto":
            models = await executor.models()
            entry = next((m for m in models if choices["model"] in (m.get("id"), m["model"])), {})
            levels = [e for e in supported(entry) if e != "auto"]
            if levels:
                raise ValueError(
                    "Task auto effort requires the initiating model to choose execution.reasoning_effort from: "
                    + ", ".join(levels)
                )
            sources["reasoning_effort"] = "model_without_reasoning"
        resolved = await executor.select(choices["model"], choices["reasoning_effort"])
        reason = requested.get("reason") or (
            "使用实例固定配置" if any(v != "auto" for v in fixed.values()) else "沿用主对话模型与思考深度"
        )
        actual = ident if row.get("model") == resolved["model"] else None
        selection = {
            "version": 2,
            "at": time.time(),
            "recommendation": actual,
            "requested_recommendation": ident,
            "sources": sources,
            "reason": reason,
            "resolved": resolved,
        }
        return {
            "kind": "agent",
            **resolved,
            **({"recommendation": actual} if actual else {}),
            "reason": reason,
            "selection": selection,
            **{k: requested[k] for k in ("model_source", "effort_source") if k in requested},
        }


def supported(model):
    return [e["reasoning_effort"] for e in model.get("supported_reasoning_efforts", [])]
