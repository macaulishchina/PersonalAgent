"""Attention time belongs to accepted interaction, never raw VAD or silent RTP."""

from dataclasses import dataclass
import time


@dataclass
class Dialogue:
    follow_up_seconds: float = 8
    continuous: bool = False
    user_at: float = 0
    reply_at: float = 0

    def wake(self):
        self.continuous = False
        self.user_at = time.monotonic()
        self.reply_at = 0

    def user(self):
        self.user_at = time.monotonic()
        self.reply_at = 0

    def replied(self):
        self.reply_at = time.monotonic()

    def expired(self, *, busy, playing, last_output, timeout, now=None):
        now = time.monotonic() if now is None else now
        # Even a stuck execution has a bounded listening lifetime. The work is
        # preserved when attention ends; receiving room speech alone cannot renew it.
        if busy:
            return now - self.user_at >= timeout
        if playing:
            return False
        if self.reply_at and not self.continuous:
            return now - max(self.reply_at, last_output or 0) >= self.follow_up_seconds
        return now - max(self.user_at, self.reply_at, last_output or 0) >= timeout
