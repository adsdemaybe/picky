"""Purchase-history database (judges' demo, contracts 0.2.4): every user's purchases in one SQLite table.

    purchases(purchase_id PK, user_id, trip_id, sku, name, qty, price_cents, store_id, ts, rating, photo_urls, source)

- SQLite via ``utils.db.sqlite_connect`` (stdlib, base deps: works in the fakes and ``make demo``); the file is
  ``PICKY_PURCHASES_DB`` (default ``var/purchases.sqlite`` under the repo root, ``:memory:`` per process).
- Seeded from ``data/fixtures/purchase_history.yaml`` (months of cadence-based history for maya / sam / jordan, real
  sim SKUs, shelf prices) RELATIVE to now; the seed (``source='seed'`` rows) is rebuilt when it is older than
  ``RESEED_AFTER_S`` so "12 days ago" stays true on demo day. Checkout rows (``source='checkout'``) are never touched.
- Every completed checkout appends its lines (``add``), with the robot's verification photos (``photo_urls``).
All queries run off the event loop (``asyncio.to_thread``) behind one lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from contracts.common import Sku, StoreId, UserId, utcnow
from contracts.profile import PurchaseRecord
from utils.db import sqlite_connect
from utils.log import get_logger

log = get_logger(__name__)

HISTORY_FILE = "fixtures/purchase_history.yaml"
RESEED_AFTER_S = 6 * 3600.0
SCHEMA = """
create table if not exists purchases (
    purchase_id text primary key,
    user_id text not null,
    trip_id text,
    sku text not null,
    name text,
    qty integer not null,
    price_cents integer not null,
    store_id text,
    ts text not null,
    rating integer,
    photo_urls text not null default '[]',
    source text not null default 'checkout'
);
create index if not exists purchases_user_ts on purchases (user_id, ts);
create table if not exists purchases_meta (key text primary key, value text not null);
"""


class SeedItem(BaseModel):
    every: int | None = Field(None, ge=1, description="Days between purchases.")
    last: int | None = Field(None, ge=0, description="Days since the last purchase.")
    qty: int = Field(1, ge=1)
    rating: int | None = Field(None, ge=1, le=5)
    with_: Sku | None = Field(None, alias="with", description="Bought on this anchor SKU's trips.")
    with_every: int = Field(1, ge=1)
    with_skip: int = Field(0, ge=0)


class SeedUser(BaseModel):
    store_id: StoreId = "store_ave"
    months: int = Field(4, ge=1, le=24)
    items: dict[Sku, SeedItem] = Field(default_factory=dict)


class SeedFile(BaseModel):
    schema_version: int = 1
    users: dict[UserId, SeedUser] = Field(default_factory=dict)


def load_seed(data_dir: Path) -> SeedFile:
    path = Path(data_dir) / HISTORY_FILE
    if not path.exists():
        return SeedFile()
    try:
        return SeedFile.model_validate(yaml.safe_load(path.read_text()) or {})
    except Exception as exc:  # noqa: BLE001 - demo data never breaks the recommender
        log.warning("purchase history seed %s unreadable (%s)", path, exc)
        return SeedFile()


def seed_days(seed: SeedUser) -> dict[Sku, list[int]]:
    """Per SKU the purchase days (days ago, newest first): cadence items every ``every`` days from ``last`` back
    ``months``; ``with`` items on every ``with_every``-th trip of their anchor after skipping the newest
    ``with_skip``."""
    horizon = seed.months * 30
    out: dict[Sku, list[int]] = {}
    for sku, it in seed.items.items():
        if it.every and it.last is not None:
            out[sku] = list(range(it.last, horizon + 1, it.every))
    for sku, it in seed.items.items():
        if it.with_ and it.with_ in out:
            anchor = out[it.with_][it.with_skip :]
            out[sku] = anchor[:: it.with_every]
    return out


def _price_of(sku: Sku, store_id: StoreId) -> tuple[int, str | None]:
    from utils.data import load_planogram, load_products

    product = load_products().get(sku)
    base = product.price_cents if product is not None else 0
    try:
        plan = load_planogram(store_id)
        slot = next((s for s in plan.slots if s.sku == sku), None)
        if slot is not None and slot.price_cents is not None:
            base = slot.price_cents
    except Exception:  # noqa: BLE001 - online / unknown store: the product price
        pass
    return base, (product.name if product is not None else None)


def seed_records(seeds: SeedFile, now: datetime) -> list[PurchaseRecord]:
    """The demo history as PurchaseRecords (deterministic for a given ``now``); same-day purchases share a trip."""
    out: list[PurchaseRecord] = []
    for user_id, seed in seeds.users.items():
        for sku, days in seed_days(seed).items():
            item = seed.items[sku]
            price, name = _price_of(sku, seed.store_id)
            for d in days:
                back_h = int(hashlib.sha256(f"{user_id}:{d}".encode()).hexdigest()[:2], 16) % 9
                ts = (now - timedelta(days=d, hours=back_h)).replace(second=0, microsecond=0)  # exactly d days ago
                out.append(
                    PurchaseRecord(
                        purchase_id=f"seed-{user_id}-{sku}-d{d:03d}",
                        user_id=user_id,
                        sku=sku,
                        name=name,
                        qty=item.qty,
                        unit_price_cents=price,
                        store_id=seed.store_id,
                        trip_id=f"seed-{user_id}-d{d:03d}",
                        ts=ts,
                        rating=item.rating if d == days[0] else None,
                        source="seed",
                    )
                )
    out.sort(key=lambda r: (r.user_id, r.ts, r.sku))
    return out


def _row_to_record(row: Any) -> PurchaseRecord:
    return PurchaseRecord(
        purchase_id=row["purchase_id"],
        user_id=row["user_id"],
        trip_id=row["trip_id"],
        sku=row["sku"],
        name=row["name"],
        qty=row["qty"],
        unit_price_cents=row["price_cents"],
        store_id=row["store_id"],
        ts=datetime.fromisoformat(row["ts"]),
        rating=row["rating"],
        photo_urls=json.loads(row["photo_urls"] or "[]"),
        source=row["source"] if row["source"] in ("checkout", "seed") else "checkout",
    )


class PurchaseStore:
    """The purchase-history table. ``path`` = SQLite file (relative = repo root) or ':memory:'."""

    def __init__(self, path: str, *, data_dir: Path | None = None, seed: bool = True) -> None:
        self.path = path
        self.data_dir = data_dir
        self.seed_enabled = seed
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._ready_at = 0.0

    # ---- plumbing ----------------------------------------------------------------------------------------------
    def _db(self) -> Any:
        if self._conn is None:
            self._conn = sqlite_connect(self.path)
            self._conn.executescript(SCHEMA)
        return self._conn

    async def _run(self, fn: Any, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _seed_sync(self, now: datetime) -> int:
        db = self._db()
        row = db.execute("select value from purchases_meta where key='seeded_at'").fetchone()
        if row is not None:
            try:
                age = (now - datetime.fromisoformat(row["value"])).total_seconds()
            except ValueError:
                age = RESEED_AFTER_S + 1
            if age < RESEED_AFTER_S:
                return 0
        if self.data_dir is None:
            from utils import data as data_mod

            self.data_dir = data_mod.data_dir()
        records = seed_records(load_seed(self.data_dir), now)
        with db:
            db.execute("delete from purchases where source='seed'")
            db.executemany(
                "insert or replace into purchases values (?,?,?,?,?,?,?,?,?,?,?,?)", [self._row(r) for r in records]
            )
            db.execute("insert or replace into purchases_meta values ('seeded_at', ?)", (now.isoformat(),))
        log.info("purchase history seeded: %d rows for %d users", len(records), len({r.user_id for r in records}))
        return len(records)

    @staticmethod
    def _row(r: PurchaseRecord) -> tuple[Any, ...]:
        return (
            r.purchase_id,
            r.user_id,
            r.trip_id,
            r.sku,
            r.name,
            r.qty,
            r.unit_price_cents,
            r.store_id,
            r.ts.astimezone(UTC).isoformat(),
            r.rating,
            json.dumps(list(r.photo_urls)),
            r.source,
        )

    async def ensure(self, now: datetime | None = None) -> None:
        """Create the table and (re)seed the demo history when missing / stale (cheap after the first call)."""
        if self.seed_enabled and time.monotonic() - self._ready_at > 60.0:
            await self._run(self._seed_sync, now or utcnow())
            self._ready_at = time.monotonic()
        elif not self.seed_enabled:
            await self._run(self._db)

    # ---- API ---------------------------------------------------------------------------------------------------
    async def add(self, records: Sequence[PurchaseRecord]) -> int:
        """Append purchases (a completed checkout); an existing purchase_id is replaced (idempotent)."""
        await self.ensure()
        rows = [self._row(r) for r in records]

        def write() -> int:
            db = self._db()
            with db:
                db.executemany("insert or replace into purchases values (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            return len(rows)

        return await self._run(write)

    async def history(
        self, user_id: UserId, *, limit: int | None = None, newest_first: bool = False
    ) -> list[PurchaseRecord]:
        await self.ensure()
        order = "desc" if newest_first else "asc"
        sql = f"select * from purchases where user_id=? order by ts {order}, sku"  # noqa: S608 - fixed order words
        args: tuple[Any, ...] = (user_id,)
        if limit is not None:
            sql += " limit ?"
            args = (user_id, int(limit))

        def read() -> list[PurchaseRecord]:
            return [_row_to_record(r) for r in self._db().execute(sql, args).fetchall()]

        return await self._run(read)

    async def users(self) -> list[UserId]:
        await self.ensure()

        def read() -> list[UserId]:
            return [r[0] for r in self._db().execute("select distinct user_id from purchases order by 1").fetchall()]

        return await self._run(read)

    async def add_photos(self, trip_id: str, sku: Sku, urls: Sequence[str]) -> int:
        """Attach verification photo urls to that trip's purchase rows of ``sku`` (rows recorded before the photos)."""
        await self.ensure()

        def write() -> int:
            db = self._db()
            n = 0
            with db:
                for row in db.execute(
                    "select purchase_id, photo_urls from purchases where trip_id=? and sku=?", (trip_id, sku)
                ).fetchall():
                    merged = list(dict.fromkeys([*json.loads(row["photo_urls"] or "[]"), *urls]))
                    db.execute("update purchases set photo_urls=? where purchase_id=?", (json.dumps(merged), row[0]))
                    n += 1
            return n

        return await self._run(write)


__all__ = ["PurchaseStore", "SeedFile", "load_seed", "seed_days", "seed_records"]
