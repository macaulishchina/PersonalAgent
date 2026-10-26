"""Shared password policy for operator-facing web entrances."""

MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 256


def validate_password(value):
    if (
        not isinstance(value, str)
        or not MIN_PASSWORD_LENGTH <= len(value) <= MAX_PASSWORD_LENGTH
        or value != value.strip()
        or any(c in value for c in ("\n", "\r", "\x00"))
    ):
        raise ValueError(
            f"密码须为 {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符，"
            "首尾不能有空白，不能包含换行或空字符。"
        )
