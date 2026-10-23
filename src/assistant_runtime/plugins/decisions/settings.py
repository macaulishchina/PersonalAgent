"""Explicit external-context consent and bounded optional judgment settings."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
import math
import re


@dataclass
class DecisionSettings:
    enabled: bool = field(default_factory=lambda: configuration_default(__file__, "enabled"))
    external_data_consent: bool = field(
        default_factory=lambda: configuration_default(__file__, "external_data_consent")
    )
    mode: str = field(default_factory=lambda: configuration_default(__file__, "mode"))
    model: str = field(default_factory=lambda: configuration_default(__file__, "model"))
    queue_size: int = field(default_factory=lambda: configuration_default(__file__, "queue_size"))
    retention: int = field(default_factory=lambda: configuration_default(__file__, "retention"))
    timeout_seconds: float = field(default_factory=lambda: configuration_default(__file__, "timeout_seconds"))
    max_age_seconds: int = field(default_factory=lambda: configuration_default(__file__, "max_age_seconds"))
    active_timeout_seconds: float = field(
        default_factory=lambda: configuration_default(__file__, "active_timeout_seconds")
    )
    warmup_enabled: bool = field(default_factory=lambda: configuration_default(__file__, "warmup_enabled"))
    keepalive_seconds: int = field(
        default_factory=lambda: configuration_default(__file__, "keepalive_seconds")
    )
    dns_cache_seconds: int = field(
        default_factory=lambda: configuration_default(__file__, "dns_cache_seconds")
    )
    default_threshold: float = field(
        default_factory=lambda: configuration_default(__file__, "default_threshold")
    )
    concurrency: int = field(default_factory=lambda: configuration_default(__file__, "concurrency"))
    timezone: str = field(default_factory=lambda: configuration_default(__file__, "timezone"))
    thresholds: dict = field(default_factory=lambda: configuration_default(__file__, "thresholds"))
    scenes: dict = field(default_factory=lambda: configuration_default(__file__, "scenes"))

    def policy(self, kind):
        defaults = {"voice_intent": "parallel", "selection": "parallel"}
        values = self.scenes.get(kind, {})
        return {
            "enabled": self.enabled and values.get("enabled", True),
            "mode": "shadow" if self.mode == "shadow" else values.get("mode", defaults.get(kind, "direct")),
            "threshold": values.get("threshold", self.thresholds.get(kind, self.default_threshold)),
            "timeout_seconds": values.get("timeout_seconds", self.active_timeout_seconds),
        }

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown decision settings")
        result = cls(**data)
        if (
            type(result.enabled) is not bool
            or type(result.external_data_consent) is not bool
            or type(result.warmup_enabled) is not bool
            or result.mode not in {"shadow", "active"}
        ):
            raise ValueError("Invalid decision enabled/mode")
        if not isinstance(result.model, str) or not re.fullmatch(r"jev-[a-zA-Z0-9._-]{1,64}", result.model):
            raise ValueError("Invalid Jev model")
        for key, low, high in (
            ("queue_size", 1, 128),
            ("concurrency", 2, 16),
            ("retention", 100, 10000),
            ("max_age_seconds", 1, 300),
            ("keepalive_seconds", 15, 600),
            ("dns_cache_seconds", 0, 3600),
        ):
            if type(getattr(result, key)) is not int or not low <= getattr(result, key) <= high:
                raise ValueError("Invalid decision " + key)
        for key, maximum in (("timeout_seconds", 30), ("active_timeout_seconds", 5)):
            value = getattr(result, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= maximum:
                raise ValueError("Invalid decision timeout")
        from zoneinfo import ZoneInfo
        from .questions import VERSIONS

        try:
            ZoneInfo(result.timezone)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Invalid decision archive timezone") from exc
        if not isinstance(result.thresholds, dict) or set(result.thresholds) - VERSIONS.keys():
            raise ValueError("Unknown decision threshold")
        if not isinstance(result.scenes, dict) or set(result.scenes) - VERSIONS.keys():
            raise ValueError("Unknown decision scene")
        for values in result.scenes.values():
            if not isinstance(values, dict) or set(values) - {
                "enabled",
                "mode",
                "threshold",
                "timeout_seconds",
            }:
                raise ValueError("Invalid decision scene policy")
            if type(values.get("enabled", True)) is not bool or values.get("mode", "direct") not in {
                "direct",
                "parallel",
                "shadow",
            }:
                raise ValueError("Invalid decision scene mode")
            timeout = values.get("timeout_seconds", result.active_timeout_seconds)
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 5:
                raise ValueError("Invalid decision scene timeout")
        probabilities = [result.default_threshold, *result.thresholds.values()]
        probabilities.extend(v["threshold"] for v in result.scenes.values() if "threshold" in v)
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities):
            raise ValueError("Invalid decision threshold")
        return result
