"""Verifier / evidence configuration, read from ``utils.config.settings`` only (CLAUDE.md section 7).

Env vars (``contracts.services.LOOP_ENV_VARS``):
- ``PERCEPTRON_API_KEY``: Perceptron key; unset = ``create_verifier`` raises and the gateway boots the fake verifier.
- ``PERCEPTRON_MODEL``: default ``perceptron-mk1.5`` (fallback ``perceptron-mk1``).
- ``PERCEPTRON_BASE_URL``: default ``https://api.perceptron.inc/v1``.
- ``PICKY_VISION_PROVIDER``: ``perceptron`` (default) | ``fake`` (the sidecar provider: the real verifier code with
  deterministic vision, no key) | ``gemini`` / ``claude`` (optional VLM fallback, not built: raises -> fake verifier).
- ``PICKY_VERIFIER_MODE``: ``decide`` | ``shadow`` | ``gt_only``; blank = per store backend (shadow on the 2D fake,
  decide on Isaac).
- ``PICKY_VISION_CALL_TIMEOUT_S``: cap per provider call (default 4 s; also bounded by the request's deadline).

Fields missing from ``Settings`` read as their defaults (so this arm runs whatever the utils PR has landed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.verify import DEFAULT_VISION_CALL_TIMEOUT_S, VerifierMode
from utils.config import get_settings
from utils.log import get_logger

from .vision.perceptron import DEFAULT_BASE_URL, DEFAULT_MODEL

log = get_logger(__name__)

VERIFIER_MODES: tuple[VerifierMode, ...] = ("decide", "shadow", "gt_only")
VISION_PROVIDERS = ("perceptron", "fake", "gemini", "claude")


def _setting(name: str, default: Any) -> Any:
    value = getattr(get_settings(), name, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return value.strip() if isinstance(value, str) else value


@dataclass(frozen=True)
class VerifierConfig:
    perceptron_api_key: str
    perceptron_model: str
    perceptron_base_url: str
    vision_provider: str
    verifier_mode: VerifierMode | None
    call_timeout_s: float


def verifier_config() -> VerifierConfig:
    mode = str(_setting("picky_verifier_mode", "")).lower() or None
    if mode is not None and mode not in VERIFIER_MODES:
        log.warning("PICKY_VERIFIER_MODE=%r is not one of %s; using the per-backend default", mode, VERIFIER_MODES)
        mode = None
    provider = str(_setting("picky_vision_provider", "perceptron")).lower()
    try:
        timeout = float(_setting("picky_vision_call_timeout_s", DEFAULT_VISION_CALL_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = DEFAULT_VISION_CALL_TIMEOUT_S
    return VerifierConfig(
        perceptron_api_key=str(_setting("perceptron_api_key", "")),
        perceptron_model=str(_setting("perceptron_model", DEFAULT_MODEL)),
        perceptron_base_url=str(_setting("perceptron_base_url", DEFAULT_BASE_URL)),
        vision_provider=provider,
        verifier_mode=mode,  # type: ignore[arg-type]
        call_timeout_s=max(0.5, timeout),
    )


__all__ = ["VERIFIER_MODES", "VISION_PROVIDERS", "VerifierConfig", "verifier_config"]
