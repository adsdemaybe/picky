"""Logging: ``log = get_logger(__name__)`` everywhere.

Format: ``12:01:02.345 INFO  [routing] arms.routing.service: message``. Level from env ``PICKY_LOG_LEVEL``
(default INFO). Only picky's own loggers get that level; third-party libraries stay at WARNING via the root logger.
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(arm)s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
_HANDLER_NAME = "picky"


def arm_of(name: str) -> str:
    """Map a logger/module name to a short component tag: ``arms.trust.service`` -> ``trust``."""
    parts = name.split(".")
    if parts[0] == "arms" and len(parts) > 1:
        return parts[1]
    return parts[0] or "root"


class _ArmFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "arm"):
            record.arm = arm_of(record.name)
        return True


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time (plays well with pytest capture / redirected stderr)."""

    @property  # type: ignore[override]
    def stream(self):  # noqa: D401
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def log_level() -> int:
    raw = os.environ.get("PICKY_LOG_LEVEL", "INFO").strip().upper()
    if raw.isdigit():
        return int(raw)
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else logging.INFO


def _ensure_handler() -> None:
    root = logging.getLogger()
    if any(getattr(h, "name", None) == _HANDLER_NAME for h in root.handlers):
        return
    handler = _StderrHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    handler.addFilter(_ArmFilter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger with picky's format; level from ``PICKY_LOG_LEVEL``."""
    _ensure_handler()
    logger = logging.getLogger(name)
    logger.setLevel(log_level())
    return logger
