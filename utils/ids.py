"""Short, url-safe ids: ``new_id("trip") -> "trip_k3f9x2ab"``.

Deterministic for tests: ``seed_ids(42)`` (or env ``PICKY_ID_SEED``) makes the sequence reproducible;
``seed_ids(None)`` restores secure randomness.
"""

from __future__ import annotations

import os
import random
import re
import secrets
import threading

ALPHABET = "0123456789abcdefghijkmnpqrstuvwxyz"  # lowercase, no 'l'/'o' to avoid confusion
DEFAULT_LEN = 8
_ID_RE = re.compile(r"^[a-z][a-z0-9]*_[" + ALPHABET + r"]+$")

_lock = threading.Lock()
_rng: random.Random | None = None
_env_seed = os.environ.get("PICKY_ID_SEED")
if _env_seed:
    _rng = random.Random(int(_env_seed) if _env_seed.isdigit() else _env_seed)


def seed_ids(seed: int | str | None) -> None:
    """Make ``new_id`` deterministic (``seed`` given) or random again (``None``)."""
    global _rng
    with _lock:
        _rng = None if seed is None else random.Random(seed)


def _token(n: int) -> str:
    with _lock:
        if _rng is not None:
            return "".join(_rng.choice(ALPHABET) for _ in range(n))
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def new_id(prefix: str, n: int = DEFAULT_LEN) -> str:
    prefix = prefix.strip().lower().rstrip("_")
    if not prefix or not re.fullmatch(r"[a-z][a-z0-9]*", prefix):
        raise ValueError(f"id prefix must be lowercase alphanumeric starting with a letter, got {prefix!r}")
    return f"{prefix}_{_token(n)}"


def is_id(value: str, prefix: str | None = None) -> bool:
    if not isinstance(value, str) or not _ID_RE.match(value):
        return False
    return prefix is None or value.split("_", 1)[0] == prefix


class IdFactory:
    """Independent deterministic id source (handy in fakes that must not disturb the global sequence)."""

    def __init__(self, seed: int | str = 0, n: int = DEFAULT_LEN) -> None:
        self._rng = random.Random(seed)
        self._n = n

    def __call__(self, prefix: str) -> str:
        return f"{prefix}_{''.join(self._rng.choice(ALPHABET) for _ in range(self._n))}"
