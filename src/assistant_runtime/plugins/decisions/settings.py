"""Explicit external-context consent and bounded optional judgment settings."""

from dataclasses import dataclass, field
import math
import re


@dataclass
class DecisionSettings:
    enabled: bool = True
    external_data_consent: bool = False
    mode: str = "active"
    model: str = "jev-1.13.0"
    queue_size: int = 32
    retention: int = 2000
    timeout_seconds: float = 8
    max_age_seconds: int = 30
    active_timeout_seconds: float = 3
    warmup_enabled: bool = True
    keepalive_seconds: int = 120
    dns_cache_seconds: int = 300
    thresholds: dict = field(
        default_factory=lambda: {
            "voice_intent": 0.98,
            "event_relevance": 0.98,
            "model_profile": 0.90,
            "model_review": 0.90,
            "selection": 0.90,
        }
    )

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
        defaults = cls().thresholds
        if not isinstance(result.thresholds, dict) or set(result.thresholds) - defaults.keys():
            raise ValueError("Unknown decision threshold")
        result.thresholds = {**defaults, **result.thresholds}
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0.5 <= v <= 1
            for v in result.thresholds.values()
        ):
            raise ValueError("Invalid decision threshold")
        return result
