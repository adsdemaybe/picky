"""Postgres-backed CatalogService (real impl; ``PICKY_REAL_ARMS=catalog``). Extra: ``uv sync --extra catalog``.

Tables: contracts/db/schema.sql, filled by ``python -m arms.catalog.seed --schema --apply`` (``make seed``).
SQLAlchemy async + psycopg 3 (``DATABASE_URL=postgresql+psycopg://...``). The engine is created lazily on first call,
never in the constructor. No Docker? Any Postgres >= 14 with pgvector works, e.g. the pip ``pgserver`` binaries run
as a normal user (``initdb`` + ``postgres -p 5433``; arms/catalog/README.md "How to run").

Every call reads the live database; nothing is cached between calls. Where a location comes from:
``slots`` (store, fixture, aisle, level, index, item_x/y/z, approach_x/y/theta, capacity, price override, shelf tag),
``inventory`` (qty), ``lots`` (per-lot qty / expiry / freshness), ``deals``, ``online_items``, ``products``; the
planogram JSONB in ``stores`` only contributes the static layout (bounds, fixtures, aisles, entrance / checkout).

- Reads: ``get_locations`` / ``list_stores`` / ``get_product`` / ``get_deals`` / ``get_near_expiry`` / stock levels,
  alerts, history and categories are SQL. ``get_planogram``, ``search``, ``get_offers``, ``category_products`` and
  ``merchant_dashboard`` build a per-call SNAPSHOT of the rows above (``_snapshot``) and run the fake's pure pricing
  / ranking rules on it, so the two implementations can never disagree on deal ids, the price rule or ranking.
- Writes: ``record_pick`` is SQL. ``apply_scan`` / ``restock`` / ``adjust_stock`` apply the fake's stock rules to a
  snapshot (no events), then persist the touched slots (inventory + lots + shelf tag), the ``scans`` row and the
  movements in one transaction, and publish ``stock.updated`` / ``stock.alert`` from the database state
  (``_after_change``). Writes are serialized per process (one asyncio lock).

TODO: pgvector product embeddings (EMBED_DIM=384) blended into search [P1]; Open Food Facts enrichment [P2].
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from arms.catalog.fake import FakeCatalogService, _SlotStats
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
    DealTerms,
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
from contracts.planogram import CategoryDef, OnlineCatalog, OnlineItem, Planogram, Slot
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
        self._write_lock = asyncio.Lock()

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
        """The store's layout (stores.planogram) with EVERY slot rebuilt from the slots / inventory / lots / deals
        rows (positions, approach poses, stock): the robot world, routing and the fleet all read locations from here."""
        view = await self._snapshot([store_id], with_online=False)
        plan = view._plans.get(store_id)
        if plan is None:
            raise KeyError(f"no planogram for store {store_id!r}")
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
        for r in rows:
            log.info(
                "catalog db: %s @ %s -> slot %s (%s, %s level %d idx %d) item (%.2f, %.2f, %.2f) approach (%.2f, %.2f, "
                "%.2f) qty %d [slots + inventory]",
                sku,
                r["store_id"],
                r["slot_id"],
                r["aisle_label"] or r["aisle_id"],
                r["fixture_id"],
                r["level"],
                r["idx"],
                r["item_x"],
                r["item_y"],
                r["item_z"],
                r["approach_x"],
                r["approach_y"],
                r["approach_theta"],
                r["qty"],
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
                    "LEFT JOIN inventory i ON i.store_id = s.store_id AND i.slot_id = s.slot_id "
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

    # ------------------------------------------------------------------------------------------------- snapshot
    async def _snapshot(
        self,
        store_ids: list[StoreId] | None = None,
        *,
        with_online: bool = True,
        skus: list[Sku] | None = None,
    ) -> FakeCatalogService:
        """A detached, event-less ``FakeCatalogService`` holding the CURRENT database state of ``store_ids``
        (None = every store) + the online store: its pure rules (offers, deals, search ranking, stock math) then run on
        rows read in this call. Never loads ``data/``; ``services=None`` so it publishes / syncs nothing."""
        view = FakeCatalogService(None)
        prod_rows = await self._rows(
            "SELECT * FROM products WHERE CAST(:skus AS TEXT[]) IS NULL OR sku = ANY(CAST(:skus AS TEXT[]))",
            skus=skus,
        )
        products = {r["sku"]: Product.model_validate({**r, "asset": _json(r.get("asset")) or {}}) for r in prod_rows}
        stores = await self._rows(
            "SELECT store_id, kind, name, planogram, shipping_cents, free_shipping_over_cents, default_eta_hours "
            "FROM stores WHERE CAST(:sids AS TEXT[]) IS NULL OR store_id = ANY(CAST(:sids AS TEXT[])) "
            "OR (CAST(:online AS BOOLEAN) AND kind = 'online')",
            sids=store_ids,
            online=with_online,
        )
        physical = [r for r in stores if r["kind"] == "physical" and r["planogram"] is not None]
        sids = [r["store_id"] for r in physical]
        slot_rows = await self._rows(
            "SELECT s.*, COALESCE(i.qty, 0) AS qty FROM slots s LEFT JOIN inventory i USING (store_id, slot_id) "
            "WHERE s.store_id = ANY(CAST(:sids AS TEXT[])) "
            "AND (CAST(:skus AS TEXT[]) IS NULL OR s.sku = ANY(CAST(:skus AS TEXT[])))",
            sids=sids,
            skus=skus,
        )
        lot_rows = await self._rows(
            "SELECT store_id, slot_id, lot_id, qty, expiry, freshness FROM lots WHERE store_id = ANY(CAST(:sids AS "
            "TEXT[])) ORDER BY store_id, slot_id, lot_id",
            sids=sids,
        )
        deal_rows = await self._rows(
            "SELECT * FROM deals WHERE store_id = ANY(CAST(:sids AS TEXT[]))",
            sids=[r["store_id"] for r in stores],
        )
        lots: dict[tuple[str, str], list[Lot]] = {}
        for r in lot_rows:
            lots.setdefault((r["store_id"], r["slot_id"]), []).append(
                Lot(lot_id=r["lot_id"], qty=r["qty"], expiry=r["expiry"], freshness=r["freshness"])
            )
        deals = {r["deal_id"]: r for r in deal_rows}

        def terms(row: dict[str, Any] | None) -> DealTerms | None:
            if row is None:
                return None
            return DealTerms(
                kind=row["kind"],
                price_cents=row["price_cents"],
                original_cents=row["original_cents"],
                percent_off=row["percent_off"],
                reason=row["reason"] or "",
                valid_until=row["valid_until"],
                lot_id=row["lot_id"],
            )

        by_store: dict[str, list[dict[str, Any]]] = {}
        for r in slot_rows:
            by_store.setdefault(r["store_id"], []).append(r)
        plans: dict[StoreId, Planogram] = {}
        for st in physical:
            sid = st["store_id"]
            layout = _json(st["planogram"])
            static = {sl["slot_id"]: sl for sl in layout.get("slots", [])}
            order = {slot_id: i for i, slot_id in enumerate(static)}
            rows = sorted(by_store.get(sid, []), key=lambda r: (order.get(r["slot_id"], len(order)), r["slot_id"]))
            slots = []
            for r in rows:
                base = dict(static.get(r["slot_id"], {}))
                base.update(
                    slot_id=r["slot_id"],
                    fixture_id=r["fixture_id"],
                    aisle_id=r["aisle_id"],
                    sku=r["sku"],
                    level=r["level"],
                    index=r["idx"],
                    item_position={"x": r["item_x"], "y": r["item_y"], "z": r["item_z"]},
                    approach_pose={"x": r["approach_x"], "y": r["approach_y"], "theta": r["approach_theta"]},
                    facings=r["facings"],
                    capacity=r["capacity"],
                    qty=r["qty"],
                    low_threshold=r["low_threshold"],
                    price_cents=r["price_cents"],
                    shelf_tag_cents=r["shelf_tag_cents"],
                    lots=lots.get((sid, r["slot_id"]), []),
                    deal=terms(deals.get(f"{sid}:{r['slot_id']}")),
                )
                slots.append(Slot.model_validate(base))
            plans[sid] = Planogram.model_validate({**layout, "slots": [sl.model_dump() for sl in slots]})
        online_row = next((r for r in stores if r["kind"] == "online"), None)
        online: OnlineCatalog | None = None
        if online_row is not None:
            items = [
                OnlineItem(
                    sku=r["sku"],
                    price_cents=r["price_cents"],
                    qty=r["qty"],
                    eta_hours=r["eta_hours"],
                    deal=terms(deals.get(f"{online_row['store_id']}:{r['sku']}")),
                )
                for r in await self._rows(
                    "SELECT sku, price_cents, qty, eta_hours FROM online_items "
                    "WHERE CAST(:skus AS TEXT[]) IS NULL OR sku = ANY(CAST(:skus AS TEXT[])) ORDER BY sku",
                    skus=skus,
                )
            ]
            extra = {
                k: online_row[k]
                for k in ("shipping_cents", "free_shipping_over_cents", "default_eta_hours")
                if online_row[k] is not None
            }
            online = OnlineCatalog(store_id=online_row["store_id"], name=online_row["name"], items=items, **extra)
        view._products = products
        view._plans = plans
        view._online = online
        view._stats = {
            (sid, sl.slot_id): _SlotStats(last_nonzero_qty=sl.qty) for sid, p in plans.items() for sl in p.slots
        }
        view._scans_total = {sid: 0 for sid in plans}
        view._movements = {sid: [] for sid in plans}
        view._loaded = True
        return view

    async def _persist_slots(self, conn: Any, store_id: StoreId, slots: list[Slot]) -> None:
        """Write the stock state of ``slots`` (inventory qty, lots, shelf tag) inside the caller's transaction."""
        from sqlalchemy import text

        for slot in slots:
            await conn.execute(
                text("UPDATE inventory SET qty = :qty, updated_at = now() WHERE store_id = :sid AND slot_id = :slot"),
                {"qty": slot.qty, "sid": store_id, "slot": slot.slot_id},
            )
            await conn.execute(
                text("UPDATE slots SET shelf_tag_cents = :tag WHERE store_id = :sid AND slot_id = :slot"),
                {"tag": slot.shelf_tag_cents, "sid": store_id, "slot": slot.slot_id},
            )
            for lot in slot.lots:
                await conn.execute(
                    text(
                        "INSERT INTO lots (store_id, slot_id, lot_id, sku, qty, expiry, freshness) "
                        "VALUES (:sid, :slot, :lot, :sku, :qty, :expiry, :fresh) "
                        "ON CONFLICT (store_id, slot_id, lot_id) DO UPDATE SET qty = EXCLUDED.qty, "
                        "expiry = EXCLUDED.expiry, freshness = EXCLUDED.freshness"
                    ),
                    {
                        "sid": store_id,
                        "slot": slot.slot_id,
                        "lot": lot.lot_id,
                        "sku": slot.sku,
                        "qty": lot.qty,
                        "expiry": lot.expiry,
                        "fresh": lot.freshness,
                    },
                )

    async def _insert_movements(self, conn: Any, movements: list[StockMovement]) -> list[StockMovement]:
        """Persist the snapshot's movements under fresh database ids (the snapshot numbers them from 1)."""
        from sqlalchemy import text

        out = []
        for mv in movements:
            mv = mv.model_copy(update={"movement_id": new_id("mv")})
            await conn.execute(
                text(
                    "INSERT INTO stock_movements (movement_id, store_id, sku, slot_id, lot_id, delta, qty_before, "
                    "qty_after, reason, actor, ref_id, note, ts) VALUES (:movement_id, :store_id, :sku, :slot_id, "
                    ":lot_id, :delta, :qty_before, :qty_after, :reason, :actor, :ref_id, :note, :ts)"
                ),
                mv.model_dump(
                    include={
                        "movement_id",
                        "store_id",
                        "sku",
                        "slot_id",
                        "lot_id",
                        "delta",
                        "qty_before",
                        "qty_after",
                        "reason",
                        "actor",
                        "ref_id",
                        "note",
                        "ts",
                    }
                ),
            )
            out.append(mv)
        return out

    async def _sync_sim(self, store_id: StoreId, slot_ids: list[SlotId]) -> None:
        robot = getattr(self.services, "robot", None) if self.services is not None else None
        sync = getattr(robot, "sync_stock", None)
        if sync is None or not slot_ids:
            return
        try:
            await sync(store_id, slot_ids)
        except Exception as exc:  # noqa: BLE001 - best effort (README: failures are logged, never raised)
            log.warning("catalog: robot.sync_stock(%s, %s) failed: %s", store_id, slot_ids, exc)

    # ------------------------------------------------------------------------------------------------- snapshot reads
    async def search(self, query: SearchQuery) -> list[SearchHit]:
        view = await self._snapshot(query.store_ids)
        return await view.search(query)

    async def get_offers(self, sku: Sku, store_ids: list[StoreId] | None = None) -> list[StoreOffer]:
        view = await self._snapshot(
            store_ids, with_online=store_ids is None or ONLINE_STORE_ID in store_ids, skus=[sku]
        )
        offers = view._offers(sku, store_ids)
        for o in offers:
            loc = o.location
            where = (
                f"slot {loc.slot_id} ({loc.aisle_label}, {loc.fixture_id} level {loc.level}) "
                f"item ({loc.item_position.x:.2f}, {loc.item_position.y:.2f}, {loc.item_position.z:.2f})"
                if loc is not None
                else "online"
            )
            log.info("catalog db: offer %s @ %s %d cents qty %d -> %s", sku, o.store_id, o.price_cents, o.qty, where)
        return offers

    async def category_products(self, category_id: CategoryId, store_id: StoreId | None = None) -> list[SearchHit]:
        view = await self._snapshot(None if store_id is None else [store_id])
        defs = [
            CategoryDef.model_validate({**r, "aliases": list(r.get("aliases") or [])})
            for r in await self._rows(
                "SELECT category_id, name, parent_id, description, icon, color, aliases, sort FROM categories"
            )
        ]
        view._taxonomy = merge_taxonomy(defs, view._products.values())
        return await view.category_products(category_id, store_id)

    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard:
        if store_id == ONLINE_STORE_ID:
            raise KeyError("the online store has no merchant dashboard")
        view = await self._snapshot([store_id], with_online=False)
        if store_id not in view._plans:
            raise KeyError(f"no planogram for store {store_id!r}")
        for r in await self._rows(
            "SELECT slot_id, COUNT(*)::int AS n, MAX(ts) AS last_ts, BOOL_OR(shelf_tag_cents IS NOT NULL) AS tag "
            "FROM scans WHERE store_id = :sid AND slot_id IS NOT NULL GROUP BY slot_id",
            sid=store_id,
        ):
            st = view._stats.get((store_id, r["slot_id"]))
            if st is not None:
                st.scans, st.last_scan_at = r["n"], r["last_ts"]
                st.mispricing_at = r["last_ts"] if r["tag"] else None
        for r in await self._rows(
            "SELECT slot_id, SUM(CASE WHEN reason = 'pick' THEN -delta ELSE 0 END)::int AS picks, "
            "SUM(CASE WHEN qty_after = 0 AND qty_before > 0 THEN 1 ELSE 0 END)::int AS oos "
            "FROM stock_movements WHERE store_id = :sid AND slot_id IS NOT NULL GROUP BY slot_id",
            sid=store_id,
        ):
            st = view._stats.get((store_id, r["slot_id"]))
            if st is not None:
                st.picks, st.oos_events = r["picks"], r["oos"]
        view._scans_total[store_id] = (
            await self._rows("SELECT COUNT(*)::int AS n FROM scans WHERE store_id = :sid", sid=store_id)
        )[0]["n"]
        return await view.merchant_dashboard(store_id)

    # ------------------------------------------------------------------------------------------------- snapshot writes
    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        async with self._write_lock:
            view = await self._snapshot([report.store_id], with_online=False)
            result = await view.apply_scan(report)
            if not result.applied:
                return result
            plan = view._plans[report.store_id]
            slot = plan.slot(result.slot_id) if result.slot_id else None
            movements = view._movements.get(report.store_id, [])
            async with self._get_engine().begin() as conn:
                from sqlalchemy import text

                if slot is not None:
                    await self._persist_slots(conn, report.store_id, [slot])
                    await conn.execute(
                        text(
                            "UPDATE inventory SET last_scan_at = :ts, last_scan_id = :scan WHERE store_id = :sid "
                            "AND slot_id = :slot"
                        ),
                        {"ts": report.ts, "scan": report.scan_id, "sid": report.store_id, "slot": slot.slot_id},
                    )
                await conn.execute(
                    text(
                        "INSERT INTO scans (scan_id, store_id, slot_id, trip_id, expected_sku, observed_count, "
                        "count_is_exact, mismatch, shelf_tag_cents, detections, freshness, lot_freshness, pose_x, "
                        "pose_y, pose_theta, frame_id, camera_id, robot_id, goal_id, latency_ms, notes, ts) VALUES "
                        "(:scan_id, :store_id, :slot_id, :trip_id, :expected_sku, :observed_count, :count_is_exact, "
                        ":mismatch, :shelf_tag_cents, CAST(:detections AS JSONB), CAST(:freshness AS JSONB), "
                        "CAST(:lot_freshness AS JSONB), :pose_x, :pose_y, :pose_theta, :frame_id, :camera_id, "
                        ":robot_id, :goal_id, :latency_ms, :notes, :ts) ON CONFLICT (scan_id) DO NOTHING"
                    ),
                    {
                        "scan_id": report.scan_id,
                        "store_id": report.store_id,
                        "slot_id": result.slot_id,
                        "trip_id": getattr(report, "trip_id", None),
                        "expected_sku": report.expected_sku,
                        "observed_count": report.observed_count,
                        "count_is_exact": report.count_is_exact,
                        "mismatch": bool(result.misplaced_skus),
                        "shelf_tag_cents": report.shelf_tag_cents,
                        "detections": json.dumps([d.model_dump(mode="json") for d in report.detections]),
                        "freshness": json.dumps(getattr(report, "freshness", {}) or {}),
                        "lot_freshness": json.dumps(report.lot_freshness or {}),
                        "pose_x": report.pose.x if report.pose else None,
                        "pose_y": report.pose.y if report.pose else None,
                        "pose_theta": report.pose.theta if report.pose else None,
                        "frame_id": getattr(report, "frame_id", None),
                        "camera_id": getattr(report, "camera_id", None),
                        "robot_id": report.robot_id,
                        "goal_id": getattr(report, "goal_id", None),
                        "latency_ms": float(getattr(report, "latency_ms", 0.0) or 0.0),
                        "notes": result.notes or "",
                        "ts": report.ts,
                    },
                )
                movements = await self._insert_movements(conn, movements)
        if movements and slot is not None:
            actor = f"robot:{report.robot_id}" if report.robot_id else "system"
            try:
                await self._after_change(report.store_id, slot.sku, movements, trigger="scan", actor=actor)
            except Exception as exc:  # noqa: BLE001 - stock is committed; alerts/events are best-effort
                log.warning("catalog: post-scan alert/event failed: %s", exc)
        return result

    async def _stock_change(
        self, store_id: StoreId, sku: Sku, apply: Any, trigger: StockReason, actor: str
    ) -> StockChangeResult:
        """restock / adjust_stock: the fake's validation + stock math on a snapshot (raises KeyError / ValueError with
        nothing written), then the touched slots + movements persisted, events published and the sim re-synced."""
        await self._require_store(store_id)
        async with self._write_lock:
            view = await self._snapshot([store_id], with_online=False)
            res: StockChangeResult = await apply(view)
            if not res.movements:
                levels = await self._levels(store_id, sku)
                alert = await self._get_alert(store_id, sku)
                return res.model_copy(
                    update={
                        "level": levels[0] if levels else res.level,
                        "alert": alert if alert and alert.active else None,
                    }
                )
            plan = view._plans[store_id]
            touched = [s for s in plan.slots if s.slot_id in {mv.slot_id for mv in res.movements}]
            async with self._get_engine().begin() as conn:
                await self._persist_slots(conn, store_id, touched)
                movements = await self._insert_movements(conn, res.movements)
        level, alert = await self._after_change(store_id, sku, movements, trigger=trigger, actor=actor)
        await self._sync_sim(store_id, [s.slot_id for s in touched])
        return StockChangeResult(store_id=store_id, sku=sku, movements=movements, level=level or res.level, alert=alert)

    async def restock(self, store_id: StoreId, request: RestockRequest) -> StockChangeResult:
        return await self._stock_change(
            store_id, request.sku, lambda v: v.restock(store_id, request), "restock", request.actor
        )

    async def adjust_stock(self, store_id: StoreId, adjustment: StockAdjustment) -> StockChangeResult:
        return await self._stock_change(
            store_id,
            adjustment.sku,
            lambda v: v.adjust_stock(store_id, adjustment),
            adjustment.reason,
            adjustment.actor,
        )
