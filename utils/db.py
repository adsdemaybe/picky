"""Shared async database access: one engine (one connection pool) per process per URL, from
``settings.database_url`` (``DATABASE_URL``). Every arm that persists (catalog, recommender profiles, trust ledger)
uses this instead of building its own engine.

SQLAlchemy + psycopg come from an arm extra (``uv sync --extra catalog``); imports are lazy, so base deps never need
them and nothing connects until the first query.

Usage::

    from utils.db import connect, session

    async with connect() as conn:                    # Core: await conn.execute(text("select 1"))
        ...
    async with session() as s:                       # ORM: AsyncSession (expire_on_commit=False)
        ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from utils.config import get_settings

POOL_SIZE = 5

_engines: dict[str, Any] = {}


class DatabaseUnavailable(RuntimeError):
    """SQLAlchemy (an arm extra) is not installed."""


def get_engine(url: str | None = None) -> Any:
    """The process-wide ``AsyncEngine`` for ``url`` (default ``settings.database_url``); created on first use."""
    url = url or get_settings().database_url
    engine = _engines.get(url)
    if engine is None:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
        except ImportError as exc:
            raise DatabaseUnavailable("the database needs sqlalchemy + psycopg: uv sync --extra catalog") from exc
        engine = create_async_engine(url, pool_pre_ping=True, pool_size=POOL_SIZE)
        _engines[url] = engine
    return engine


@asynccontextmanager
async def connect(url: str | None = None) -> AsyncIterator[Any]:
    """``AsyncConnection`` from the shared pool (Core / ``text()`` queries)."""
    async with get_engine(url).connect() as conn:
        yield conn


@asynccontextmanager
async def session(url: str | None = None) -> AsyncIterator[Any]:
    """``AsyncSession`` on the shared engine (``expire_on_commit=False``)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(get_engine(url), expire_on_commit=False) as s:
        yield s


async def dispose_all() -> None:
    """Close every pool (gateway shutdown, tests)."""
    engines = list(_engines.values())
    _engines.clear()
    for engine in engines:
        await engine.dispose()


__all__ = ["DatabaseUnavailable", "connect", "dispose_all", "get_engine", "session"]
