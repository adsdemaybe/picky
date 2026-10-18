"""Stock-management rules shared by the fake and the Postgres catalog (pure functions, base deps only).

The thresholds / status rule live in ``contracts.catalog`` (``stock_status``, ``default_low_threshold``); this module
adds the catalog arm's alert lifecycle (raise on low/out, update silently while the status holds, re-raise on a status
change, clear once back to ok), messages, ordering and actor matching.
"""

from __future__ import annotations

from datetime import datetime

from contracts.catalog import StockAlert, StockLevel, StockMovement, StockReason
from contracts.common import RobotId, Sku, StoreId

STATUS_RANK: dict[str, int] = {"out": 0, "low": 1, "ok": 2}
"""Worst first: out, low, ok."""


def alert_id(store_id: StoreId, sku: Sku) -> str:
    return f"{store_id}:{sku}"


def alert_message(level: StockLevel, status: str) -> str:
    """Staff-facing text, e.g. 'Casa Verde Mild Salsa is out of stock (Aisle 3)'."""
    where = f" ({level.aisle_label})" if level.aisle_label else ""
    if status == "out":
        return f"{level.name} is out of stock{where}"
    if status == "low":
        return f"{level.name} is running low: {level.qty} left{where}"
    return f"{level.name} is back in stock: {level.qty} on the shelf{where}"


def next_alert(
    current: StockAlert | None,
    level: StockLevel,
    *,
    trigger: StockReason | None,
    actor: str | None,
    now: datetime,
) -> tuple[StockAlert | None, bool]:
    """THE alert lifecycle for one store+sku after a change. Returns (alert after the change, should_publish).

    - low/out and an active alert with the same status -> same alert with fresh qty fields, not published;
    - low/out otherwise (new, or low <-> out) -> (re)raised alert, published;
    - ok with an active alert -> cleared (``active=False``, ``cleared_at``), published;
    - ok without an active alert -> (None, False).
    """
    status = level.status
    live = {
        "qty": level.qty,
        "low_threshold": level.low_threshold,
        "capacity": level.capacity,
        "slot_ids": list(level.slot_ids),
        "aisle_label": level.aisle_label,
        "suggested_restock_qty": max(0, level.capacity - level.qty),
    }
    if status in ("low", "out"):
        if current is not None and current.active and current.status == status:
            return current.model_copy(update={**live, "message": alert_message(level, status)}), False
        return (
            StockAlert(
                alert_id=alert_id(level.store_id, level.sku),
                store_id=level.store_id,
                sku=level.sku,
                name=level.name,
                category=level.category,
                unit=level.unit,
                size=level.size,
                image_url=level.image_url,
                status=status,  # type: ignore[arg-type]
                message=alert_message(level, status),
                trigger=trigger,
                actor=actor,
                active=True,
                raised_at=now,
                **live,
            ),
            True,
        )
    if current is not None and current.active:
        update = {
            **live,
            "active": False,
            "cleared_at": now,
            "message": alert_message(level, "ok"),
            "trigger": trigger,
            "actor": actor,
        }
        return current.model_copy(update=update), True
    return None, False


def sort_levels(levels: list[StockLevel]) -> list[StockLevel]:
    return sorted(levels, key=lambda lv: (STATUS_RANK[lv.status], lv.name.lower(), lv.sku))


def sort_alerts(alerts: list[StockAlert]) -> list[StockAlert]:
    return sorted(alerts, key=lambda a: (STATUS_RANK[a.status], a.name.lower(), a.sku))


def actor_matches(movement_actor: str, wanted: str) -> bool:
    """``wanted`` matches exactly, or as a prefix when it ends in ':' ('robot:' matches every robot)."""
    return movement_actor == wanted or (wanted.endswith(":") and movement_actor.startswith(wanted))


def robot_id_from_actor(actor: str | None) -> RobotId | None:
    if actor and actor.startswith("robot:"):
        return actor.split(":", 1)[1] or None
    return None


def matches_query(q: str | None, *fields: str | None) -> bool:
    """Case-insensitive substring of any field (sku / name / brand); empty query matches everything."""
    needle = (q or "").strip().lower()
    return not needle or any(needle in (f or "").lower() for f in fields)


def history_filter(
    movements: list[StockMovement],
    *,
    sku: Sku | None,
    before: datetime | None,
    reason: StockReason | None,
    actor: str | None,
    limit: int,
) -> list[StockMovement]:
    """Newest first over an append-ordered list (ties keep insertion order, newest first)."""
    out: list[StockMovement] = []
    for mv in reversed(movements):
        if sku is not None and mv.sku != sku:
            continue
        if before is not None and not mv.ts < before:
            continue
        if reason is not None and mv.reason != reason:
            continue
        if actor is not None and not actor_matches(mv.actor, actor):
            continue
        out.append(mv.model_copy())
        if len(out) >= max(0, limit):
            break
    return out
