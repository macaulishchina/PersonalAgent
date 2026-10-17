"""Instance-owned model policy. No inference, provider RPC or task-text classifier."""

from dataclasses import dataclass, field
import time

PROFILES = ("fast", "balanced", "deep")
GUIDANCE = (
    "Choose the lightest profile that can meet the task's acceptance checks. "
    "fast: clear objective, familiar steps and easy verification; "
    "balanced: unfamiliar interfaces or coordination across tools; "
    "deep: conflicting evidence, unexplained failures or complex diagnosis. "
    "Task length and number of steps do not by themselves require deeper reasoning. "
    "Explain uncertainty, available procedure and verification in execution.reason. "
    "Never inherit the parent/main model merely because it created the task. "
    "Network errors, offline devices, missing tools and uncertain side effects need reconciliation, not a stronger model."
)


@dataclass
class ModelPolicy:
    default_profile: str = "fast"
    max_upgrades: int = 1
    profiles: dict = field(
        default_factory=lambda: {
            "fast": {"model": "gpt-6-luna", "reasoning_effort": "low"},
            "balanced": {"model": "gpt-6.1-sol", "reasoning_effort": "low"},
            "deep": {"model": "gpt-6.1-sol", "reasoning_effort": "medium"},
        }
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - {"default_profile", "max_upgrades", "profiles"}:
            raise ValueError("Unknown task model policy settings")
        result = cls()
        result.default_profile = data.get("default_profile", result.default_profile)
        result.max_upgrades = data.get("max_upgrades", result.max_upgrades)
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

    async def resolve(self, executor, requested, fixed):
        profile = requested.get("profile", self.default_profile)
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
        reason = reason or (
            "使用实例固定配置"
            if any(v != "auto" for v in fixed.values())
            else f"任务未明确选型，采用实例默认 {self.default_profile} 策略；不继承发起会话配置"
        )
        choices, sources = {}, {}
        for key in ("model", "reasoning_effort"):
            if fixed[key] != "auto":
                choices[key], sources[key] = fixed[key], "instance_fixed"
            elif requested.get(key, "auto") != "auto":
                choices[key], sources[key] = requested[key], "agent"
            else:
                choices[key], sources[key] = self.profiles[profile][key], "profile_default"
            if not isinstance(choices[key], str):
                raise ValueError("Invalid task model selection")
        resolved = await executor.select(choices["model"], choices["reasoning_effort"])
        matched = [p for p in PROFILES if self.profiles[p] == resolved]
        actual = profile if self.profiles[profile] == resolved else matched[0] if matched else "custom"
        selection = {
            "version": 1,
            "at": time.time(),
            "profile": actual,
            "requested_profile": profile,
            "sources": sources,
            "reason": reason,
            "resolved": resolved,
        }
        return {"kind": "codex", **resolved, "profile": actual, "reason": reason, "selection": selection}
