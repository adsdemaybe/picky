"""Postgres-backed CatalogService (real impl; ``PICKY_REAL_ARMS=catalog``). Extra: ``uv sync --extra catalog``.

Tables: contracts/db/schema.sql, filled by ``python -m arms.catalog.seed --schema --apply`` (``make seed``).
SQLAlchemy async + psycopg 3 (``DATABASE_URL=postgresql+psycopg://...``). The engine is created lazily on first call,
never in the constructor. Read paths and ``record_pick`` are real; the rest raise NotImplementedError with a TODO
list. Until then run the fake (default) — it implements every method over the same data.

Stock management: ``stock_levels`` / ``stock_alerts`` / ``stock_history`` / ``list_categories`` read the v0.2 tables
(``stock_movements``, ``stock_alerts``, ``categories``; seeded by ``seed.py``) and ``record_pick`` writes a movement +
updates the alert with the same lifecycle as the fake (``arms/catalog/stock.py``). Untested against a live DB this
weekend (no Docker on the dev box); the fake is the demo path.

TODO (numbered; maps to the catalog scope in CLAUDE.md §3 / arms/catalog/README.md):
  1. search: SQL prefilter (category, tags @>, dietary @>, ILIKE on name/tags) + the fake's ``text_score`` ranking;
     later pgvector cosine on product_embeddings (EMBED_DIM=384) blended in.            [P0 multi-store search]
  2. get_offers: join slots+inventory+lots+deals per store and online_items for 'online' (mirror the fake's
     ``_store_offer`` / ``_online_offer`` exactly: price = active deal price else slot override else base).  [P0]
  3. apply_scan: INSERT scans row; UPDATE inventory qty per ``count_is_exact`` rule; lots.freshness from
     ``lot_freshness``; slots.shelf_tag_cents on mispricing; pose -> nearest slot when slot_id is None.      [P0]
  4. merchant_dashboard: OOS from inventory, mispricing from slots.shelf_tag_cents, near-expiry from lots,
     heatmap from scans + a picks table/column (add ``picks`` counter via a contracts/db PR).             [P1]
  5. Persist pick counts for the heatmap (schema has no picks table yet -> contracts PR or JSONB on inventory). [P1]
  6. Open Food Facts enrichment (DuckDB over the parquet dump; never live API calls in the demo).           [P2]
  7. category_products: needs get_offers (TODO 2); products WHERE category = :c OR subcategory = :c.       [P1]
  8. restock / adjust_stock: same validation as the fake (free capacity, set_qty single slot), lots + movement +
     alert in one transaction, then publish + robot.sync_stock. apply_scan (TODO 3) must also write movements. [P1]
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from arms.catalog.stock import (
    matches_query,
    next_alert,
    robot_id_from_actor,
    sort_alerts,
    sort_levels,
)
from arms.catalog.taxonomy import UNCATEGORIZED, category_matches, merge_taxonomy, natural_key
from contracts.catalog import (
    CategoryInfo,
    Deal,
    Lot,
    MerchantDashboard,
    NearExpiryItem,
    Product,
    ProductLocation,
    RestockRequest,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StockAdjustment,
    StockAlert,
    StockChangeResult,
    StockLevel,
    StockMovement,
    StockReason,
    StockStatus,
    StoreInfo,
    StoreOffer,
    stock_status,
)
from contracts.common import (
    ONLINE_STORE_ID,
    CategoryId,
    LotId,
    Point3,
    Pose2D,
    RobotId,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    TripId,
    utcnow,
)
from contracts.events import make_event
from contracts.perception import ScanReport
from contracts.planogram import CategoryDef, Planogram
from contracts.services import Services
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

NEAR_EXPIRY_DAYS = 3

# One row per SKU stocked in a store (aggregated over its slots); deal price applied like the fake.
_LEVELS_SQL = """
SELECT s.store_id, s.sku, p.name, p.brand, p.category, p.subcategory, p.unit, p.size, p.image_url,
       SUM(COALESCE(i.qty, 0))::int AS qty,
       SUM(s.capacity)::int AS capacity,
       SUM(COALESCE(s.low_threshold, GREATEST(1, CEIL(s.capacity * 0.25))::int))::int AS low_threshold,
       ARRAY_AGG(s.slot_id ORDER BY s.slot_id) AS slot_ids,
       (ARRAY_AGG(s.aisle_label ORDER BY s.slot_id))[1] AS aisle_label,
       COALESCE(
         (SELECT MIN(d.price_cents) FROM deals d WHERE d.store_id = s.store_id AND d.sku = s.sku
            AND (d.valid_until IS NULL OR d.valid_until >= now())),
         MIN(COALESCE(s.price_cents, p.price_cents))) AS price_cents,
       (SELECT COALESCE(SUM(l.qty), 0)::int FROM lots l WHERE l.store_id = s.store_id AND l.sku = s.sku
          AND l.qty > 0 AND l.expiry BETWEEN :d0 AND :d1) AS near_expiry_qty,
       MAX(i.last_scan_at) AS last_scan_at,
       (SELECT MAX(m.ts) FROM stock_movements m WHERE m.store_id = s.store_id AND m.sku = s.sku) AS last_movement_at
