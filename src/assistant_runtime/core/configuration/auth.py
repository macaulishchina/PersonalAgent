"""Independent local-owner authentication for the configuration console."""

from assistant_runtime.api.system import system
from assistant_runtime.api.passwords import MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH, validate_password

import hashlib
import hmac
from pathlib import Path
import os
import secrets
import time


class PasswordPolicyError(ValueError):
    """Only fixed, public validation messages; never include submitted credentials."""


class AdminAccess:
    MIN_PASSWORD_LENGTH = MIN_PASSWORD_LENGTH
    MAX_PASSWORD_LENGTH = MAX_PASSWORD_LENGTH

    @classmethod
    def password_policy(cls):
        return {"min_length": cls.MIN_PASSWORD_LENGTH, "max_length": cls.MAX_PASSWORD_LENGTH}

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Administrator credential cannot be a symlink")
        if not self.path.exists():
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
        system.private(self.path)
        token = self.path.read_text(encoding="utf-8").strip()
        if not self.MIN_PASSWORD_LENGTH <= len(token) <= self.MAX_PASSWORD_LENGTH:
            raise ValueError("Administrator credential is invalid; reset it locally")
        self.digest = hashlib.sha256(token.encode()).digest()
        self.sessions = {}
        self.attempts = {}

    def refresh(self):
        if self.path.is_symlink():
            raise ValueError("Administrator credential cannot be a symlink")
        token = self.path.read_text(encoding="utf-8").strip()
        if not self.MIN_PASSWORD_LENGTH <= len(token) <= self.MAX_PASSWORD_LENGTH:
            self.sessions.clear()
            raise ValueError("Administrator credential is invalid; reset it locally")
        digest = hashlib.sha256(token.encode()).digest()
        if digest != self.digest:
            self.digest = digest
            self.sessions.clear()

    def verify(self, token, peer):
        self.refresh()
        now = time.monotonic()
        self.attempts = {k: v for k, v in self.attempts.items() if now - v[1] < 60}
        count, start = self.attempts.get(peer, (0, now))
        if count >= 10 or (peer not in self.attempts and len(self.attempts) >= 1024):
            raise PermissionError("登录尝试过于频繁，请稍后重试")
        self.attempts[peer] = count + 1, start
        if not isinstance(token, str) or not hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(), self.digest
        ):
            raise PermissionError("管理员口令不正确")

    def login(self, token, peer):
        self.verify(token, peer)
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        if len(self.sessions) >= 64:
            raise PermissionError("登录设备过多")
        session = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(session.encode()).digest()] = now + 12 * 3600
        return session

    def change_password(self, current_password, new_password, confirmation, peer):
        self.verify(current_password, peer)
        try:
            validate_password(new_password)
        except ValueError as exc:
            raise PasswordPolicyError(str(exc)) from exc
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            new_password.encode(), confirmation.encode()
        ):
            raise PasswordPolicyError("两次输入的新密码不一致。")
        from .files import atomic

        atomic(self.path, (new_password + "\n").encode())
        self.digest = hashlib.sha256(new_password.encode()).digest()
        self.sessions.clear()

    def valid(self, session):
        self.refresh()
        return bool(
            session and self.sessions.get(hashlib.sha256(session.encode()).digest(), 0) > time.monotonic()
        )

    def logout(self, session):
        self.sessions.pop(hashlib.sha256(session.encode()).digest(), None)
