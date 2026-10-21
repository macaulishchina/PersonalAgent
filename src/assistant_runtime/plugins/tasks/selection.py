"""Instance-owned model policy. No inference, provider RPC or task-text classifier."""

from dataclasses import dataclass, field
import time

PROFILES = ("fast", "balanced", "deep", "ultra")
GUIDANCE = (
    "Choose the lightest profile that can meet the task's acceptance checks. "
    "fast: clear objective, familiar steps and easy verification; "
    "balanced: unfamiliar interfaces or coordination across tools; "
    "deep: conflicting evidence, unexplained failures or complex diagnosis. "
    "ultra: use sparingly for exceptionally complex reasoning or original scientific research, "
    "only when there is concrete justification that deep is insufficient. "
    "Routine literature search, summaries and a research-related title do not justify ultra. "
    "Profile selects the execution model tier; reasoning_effort can be chosen independently "
    "from that model's supported efforts. Low suits known steps; medium suits exploration; "
    "high or above needs difficult reasoning, not a long task. Prefer the lightest adequate effort. "
    "Task length and number of steps do not by themselves require deeper reasoning. "
    "Explain uncertainty, available procedure and verification in execution.reason. "
    "Declare model_source/effort_source as assistant for your recommendations, user only for explicit user choices. "
    "Source labels require corresponding choices; redundant assistant labels on missing/auto fields are ignored. "
    "Free-form user_preference is context, not a model/effort lock; encode explicit user choices in those fields. "
    "Active Jev correction may change assistant recommendations; user/fixed/unlabelled explicit fields stay protected. "
    "Never inherit the parent/main model merely because it created the task. "
    "Network errors, offline devices, missing tools and uncertain side effects need reconciliation, not a stronger model."
)


