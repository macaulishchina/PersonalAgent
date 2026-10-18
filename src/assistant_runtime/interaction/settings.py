"""Opt-in until the instance's recognition and acoustic acceptance passes."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class InteractionSettings:
    enabled: bool = False
    asr_model_dir: str = ""
    tts_model_dir: str = ""
    tts_voice: int = 46
    follow_up_seconds: float = 3
    context_seconds: float = 120
    acknowledgement_seconds: float = 0.8
    routing_seconds: float = 1
    input_buffer_seconds: float = 30

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown interaction settings")
        result = cls(**value)
        if type(result.enabled) is not bool:
            raise ValueError("interaction.enabled must be boolean")
        for key in ("asr_model_dir", "tts_model_dir"):
            if not isinstance(getattr(result, key), str):
                raise ValueError("Invalid speech model directory")
            if result.enabled and not getattr(result, key):
                raise ValueError("Enabled interaction requires explicit local model directories")
        if type(result.tts_voice) is not int or not 0 <= result.tts_voice <= 1000:
            raise ValueError("Invalid local speech voice")
        for key, low, high in (
            ("follow_up_seconds", 1, 15),
            ("context_seconds", 10, 600),
            ("acknowledgement_seconds", 0.2, 3),
            ("routing_seconds", 0.1, 3),
            ("input_buffer_seconds", 2, 60),
        ):
            number = getattr(result, key)
            if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError("Invalid interaction." + key)
        return result
