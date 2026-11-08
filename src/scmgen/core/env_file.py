"""Generate secrets and write them into the local .env file (never committed)."""

from __future__ import annotations

import secrets
import string
from pathlib import Path

from dotenv import set_key

_ALPHABET = string.ascii_letters + string.digits
_SPECIALS = "_-."  # safe in .env files, shells and MySQL string literals


def generate_password(length: int = 32) -> str:
    """Random password guaranteed to contain upper, lower, digit and special characters,
    so it satisfies MySQL's validate_password policy (MEDIUM/STRONG) if that's enabled."""
    if length < 12:
        raise ValueError("password length must be at least 12")
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(_SPECIALS),
    ]
    rest = [secrets.choice(_ALPHABET + _SPECIALS) for _ in range(length - len(required))]
    chars = required + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def upsert_env_value(env_file: Path, key: str, value: str) -> None:
    """Set KEY=value in the .env file, replacing an existing line or appending a new one.
    Comments and other keys are preserved. The file is created (mode 600) if missing."""
    if not env_file.exists():
        env_file.touch(mode=0o600)
    set_key(str(env_file), key, value, quote_mode="never")
