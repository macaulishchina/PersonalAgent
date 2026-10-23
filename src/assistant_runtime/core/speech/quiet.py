"""Daily, local-time silence policy; independent of audio device settings."""

from dataclasses import dataclass
from datetime import datetime, time
import re
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time
    timezone: ZoneInfo
    enable: bool

    @classmethod
    def from_settings(cls, settings):
        if not isinstance(settings, dict):
            raise ValueError("quiet_hours must be a table")
        if not settings:
            return None
        if set(settings) != {"enable", "start", "end", "timezone"}:
            raise ValueError("quiet_hours requires exactly enable, start, end and timezone")
        if type(settings["enable"]) is not bool:
            raise ValueError("quiet_hours.enable must be boolean")
        values = []
        for name in ("start", "end"):
            value = settings[name]
            if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                raise ValueError(f"quiet_hours.{name} must be HH:MM")
            values.append(time.fromisoformat(value))
        if values[0] == values[1]:
            raise ValueError("quiet_hours start and end must differ")
        if not isinstance(settings["timezone"], str):
            raise ValueError("quiet_hours.timezone must be an IANA timezone")
        try:
            zone = ZoneInfo(settings["timezone"])
        except (KeyError, ValueError) as exc:
            raise ValueError("quiet_hours.timezone must be an IANA timezone") from exc
        return cls(values[0], values[1], zone, settings["enable"])

    def active(self, now=None):
        if not self.enable:
            return False
        now = now or datetime.now(self.timezone)
        if now.tzinfo is None:
            raise ValueError("Quiet-hours clock must include a timezone")
        local = now.astimezone(self.timezone).time()
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end
