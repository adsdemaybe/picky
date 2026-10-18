"""Shared helpers for the loop tests (plain functions driving coroutines with asyncio.run)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

TACO_TEXT = "tacos for 6, under $40, one vegetarian"
ROBOT, STORE = "rover-ave-1", "store_ave"


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)
