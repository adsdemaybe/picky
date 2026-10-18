"""Replenish suggestions ("you bought this 12 days ago"): the Cart Whisperer replenish generator, minimal.

Deterministic, no LLM. Purchase history = ``data/fixtures/replenish.yaml`` (dates RELATIVE to now, so the demo always
reads the same) plus purchases recorded at runtime (``record_purchase`` after a paid trip). Per SKU with >= 2
purchases: cadence = mean gap between purchases (rounded, >= 1 day); last_bought_days = whole days since the last
purchase; overdue_days = last_bought_days - cadence. Cards: overdue_days >= -due_window_days, sorted by overdue_days
desc, then overdue / cadence desc, then SKU. Price / stock / shelf come from the catalog at call time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from contracts.catalog import Product, StoreOffer
from contracts.common import Sku, StoreId, UserId
from contracts.profile import PurchaseRecord, ReplenishSuggestion
from utils.log import get_logger

log = get_logger(__name__)

REPLENISH_FILE = "fixtures/replenish.yaml"
DEFAULT_DUE_WINDOW_DAYS = 3
DEFAULT_LIMIT = 8
DAY_S = 86400.0


class HistoryEntry(BaseModel):
    days_ago: list[int] = Field(min_length=1, description="Purchase dates as days before now (any order).")
    qty: int = Field(1, ge=1)


class ReplenishUser(BaseModel):
    store_id: StoreId = "store_ave"
    due_window_days: int = Field(DEFAULT_DUE_WINDOW_DAYS, ge=0)
    purchases: dict[Sku, HistoryEntry] = Field(default_factory=dict)


class ReplenishFile(BaseModel):
    schema_version: int = 1
    users: dict[UserId, ReplenishUser] = Field(default_factory=dict)


def load_replenish(data_dir: Path) -> ReplenishFile:
    """Parse ``<data_dir>/fixtures/replenish.yaml``; a missing / broken file -> no history (logged)."""
    path = Path(data_dir) / REPLENISH_FILE
    if not path.exists():
        return ReplenishFile()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        return ReplenishFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - demo data must never break the recommender
        log.warning("replenish fixture %s unreadable (%s); no suggestions", path, exc)
        return ReplenishFile()


@dataclass(frozen=True)
class Cadence:
    sku: Sku
    last_at: datetime
    last_days: int
    cadence_days: int
    overdue_days: int
    times: int
    usual_qty: int

    @property
    def urgency(self) -> float:
        return self.overdue_days / self.cadence_days


def cadence_of(sku: Sku, buys: Iterable[tuple[datetime, int]], now: datetime) -> Cadence | None:
    """(ts, qty) purchases of one SKU -> its cadence; None with fewer than 2 purchases (no own rhythm yet)."""
    rows = sorted(buys, key=lambda b: b[0])
    if len(rows) < 2:
        return None
    gaps = [(b[0] - a[0]).total_seconds() / DAY_S for a, b in zip(rows, rows[1:], strict=False)]
    cadence = max(1, round(sum(gaps) / len(gaps)))
    last_at = rows[-1][0]
    last_days = max(0, int((now - last_at).total_seconds() // DAY_S))
    qtys = sorted(q for _, q in rows)
    usual = qtys[len(qtys) // 2]
    return Cadence(sku, last_at, last_days, cadence, last_days - cadence, len(rows), max(1, usual))


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def reason_for(c: Cadence) -> str:
    ago = "today" if c.last_days == 0 else f"{_days(c.last_days)} ago"
    if c.overdue_days > 0:
        return f"You bought this {ago} — you usually buy it every {_days(c.cadence_days)}."
    if c.overdue_days == 0:
        return f"You bought this {ago} — right on your usual {c.cadence_days}-day schedule."
    return (
        f"You bought this {ago} — you usually buy it every {_days(c.cadence_days)}, "
        f"so you'll likely need it in {_days(-c.overdue_days)}."
    )


def due_cadences(
    user: ReplenishUser | None,
    session: list[PurchaseRecord],
    now: datetime,
    *,
    include_all: bool = False,
) -> list[Cadence]:
    """Every SKU's cadence (fixture + runtime purchases), filtered to the due ones and sorted (most overdue first)."""
    buys: dict[Sku, list[tuple[datetime, int]]] = {}
    if user is not None:
        for sku, h in user.purchases.items():
            buys.setdefault(sku, []).extend((now - timedelta(days=d), h.qty) for d in h.days_ago)
    for r in session:
        buys.setdefault(r.sku, []).append((min(r.ts, now), r.qty))
    window = user.due_window_days if user is not None else DEFAULT_DUE_WINDOW_DAYS
    out = [c for sku, rows in buys.items() if (c := cadence_of(sku, rows, now)) is not None]
    if not include_all:
        out = [c for c in out if c.overdue_days >= -window]
    out.sort(key=lambda c: (-c.overdue_days, -c.urgency, c.sku))
    return out


def shelf_label(offer: StoreOffer | None) -> tuple[str | None, str | None, int | None]:
    """(aisle_label 'Aisle 3 · shelf 2', slot_id, level) from an in-store offer's location."""
    loc = offer.location if offer is not None else None
    if loc is None:
        return None, None, None
    aisle = loc.aisle_label or loc.aisle_id or loc.fixture_id
    return f"{aisle} · shelf {loc.level + 1}", loc.slot_id, loc.level


def build_card(
    user_id: UserId,
    c: Cadence,
    product: Product | None,
    offer: StoreOffer | None,
    store_id: StoreId,
) -> ReplenishSuggestion:
    aisle, slot_id, level = shelf_label(offer)
    price = offer.price_cents if offer is not None else (product.price_cents if product is not None else 0)
    signals: dict[str, Any] = {
        "last_bought_days": c.last_days,
        "cadence_days": c.cadence_days,
        "overdue_days": c.overdue_days,
        "times_bought": c.times,
        "source": "own purchase history",
    }
    return ReplenishSuggestion(
        suggestion_id=f"rep-{user_id}-{c.sku}",
        user_id=user_id,
        sku=c.sku,
        name=product.name if product is not None else c.sku,
        brand=product.brand if product is not None else None,
        size=product.size if product is not None else None,
        category=product.category if product is not None else None,
        image_url=f"/api/products/{c.sku}/image.jpg",
        label_color=product.asset.label_color if product is not None else None,
        price_cents=price,
        store_id=store_id,
        in_stock=bool(offer is not None and offer.qty > 0),
        stock_qty=offer.qty if offer is not None else 0,
        aisle_label=aisle,
        slot_id=slot_id,
        shelf_level=level,
        last_bought_at=c.last_at,
        last_bought_days=c.last_days,
        cadence_days=c.cadence_days,
        overdue_days=c.overdue_days,
        times_bought=c.times,
        suggested_qty=c.usual_qty,
        reason=reason_for(c),
        signals=signals,
    )


__all__ = [
    "Cadence",
    "ReplenishFile",
    "ReplenishUser",
    "build_card",
    "cadence_of",
    "due_cadences",
    "load_replenish",
    "reason_for",
    "shelf_label",
]
