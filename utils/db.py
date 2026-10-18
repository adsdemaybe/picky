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

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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


# ---- SQLite (stdlib, base deps): small per-feature stores that must work in the fakes / make demo ------------------
def sqlite_path(path: str) -> str:
    """``':memory:'`` stays; a relative path resolves against the repo root (parent dirs created)."""
    if path == ":memory:" or path.startswith("file:"):
        return path
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = get_settings().repo_root / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def sqlite_connect(path: str) -> sqlite3.Connection:
    """One SQLite connection (``check_same_thread=False``: callers serialize access and run queries off the event
    loop with ``asyncio.to_thread``); rows as ``sqlite3.Row``; WAL for file databases."""
    conn = sqlite3.connect(sqlite_path(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        try:
            conn.execute("pragma journal_mode=wal")
        except sqlite3.DatabaseError:
            pass
    return conn
