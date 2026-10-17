"""Postgres-backed CatalogService (real impl; ``PICKY_REAL_ARMS=catalog``). Extra: ``uv sync --extra catalog``.

Tables: contracts/db/schema.sql, filled by ``python -m arms.catalog.seed --schema --apply`` (``make seed``).
SQLAlchemy async + psycopg 3 (``DATABASE_URL=postgresql+psycopg://...``). The engine is created lazily on first call,
never in the constructor. Read paths and ``record_pick`` are real; the rest raise NotImplementedError with a TODO
list. Until then run the fake (default) — it implements every method over the same data.

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
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from contracts.catalog import (
    Deal,
    Lot,
    MerchantDashboard,
    NearExpiryItem,
    Product,
    ProductLocation,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StoreInfo,
    StoreOffer,
)
from contracts.common import ONLINE_STORE_ID, Point3, Pose2D, Sku, SlotId, StoreId, TripId, utcnow
from contracts.perception import ScanReport
from contracts.planogram import Planogram
from contracts.services import Services
from utils.log import get_logger

log = get_logger(__name__)


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
        self, store_id: StoreId, slot_id: SlotId, sku: Sku, qty: int = 1, trip_id: TripId | None = None
    ) -> ScanApplyResult:
        rows = await self._exec(
            "UPDATE inventory i SET qty = GREATEST(i.qty - :n, 0), updated_at = now() "
            "FROM inventory old WHERE i.store_id = :sid AND i.slot_id = :slot AND i.sku = :sku "
            "AND old.store_id = i.store_id AND old.slot_id = i.slot_id "
            "RETURNING old.qty AS before, i.qty AS after",
            n=max(qty, 0),
            sid=store_id,
            slot=slot_id,
            sku=sku,
        )
        if not rows:
            return ScanApplyResult(store_id=store_id, slot_id=slot_id, applied=False, notes="unknown slot/sku")
        before, after = rows[0]["before"], rows[0]["after"]
        # TODO(5): decrement lots FIFO + persist pick counts for the heatmap.
        return ScanApplyResult(
            store_id=store_id, slot_id=slot_id, qty_before=before, qty_after=after, out_of_stock=after == 0
        )

    # ------------------------------------------------------------------------------------------------- TODO
    async def search(self, query: SearchQuery) -> list[SearchHit]:
        raise NotImplementedError("catalog real: search (TODO 1); use the fake (unset PICKY_REAL_ARMS)")

    async def get_offers(self, sku: Sku, store_ids: list[StoreId] | None = None) -> list[StoreOffer]:
        raise NotImplementedError("catalog real: get_offers (TODO 2)")

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        raise NotImplementedError("catalog real: apply_scan (TODO 3)")

    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard:
        raise NotImplementedError("catalog real: merchant_dashboard (TODO 4)")