@dataclass
class ModelPolicy:
    default_profile: str = "fast"
    max_upgrades: int = 1
    review_mode: str = "shadow"
    fallbacks: dict = field(default_factory=dict)
    profiles: dict = field(
        default_factory=lambda: {
            "fast": {"model": "gpt-6-luna", "reasoning_effort": "low"},
            "balanced": {"model": "gpt-6.1-sol", "reasoning_effort": "low"},
            "deep": {"model": "gpt-6.1-sol", "reasoning_effort": "medium"},
            "ultra": {"model": "gpt-6-astra", "reasoning_effort": "high"},
        }
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {
            "default_profile",
            "max_upgrades",
            "profiles",
            "fallbacks",
            "review_mode",
        }:
            raise ValueError("Unknown task model policy settings")
        result = cls()
        result.review_mode = data.get("review_mode", "shadow")
        if not isinstance(result.review_mode, str) or result.review_mode not in {"off", "shadow", "active"}:
            raise ValueError("Model review supports off, shadow or active")
        result.default_profile = data.get("default_profile", result.default_profile)
        result.max_upgrades = data.get("max_upgrades", result.max_upgrades)
        result.fallbacks = data.get("fallbacks", {})
        if not isinstance(result.fallbacks, dict) or len(result.fallbacks) > 64:
            raise ValueError("Invalid task fallback configuration")
        for model, candidates in result.fallbacks.items():
            if (
                not isinstance(model, str)
                or not 1 <= len(model) <= 160
                or not isinstance(candidates, list)
                or not 1 <= len(candidates) <= 3
            ):
                raise ValueError("Task fallback chains require 1..3 concrete candidates")
            seen = {model}
            for candidate in candidates:
                if (
                    not isinstance(candidate, dict)
                    or set(candidate) != {"model", "reasoning_effort"}
                    or any(
                        not isinstance(v, str) or not 1 <= len(v) <= 160 or v == "auto"
                        for v in candidate.values()
                    )
                ):
                    raise ValueError("Task fallback requires concrete model and reasoning effort")
                if candidate["model"] in seen:
                    raise ValueError("Task fallback candidates cannot repeat")
                seen.add(candidate["model"])
        if (
            result.default_profile not in PROFILES
            or type(result.max_upgrades) is not int
            or not 0 <= result.max_upgrades <= 3
        ):
            raise ValueError("Invalid task model profile/upgrade limit")
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict) or set(profiles) - set(PROFILES):
            raise ValueError("Unknown task model profile")
        for name, values in profiles.items():
            if not isinstance(values, dict) or set(values) - {"model", "reasoning_effort"}:
                raise ValueError("Invalid task model profile fields")
            result.profiles[name].update(values)
        for values in result.profiles.values():
            if any(
                not isinstance(v, str) or not 1 <= len(v.strip()) <= 160 or v == "auto"
                for v in values.values()
            ):
                raise ValueError("Task profiles require concrete model and reasoning effort")
        return result

    def describe(self, models):
        catalog = {m["model"]: m for m in models}
        return {
            "version": 1,
            "default_profile": self.default_profile,
            "max_upgrades": self.max_upgrades,
            "review_mode": self.review_mode,
            "fallbacks": self.fallbacks,
            "guidance": GUIDANCE,
            "profiles": {
                name: {
                    **values,
                    "available": values["model"] in catalog
                    and values["reasoning_effort"]
                    in [
                        e["reasoningEffort"]
                        for e in catalog[values["model"]].get("supportedReasoningEfforts", [])
                    ],
                }
                for name, values in self.profiles.items()
            },
        }

    @staticmethod
    def normalize(requested):
        """Drop empty recommendation provenance; never guess an explicit user choice."""
        requested = dict(requested)
        for choice_field, source in (("model", "model_source"), ("reasoning_effort", "effort_source")):
            value = requested.get(source)
            if value is not None and value not in ("assistant", "user"):
                raise ValueError("Invalid " + source)
            explicit = requested.get(choice_field) not in (None, "auto") or (
                choice_field == "model" and requested.get("profile") not in (None, "auto")
            )
            if value == "user" and not explicit:
                raise ValueError(source + " requires an explicit choice")
            if value is None or not explicit:
                requested.pop(source, None)
        return requested

    def validate(self, requested, fixed):
        requested = self.normalize(requested)
        profile = requested.get("profile")
        if profile in (None, "auto"):
            profile = self.default_profile
        if profile not in PROFILES:
            raise ValueError("Unknown task execution profile")
        reason = requested.get("reason")
        explicit = any(
            requested.get(k) not in (None, "auto") for k in ("model", "reasoning_effort", "profile")
        )
        if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 400):
            raise ValueError("Task model reason must be 1..400 characters")
        if explicit and not reason and all(v == "auto" for v in fixed.values()):
            raise ValueError("Explain task uncertainty and verification in execution.reason")
        for key in ("model", "reasoning_effort"):
            value = requested.get(key)
            if value not in (None, "auto") and (
                not isinstance(value, str) or not 1 <= len(value.strip()) <= 160
            ):
                raise ValueError("Invalid task " + key)
        return profile

    def options(self, models, requested, fixed):
        """Only configured tiers/account-supported efforts, preserving explicit fields."""
        self.validate(requested, fixed)
        catalog = {alias: row for row in models for alias in (row.get("id"), row["model"]) if alias}
        model = fixed["model"] if fixed["model"] != "auto" else requested.get("model")
        effort = (
            fixed["reasoning_effort"]
            if fixed["reasoning_effort"] != "auto"
            else requested.get("reasoning_effort")
        )
        profile = requested.get("profile")
        profiles = [profile] if profile not in (None, "auto") else list(PROFILES)
        if model not in (None, "auto") and profile in (None, "auto"):
            profiles = [p for p in PROFILES if self.profiles[p]["model"] == model] or ["custom"]
        result = {}
        for tier in profiles:
            selected_model = model if model not in (None, "auto") else self.profiles[tier]["model"]
            supported = catalog.get(selected_model, {}).get("supportedReasoningEfforts", [])
            for item in supported:
                value = item["reasoningEffort"]
                if value not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
                    continue
                if effort not in (None, "auto", value):
                    continue
                result[tier + "_" + value] = {
                    "profile": tier,
                    "model": selected_model,
                    "reasoning_effort": value,
                }
        # Identical model/effort aliases would split judgment probability. Prefer
        # the tier whose configured default matches, then the higher tier name.
        unique = {}
        for ident, option in result.items():
            tier, effort = option["profile"], option["reasoning_effort"]
            identity = (catalog[option["model"]]["model"], effort)
            score = (
                self.profiles.get(tier, {}).get("reasoning_effort") == effort,
                PROFILES.index(tier) if tier in PROFILES else -1,
            )
            if identity not in unique or score > unique[identity][0]:
                unique[identity] = (score, ident, option)
        return {ident: option for _, ident, option in unique.values()}

    async def resolve(self, executor, requested, fixed):
        requested = self.normalize(requested)
        profile = self.validate(requested, fixed)
        reason = requested.get("reason")
        reason = reason or (
            "使用实例固定配置"
            if any(v != "auto" for v in fixed.values())
            else f"任务未明确选型，采用实例默认 {self.default_profile} 策略；不继承发起会话配置"
        )
        choices, sources = {}, {}
        for key in ("model", "reasoning_effort"):
            if fixed[key] != "auto":
                choices[key], sources[key] = fixed[key], "instance_fixed"
            elif requested.get(key) not in (None, "auto"):
                choices[key], sources[key] = requested[key], "agent"
            else:
                choices[key], sources[key] = self.profiles[profile][key], "profile_default"
            if not isinstance(choices[key], str):
                raise ValueError("Invalid task model selection")
        resolved = await executor.select(choices["model"], choices["reasoning_effort"])
        matched = [p for p in PROFILES if self.profiles[p] == resolved]
        model_matched = [
            p for p in PROFILES if self.profiles[p]["model"] in (choices["model"], resolved["model"])
        ]
        actual = (
            profile
            if self.profiles[profile]["model"] in (choices["model"], resolved["model"])
            else matched[0]
            if matched
            else model_matched[-1]
            if model_matched
            else "custom"
        )
        selection = {
            "version": 1,
            "at": time.time(),
            "profile": actual,
            "requested_profile": profile,
            "sources": sources,
            "reason": reason,
            "resolved": resolved,
        }
        return {
            "kind": "codex",
            **resolved,
            "profile": actual,
            "reason": reason,
            "selection": selection,
            **{k: requested[k] for k in ("model_source", "effort_source") if k in requested},
        }
