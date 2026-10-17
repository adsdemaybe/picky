"""Canonical JSON + SHA-256 + HMAC-SHA256 for mandates, cart hashes and receipts.

Canonical JSON = sorted keys, no whitespace, UTF-8, pydantic models dumped in JSON mode. The same logical object
always hashes the same regardless of key order.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import enum
import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def _default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, _dt.datetime | _dt.date | _dt.time):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, set | frozenset):
        return sorted(o, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    if isinstance(o, Decimal | Path):
        return str(o)
    if isinstance(o, bytes):
        return base64.b64encode(o).decode()
    raise TypeError(f"not canonical-JSON serializable: {type(o).__name__}")


def canonical_json(obj: Any, *, exclude: set[str] | frozenset[str] = frozenset()) -> str:
    """Deterministic JSON string. ``exclude`` drops top-level keys (e.g. ``{"signature"}``) before encoding."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump(mode="json")
    if exclude and isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in exclude}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def canonical_bytes(obj: Any, *, exclude: set[str] | frozenset[str] = frozenset()) -> bytes:
    return canonical_json(obj, exclude=exclude).encode("utf-8")


def sha256_hex(obj: Any, *, exclude: set[str] | frozenset[str] = frozenset()) -> str:
    """SHA-256 hex digest of the canonical JSON of ``obj`` (``bytes``/``str`` are hashed as-is)."""
    if isinstance(obj, bytes):
        data = obj
    elif isinstance(obj, str):
        data = obj.encode("utf-8")
    else:
        data = canonical_bytes(obj, exclude=exclude)
    return hashlib.sha256(data).hexdigest()


def _key(key: str | bytes | None) -> bytes:
    if key is None:
        from utils.config import get_settings

        key = get_settings().picky_signing_key
    return key if isinstance(key, bytes) else key.encode("utf-8")


def sign(obj: Any, key: str | bytes | None = None, *, exclude: set[str] | frozenset[str] = frozenset()) -> str:
    """HMAC-SHA256 hex over canonical JSON. ``key`` defaults to ``PICKY_SIGNING_KEY``."""
    return hmac.new(_key(key), canonical_bytes(obj, exclude=exclude), hashlib.sha256).hexdigest()


def verify(
    obj: Any, signature: str, key: str | bytes | None = None, *, exclude: set[str] | frozenset[str] = frozenset()
) -> bool:
    """Constant-time check of ``signature`` against :func:`sign`."""
    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign(obj, key, exclude=exclude), signature)
