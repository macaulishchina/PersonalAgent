"""Stable, sanitized errors. Provider response bodies never cross this boundary."""


class MediaError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)