FROM slots s JOIN products p ON p.sku = s.sku LEFT JOIN inventory i USING (store_id, slot_id)
WHERE s.store_id = :sid AND (CAST(:sku AS TEXT) IS NULL OR s.sku = :sku)
GROUP BY s.store_id, s.sku, p.name, p.brand, p.category, p.subcategory, p.unit, p.size, p.image_url
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


class PostgresCatalogService:
    """Satisfies ``contracts.services.CatalogService`` against Postgres."""

    def __init__(self, services: Services | None = None, database_url: str | None = None) -> None:
        self.services = services
        self._database_url = database_url
        self._engine: Any = None

    # ------------------------------------------------------------------------------------------------- db plumbing
    def _get_engine(self) -> Any:
        if self._engine is None:
            from utils.config import get_settings
            from utils.db import get_engine  # shared pool (utils/db.py); raises DatabaseUnavailable without the extra

            url = self._database_url or get_settings().database_url
            self._engine = get_engine(url)
            log.info("catalog: postgres engine -> %s", url.split("@")[-1])
        return self._engine

    async def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._get_engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r._mapping) for r in result]

    async def _exec(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        async with self._get_engine().begin() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r._mapping) for r in result] if result.returns_rows else []

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------------------------------------------- reads
    async def list_stores(self) -> list[StoreInfo]:
        rows = await self._rows(
            "SELECT store_id, name, kind, address, width_m, depth_m, planogram IS NOT NULL AS has_planogram, "
            "shipping_cents, free_shipping_over_cents, default_eta_hours FROM stores "
            "ORDER BY kind DESC, store_id"
        )
        return [StoreInfo(**r) for r in rows]

    async def get_planogram(self, store_id: StoreId) -> Planogram:
        rows = await self._rows("SELECT planogram FROM stores WHERE store_id = :sid", sid=store_id)
        if not rows or rows[0]["planogram"] is None:
            raise KeyError(f"no planogram for store {store_id!r}")
        plan = Planogram.model_validate(_json(rows[0]["planogram"]))
        # Overlay live stock / lots / shelf tags.
        inv = {
            r["slot_id"]: r
            for r in await self._rows(
                "SELECT i.slot_id, i.qty, s.shelf_tag_cents FROM inventory i "
                "JOIN slots s USING (store_id, slot_id) WHERE i.store_id = :sid",
                sid=store_id,
            )
        }
        lots: dict[str, list[Lot]] = {}
        for r in await self._rows(
            "SELECT slot_id, lot_id, qty, expiry, freshness FROM lots WHERE store_id = :sid ORDER BY slot_id, lot_id",
            sid=store_id,
        ):
            lots.setdefault(r["slot_id"], []).append(
                Lot(lot_id=r["lot_id"], qty=r["qty"], expiry=r["expiry"], freshness=r["freshness"])
            )
        for slot in plan.slots:
            if slot.slot_id in inv:
                slot.qty = inv[slot.slot_id]["qty"]
                slot.shelf_tag_cents = inv[slot.slot_id]["shelf_tag_cents"]
            if slot.slot_id in lots:
                slot.lots = lots[slot.slot_id]
        return plan

    async def get_product(self, sku: Sku) -> Product | None:
        rows = await self._rows("SELECT * FROM products WHERE sku = :sku", sku=sku)
        if not rows:
            return None
        row = rows[0]
        row["asset"] = _json(row.get("asset")) or {}
        return Product.model_validate(row)

    async def get_locations(self, sku: Sku, store_id: StoreId | None = None) -> list[ProductLocation]:
        rows = await self._rows(
            "SELECT s.*, COALESCE(i.qty, 0) AS qty FROM slots s "
            "LEFT JOIN inventory i USING (store_id, slot_id) "
            "WHERE s.sku = :sku AND (CAST(:sid AS TEXT) IS NULL OR s.store_id = :sid) ORDER BY s.store_id, s.slot_id",
            sku=sku,
            sid=store_id,
        )
        return [
            ProductLocation(
                store_id=r["store_id"],
                sku=r["sku"],
                slot_id=r["slot_id"],
                aisle_id=r["aisle_id"],
                aisle_label=r["aisle_label"],
                fixture_id=r["fixture_id"],
                level=r["level"],
                index=r["idx"],
                item_position=Point3(x=r["item_x"], y=r["item_y"], z=r["item_z"]),
                approach_pose=Pose2D(x=r["approach_x"], y=r["approach_y"], theta=r["approach_theta"]),
                qty=r["qty"],
            )
            for r in rows
        ]

    async def get_deals(self, store_id: StoreId) -> list[Deal]:
        rows = await self._rows(
            "SELECT deal_id, store_id, sku, kind, price_cents, original_cents, percent_off, reason, lot_id, "
            "valid_until FROM deals WHERE store_id = :sid AND (valid_until IS NULL OR valid_until >= now()) "
            "ORDER BY deal_id",
            sid=store_id,
        )
        return [Deal(**r) for r in rows]

    async def get_near_expiry(self, store_id: StoreId, within_days: int = 3) -> list[NearExpiryItem]:
        if store_id == ONLINE_STORE_ID:
            return []
        today = utcnow().date()
        rows = await self._rows(
            "SELECT l.slot_id, l.sku, l.lot_id, l.qty, l.expiry, l.freshness, "
            "COALESCE(s.price_cents, p.price_cents) AS list_price "
            "FROM lots l JOIN slots s USING (store_id, slot_id) JOIN products p ON p.sku = l.sku "
            "WHERE l.store_id = :sid AND l.qty > 0 AND l.expiry BETWEEN :d0 AND :d1 ORDER BY l.expiry, l.sku",
            sid=store_id,
            d0=today,
            d1=today + timedelta(days=within_days),
        )
        deals = {(d.sku, d.lot_id): d for d in await self.get_deals(store_id)}
        out = []
        for r in rows:
            deal = deals.get((r["sku"], r["lot_id"])) or deals.get((r["sku"], None))
            out.append(
                NearExpiryItem(
                    store_id=store_id,
                    sku=r["sku"],
                    slot_id=r["slot_id"],
                    lot=Lot(lot_id=r["lot_id"], qty=r["qty"], expiry=r["expiry"], freshness=r["freshness"]),
                    days_left=(r["expiry"] - today).days,
                    price_cents=deal.price_cents if deal else r["list_price"],
                    original_cents=r["list_price"],
                    deal=deal,
                )
            )
        return out

    # ------------------------------------------------------------------------------------------------- writes
    async def record_pick(
        self,
        store_id: StoreId,
        slot_id: SlotId,
        sku: Sku,
        qty: int = 1,
        trip_id: TripId | None = None,
        robot_id: RobotId | None = None,
        lot_id: LotId | None = None,
    ) -> ScanApplyResult:
        from sqlalchemy import text

        actor = f"robot:{robot_id}" if robot_id else "system"
        async with self._get_engine().begin() as conn:

            async def q(sql: str, **params: Any) -> list[dict[str, Any]]:
                result = await conn.execute(text(sql), params)
                return [dict(r._mapping) for r in result] if result.returns_rows else []

            rows = await q(
                "SELECT i.qty FROM inventory i WHERE i.store_id = :sid AND i.slot_id = :slot AND i.sku = :sku "
                "FOR UPDATE",
                sid=store_id,
                slot=slot_id,
                sku=sku,
            )
            if not rows:
                return ScanApplyResult(store_id=store_id, slot_id=slot_id, applied=False, notes="unknown slot/sku")
            before = rows[0]["qty"]
            n = max(0, min(qty, before))
            sku_before = (
                await q(
                    "SELECT COALESCE(SUM(qty), 0)::int AS n FROM inventory WHERE store_id = :sid AND sku = :sku",
                    sid=store_id,
                    sku=sku,
                )
            )[0]["n"]
            if n:
                await q(
                    "UPDATE inventory SET qty = qty - :n, updated_at = now() WHERE store_id = :sid AND slot_id = :slot",
                    n=n,
                    sid=store_id,
                    slot=slot_id,
                )
                remaining = n  # the picked unit's lot first, then earliest expiry (same rule as the fake)
                for lot in await q(
                    "SELECT lot_id, qty FROM lots WHERE store_id = :sid AND slot_id = :slot AND qty > 0 "
                    "ORDER BY (lot_id = CAST(:lot AS TEXT)) DESC NULLS LAST, expiry NULLS LAST, lot_id FOR UPDATE",
                    sid=store_id,
                    slot=slot_id,
                    lot=lot_id,
                ):
                    take = min(lot["qty"], remaining)
                    await q(
                        "UPDATE lots SET qty = qty - :t WHERE store_id = :sid AND slot_id = :slot AND lot_id = :lot",
                        t=take,
                        sid=store_id,
                        slot=slot_id,
                        lot=lot["lot_id"],
                    )
                    remaining -= take
                    if remaining == 0:
                        break
                mv = StockMovement(
                    movement_id=new_id("mv"),
                    store_id=store_id,
                    sku=sku,
                    slot_id=slot_id,
                    lot_id=lot_id,
                    delta=-n,
                    qty_before=sku_before,
                    qty_after=sku_before - n,
                    reason="pick",
                    actor=actor,
                    ref_id=trip_id,
                    note="robot pick" if robot_id else "",
                )
                await q(
                    "INSERT INTO stock_movements (movement_id, store_id, sku, slot_id, lot_id, delta, qty_before, "
                    "qty_after, reason, actor, ref_id, note, ts) VALUES (:movement_id, :store_id, :sku, :slot_id, "
                    ":lot_id, :delta, :qty_before, :qty_after, :reason, :actor, :ref_id, :note, :ts)",
                    **mv.model_dump(),
                )
        after = before - n
        if n:
            try:
                await self._after_change(store_id, sku, [mv], trigger="pick", actor=actor)
            except Exception as exc:  # stock is committed; alerts/events are best-effort
                log.warning("catalog: post-pick alert/event failed: %s", exc)
        return ScanApplyResult(
            store_id=store_id,
            slot_id=slot_id,
            qty_before=before,
            qty_after=after,
            out_of_stock=after == 0,
            notes="picked from a slot recorded as empty (stale stock)" if qty > before else "",
        )

    # ------------------------------------------------------------------------------------------------- stock
    async def _require_store(self, store_id: StoreId) -> None:
        rows = await self._rows("SELECT kind FROM stores WHERE store_id = :sid", sid=store_id)
        if not rows or rows[0]["kind"] != "physical":
            raise KeyError(f"unknown physical store {store_id!r}")

    async def _levels(self, store_id: StoreId, sku: Sku | None = None) -> list[StockLevel]:
        today = utcnow().date()
        rows = await self._rows(
            _LEVELS_SQL, sid=store_id, sku=sku, d0=today, d1=today + timedelta(days=NEAR_EXPIRY_DAYS)
        )
        return [
            StockLevel(**{**r, "category": r["category"] or UNCATEGORIZED, "slot_ids": list(r["slot_ids"] or [])})
            for r in rows
        ]

    async def _get_alert(self, store_id: StoreId, sku: Sku) -> StockAlert | None:
        rows = await self._rows(
            "SELECT a.*, p.name, p.category, p.unit, p.size, p.image_url FROM stock_alerts a "
            "JOIN products p ON p.sku = a.sku WHERE a.store_id = :sid AND a.sku = :sku",
            sid=store_id,
            sku=sku,
        )
        return StockAlert.model_validate(rows[0]) if rows else None

    async def _after_change(
        self, store_id: StoreId, sku: Sku, movements: list[StockMovement], *, trigger: StockReason, actor: str
    ) -> tuple[StockLevel | None, StockAlert | None]:
        """Alert lifecycle (``stock.next_alert``) persisted to stock_alerts, then stock.updated / stock.alert."""
        levels = await self._levels(store_id, sku)
        if not levels:
            return None, None
        level = levels[0]
        alert, changed = next_alert(
            await self._get_alert(store_id, sku), level, trigger=trigger, actor=actor, now=utcnow()
        )
        if alert is not None:
            await self._exec(
                "INSERT INTO stock_alerts (alert_id, store_id, sku, status, qty, low_threshold, capacity, message, "
                "trigger, actor, active, raised_at, cleared_at) VALUES (:alert_id, :store_id, :sku, :status, :qty, "
                ":low_threshold, :capacity, :message, :trigger, :actor, :active, :raised_at, :cleared_at) "
                "ON CONFLICT (alert_id) DO UPDATE SET status = EXCLUDED.status, qty = EXCLUDED.qty, "
                "low_threshold = EXCLUDED.low_threshold, capacity = EXCLUDED.capacity, message = EXCLUDED.message, "
                "trigger = EXCLUDED.trigger, actor = EXCLUDED.actor, active = EXCLUDED.active, "
                "raised_at = EXCLUDED.raised_at, cleared_at = EXCLUDED.cleared_at",
                **alert.model_dump(
                    include={
                        "alert_id",
                        "store_id",
                        "sku",
                        "status",
                        "qty",
                        "low_threshold",
                        "capacity",
                        "message",
                        "trigger",
                        "actor",
                        "active",
                        "raised_at",
                        "cleared_at",
                    }
                ),
            )
        events = getattr(self.services, "events", None) if self.services is not None else None
        if events is not None:
            robot_id = robot_id_from_actor(actor)
            for mv in movements:
                await events.publish(
                    make_event(
                        "stock.updated",
                        trip_id=None,
                        store_id=store_id,
                        robot_id=robot_id,
                        source="catalog",
                        level=level,
                        movement=mv,
                    )
                )
            if changed and alert is not None:
                await events.publish(
                    make_event(
                        "stock.alert", trip_id=None, store_id=store_id, robot_id=robot_id, source="catalog", alert=alert
                    )
                )
        return level, alert

    async def stock_levels(
        self,
        store_id: StoreId,
        status: StockStatus | None = None,
        category: CategoryId | None = None,
        q: str | None = None,
    ) -> list[StockLevel]:
        await self._require_store(store_id)
        cat = category.strip().lower() if category else None
        levels = [
            lv
            for lv in await self._levels(store_id)
            if (status is None or lv.status == status)
            and (cat is None or cat in {lv.category.lower(), (lv.subcategory or "").lower()})
            and matches_query(q, lv.sku, lv.name, lv.brand)
        ]
        return sort_levels(levels)

    async def stock_alerts(self, store_id: StoreId) -> list[StockAlert]:
        await self._require_store(store_id)
        rows = await self._rows(
            "SELECT a.*, p.name, p.category, p.unit, p.size, p.image_url, "
            "(SELECT ARRAY_AGG(s.slot_id ORDER BY s.slot_id) FROM slots s "
            "  WHERE s.store_id = a.store_id AND s.sku = a.sku) AS slot_ids, "
            "(SELECT MIN(s.aisle_label) FROM slots s WHERE s.store_id = a.store_id AND s.sku = a.sku) AS aisle_label "
            "FROM stock_alerts a JOIN products p ON p.sku = a.sku WHERE a.store_id = :sid AND a.active",
            sid=store_id,
        )
        alerts = [
            StockAlert.model_validate(
                {
                    **r,
                    "slot_ids": list(r["slot_ids"] or []),
                    "suggested_restock_qty": max(0, r["capacity"] - r["qty"]),
                }
            )
            for r in rows
        ]
        return sort_alerts(alerts)

    async def stock_history(
        self,
        store_id: StoreId,
        sku: Sku | None = None,
        limit: int = 100,
        before: Timestamp | None = None,
        reason: StockReason | None = None,
        actor: str | None = None,
    ) -> list[StockMovement]:
        await self._require_store(store_id)
        rows = await self._rows(
            "SELECT * FROM stock_movements WHERE store_id = :sid "
            "AND (CAST(:sku AS TEXT) IS NULL OR sku = :sku) "
            "AND (CAST(:before AS TIMESTAMPTZ) IS NULL OR ts < :before) "
            "AND (CAST(:reason AS TEXT) IS NULL OR reason = :reason) "
            "AND (CAST(:actor AS TEXT) IS NULL OR actor = :actor "
            "     OR (RIGHT(:actor, 1) = ':' AND actor LIKE :actor || '%')) "
            "ORDER BY ts DESC, movement_id DESC LIMIT :limit",
            sid=store_id,
            sku=sku,
            before=before,
            reason=reason,
            actor=actor,
            limit=max(0, limit),
        )
        return [StockMovement.model_validate(r) for r in rows]

    async def list_categories(self, store_id: StoreId | None = None) -> list[CategoryInfo]:
        defs = [
            CategoryDef.model_validate({**r, "aliases": list(r.get("aliases") or [])})
            for r in await self._rows(
                "SELECT category_id, name, parent_id, description, icon, color, aliases, sort FROM categories"
            )
        ]
        products = [
            Product.model_validate({**r, "asset": _json(r.get("asset")) or {}})
            for r in await self._rows("SELECT * FROM products")
        ]
        by_sku = {p.sku: p for p in products}
        if store_id == ONLINE_STORE_ID:
            scope = {r["sku"]: (r["qty"], 0) for r in await self._rows("SELECT sku, qty FROM online_items")}
            aisles: list[dict[str, Any]] = []
        else:
            if store_id is not None:
                await self._require_store(store_id)
            scope = {
                r["sku"]: (r["qty"], r["thr"])
                for r in await self._rows(
                    "SELECT s.sku, SUM(COALESCE(i.qty, 0))::int AS qty, "
                    "SUM(COALESCE(s.low_threshold, GREATEST(1, CEIL(s.capacity * 0.25))::int))::int AS thr "
                    "FROM slots s JOIN stores st ON st.store_id = s.store_id AND st.kind = 'physical' "
                    "LEFT JOIN inventory i USING (store_id, slot_id) "
                    "WHERE CAST(:sid AS TEXT) IS NULL OR s.store_id = :sid GROUP BY s.sku",
                    sid=store_id,
                )
            }
            aisles = await self._rows(
                "SELECT DISTINCT s.sku, s.aisle_label FROM slots s JOIN stores st ON st.store_id = s.store_id "
                "AND st.kind = 'physical' WHERE s.aisle_label IS NOT NULL "
                "AND (CAST(:sid AS TEXT) IS NULL OR s.store_id = :sid)",
                sid=store_id,
            )
        out: list[CategoryInfo] = []
        for cat in merge_taxonomy(defs, products):
            skus = [sku for sku in scope if sku in by_sku and category_matches(by_sku[sku], cat.category_id)]
            statuses = [stock_status(*scope[sku]) for sku in skus]
            labels = {
                a["aisle_label"]
                for a in aisles
                if a["sku"] in by_sku and category_matches(by_sku[a["sku"]], cat.category_id)
            }
            out.append(
                CategoryInfo(
                    category_id=cat.category_id,
                    name=cat.name,
                    parent_id=cat.parent_id,
                    description=cat.description,
                    icon=cat.icon,
                    color=cat.color,
                    sort=cat.sort,
                    store_id=store_id,
                    aisle_labels=sorted(labels, key=natural_key),
                    product_count=len(skus),
                    in_stock_count=sum(1 for st in statuses if st != "out"),
                    low_stock_count=statuses.count("low"),
                    out_of_stock_count=statuses.count("out"),
                )
            )
        return out

    # ------------------------------------------------------------------------------------------------- TODO
    async def search(self, query: SearchQuery) -> list[SearchHit]:
        raise NotImplementedError("catalog real: search (TODO 1); use the fake (unset PICKY_REAL_ARMS)")

    async def get_offers(self, sku: Sku, store_ids: list[StoreId] | None = None) -> list[StoreOffer]:
        raise NotImplementedError("catalog real: get_offers (TODO 2)")

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        raise NotImplementedError("catalog real: apply_scan (TODO 3)")

    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard:
        raise NotImplementedError("catalog real: merchant_dashboard (TODO 4)")

    async def category_products(self, category_id: CategoryId, store_id: StoreId | None = None) -> list[SearchHit]:
        raise NotImplementedError("catalog real: category_products (TODO 7, needs get_offers)")

    async def restock(self, store_id: StoreId, request: RestockRequest) -> StockChangeResult:
        raise NotImplementedError("catalog real: restock (TODO 8); use the fake (unset PICKY_REAL_ARMS)")

    async def adjust_stock(self, store_id: StoreId, adjustment: StockAdjustment) -> StockChangeResult:
        raise NotImplementedError("catalog real: adjust_stock (TODO 8)")
