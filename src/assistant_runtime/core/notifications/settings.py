from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field


@dataclass
class NotificationSettings:
    channels: list[str] = field(default_factory=lambda: configuration_default(__file__, "channels"))
    max_attempts: int = field(default_factory=lambda: configuration_default(__file__, "max_attempts"))
    retry_seconds: int = field(default_factory=lambda: configuration_default(__file__, "retry_seconds"))
    delivery_timeout: int = field(default_factory=lambda: configuration_default(__file__, "delivery_timeout"))
    max_notifications: int = field(
        default_factory=lambda: configuration_default(__file__, "max_notifications")
    )
    voice_enabled: bool = field(default_factory=lambda: configuration_default(__file__, "voice_enabled"))
    voice_spacing_seconds: int = field(
        default_factory=lambda: configuration_default(__file__, "voice_spacing_seconds")
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown notification settings")
        result = cls(**data)
        for key, low, high in (
            ("max_attempts", 1, 20),
            ("retry_seconds", 1, 3600),
            ("delivery_timeout", 5, 120),
            ("max_notifications", 100, 100000),
            ("voice_spacing_seconds", 0, 3600),
        ):
            value = getattr(result, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid notifications.{key}")
        if (
            not isinstance(result.channels, list)
            or not result.channels
            or any(not isinstance(c, str) or not c.isidentifier() for c in result.channels)
            or len(result.channels) != len(set(result.channels))
            or "voice" in result.channels
        ):
            raise ValueError("Notification channels must be unique registered names; voice is explicit")
        if type(result.voice_enabled) is not bool:
            raise ValueError("voice_enabled must be boolean")
        return result
