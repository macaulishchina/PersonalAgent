"""Plugin-owned metering policy. Absence of an optional limit means disabled."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field, asdict
import hashlib
import json
import math

LIMITS = {
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_compactions",
    "max_seconds",
    "max_cost_cny",
    "concurrency",
    "requests_per_second",
}
SELECTORS = {"work_id", "task_id", "service", "provider", "model"}


def limits(value):
    if not isinstance(value, dict) or set(value) - LIMITS:
        raise ValueError("Invalid ai_usage limits")
    for key, n in value.items():
        if type(n) not in (int, float) or not math.isfinite(n) or n <= 0:
            raise ValueError("Invalid ai_usage limit: " + key)
        if key not in {"max_seconds", "max_cost_cny", "requests_per_second"} and type(n) is not int:
            raise ValueError("Usage counts must be integers")


@dataclass(frozen=True)
class UsageSettings:
    max_requests: int = field(default_factory=lambda: configuration_default(__file__, "max_requests"))
    warning_fraction: float = field(
        default_factory=lambda: configuration_default(__file__, "warning_fraction")
    )
    queue_size: int = field(default_factory=lambda: configuration_default(__file__, "queue_size"))
    retention_days: int = field(default_factory=lambda: configuration_default(__file__, "retention_days"))
    aggregate_days: int = field(default_factory=lambda: configuration_default(__file__, "aggregate_days"))
    body_enabled: bool = field(default_factory=lambda: configuration_default(__file__, "body_enabled"))
    body_days: int = field(default_factory=lambda: configuration_default(__file__, "body_days"))
    body_max_bytes: int = field(default_factory=lambda: configuration_default(__file__, "body_max_bytes"))
    work_limits: dict = field(default_factory=lambda: configuration_default(__file__, "work_limits"))
    rules: list = field(default_factory=lambda: configuration_default(__file__, "rules"))
    prices: dict = field(default_factory=lambda: configuration_default(__file__, "prices"))

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown ai_usage configuration")
        obj = cls(**value)
        for name in (
            "max_requests",
            "queue_size",
            "retention_days",
            "aggregate_days",
            "body_days",
            "body_max_bytes",
        ):
            if type(getattr(obj, name)) is not int or not 1 <= getattr(obj, name) <= 1_000_000:
                raise ValueError("Invalid ai_usage." + name)
        if type(obj.warning_fraction) not in (int, float) or not 0 < obj.warning_fraction < 1:
            raise ValueError("Invalid ai_usage.warning_fraction")
        if type(obj.body_enabled) is not bool:
            raise ValueError("Invalid ai_usage.body_enabled")
        limits(obj.work_limits)
        if not isinstance(obj.rules, list) or len(obj.rules) > 128:
            raise ValueError("Invalid usage rules")
        names = set()
        for rule in obj.rules:
            if not isinstance(rule, dict) or set(rule) - {"id", "scope", "match", "window_seconds", "limits"}:
                raise ValueError("Invalid usage rule")
            if not isinstance(rule.get("id"), str) or not rule["id"] or rule["id"] in names:
                raise ValueError("Usage rule needs a unique id")
            names.add(rule["id"])
            if rule.get("scope") not in {"work", "plan", "global"}:
                raise ValueError("Invalid usage rule scope")
            match = rule.get("match", {})
            if (
                not isinstance(match, dict)
                or set(match) - SELECTORS
                or any(not isinstance(v, str) or not v for v in match.values())
            ):
                raise ValueError("Invalid usage rule selector")
            window = rule.get("window_seconds")
            if window is not None and (type(window) is not int or window <= 0):
                raise ValueError("Invalid usage time window")
            if rule["scope"] != "work" and window is None:
                raise ValueError("Plan/global usage rules require an explicit time window")
            if window is not None and window > obj.retention_days * 86400:
                raise ValueError("Usage window exceeds detailed retention")
            limits(rule.get("limits", {}))
        if not isinstance(obj.prices, dict):
            raise ValueError("Invalid usage prices")
        price_routes = set()
        for price in obj.prices.values():
            if not isinstance(price, dict) or set(price) - {
                "provider",
                "model",
                "input_cny_per_million",
                "output_cny_per_million",
                "request_cny",
                "audio_cny_per_second",
                "text_cny_per_character",
            }:
                raise ValueError("Invalid usage price")
            if not isinstance(price.get("provider"), str) or not isinstance(price.get("model", ""), str):
                raise ValueError("Usage price needs provider")
            route = (price["provider"], price.get("model", ""))
            if route in price_routes:
                raise ValueError("Ambiguous usage price route")
            price_routes.add(route)
            if not set(price) & {
                "request_cny",
                "input_cny_per_million",
                "output_cny_per_million",
                "audio_cny_per_second",
                "text_cny_per_character",
            }:
                raise ValueError("Usage price needs explicit amounts")
            if ("input_cny_per_million" in price) != ("output_cny_per_million" in price):
                raise ValueError("Token prices require both input and output amounts")
            for k, v in price.items():
                if k not in {"provider", "model"} and (
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0
                ):
                    raise ValueError("Invalid usage price amount")
        return obj

    def revision(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]
