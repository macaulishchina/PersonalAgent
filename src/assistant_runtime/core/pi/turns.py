"""Bounded foreground execution receipts; provider prose never spans two turns."""

from dataclasses import dataclass, field


@dataclass
class Turn:
    task_id: str
    progress: str = ""
    result: str = ""
    status: str | None = None
    messages: dict = field(default_factory=dict)

    def message(self, item):
        ident = item["id"]
        if ident in self.messages or self.status is not None:
            return False
        self.messages[ident] = True
        if len(self.messages) > 128:
            del self.messages[next(iter(self.messages))]
        self.progress = item.get("text", "")[:16000]
        if item.get("phase") != "commentary":
            self.result = "\n\n".join(filter(None, (self.result, item.get("text", ""))))
        return True


class Turns:
    def __init__(self):
        self.records = {}

    def start(self, ident, task_id):
        if ident in self.records:
            return None
        record = self.records[ident] = Turn(task_id)
        while len(self.records) > 32:
            old = next((k for k, v in self.records.items() if v.status is not None), None)
            if old is None:
                raise RuntimeError("Too many unfinished foreground turns")
            del self.records[old]
        return record

    def active(self, ident):
        record = self.records.get(ident)
        return record if record and record.status is None else None
