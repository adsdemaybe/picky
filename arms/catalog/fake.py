"""In-memory CatalogService over ``data/`` (via utils.data). Deterministic, base deps only.

This is THE catalog every other arm's fake leans on, so it is complete: multi-store search (physical + online),
locations, offers, deals, near-expiry, scan application (stock / per-lot freshness / mispricing / slot-less location)
and a merchant dashboard computed from scans + picks. State lives in memory for the process lifetime; ``reset()``
reloads it from disk.

Deal ids: physical slot deal -> ``f"{store_id}:{slot_id}"``; online item deal -> ``f"online:{sku}"``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from contracts.catalog import (
    Deal,
    DealTerms,
    HeatmapCell,
    MerchantDashboard,
    Mispricing,
    NearExpiryItem,
    OutOfStockItem,
    Product,
    ProductLocation,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StoreInfo,
    StoreOffer,
)
from contracts.common import ONLINE_STORE_ID, Rect2D, Sku, SlotId, StoreId, TripId, utcnow
from contracts.perception import ScanReport
from contracts.planogram import OnlineCatalog, Planogram, Slot
from contracts.services import Services
from utils import data as data_io
from utils.log import get_logger

log = get_logger(__name__)

# Max distance (m) between a slot-less scan's robot pose and a slot's approach pose to attribute the scan to it.
POSE_MATCH_RADIUS_M = 1.0
HEATMAP_CELL_M = 0.5

_STOPWORDS = {"a", "an", "the", "of", "for", "and", "with", "some", "any", "to", "in", "or", "pack", "x"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Field weights for text scoring (best field wins per query token).
_W_NAME = 3.0
_W_TAG = 2.0
_W_CATEGORY = 1.5
_W_BRAND = 1.0
_W_DESC = 0.5
_W_PARTIAL = 0.75
_W_PHRASE = 2.0


# ----------------------------------------------------------------------------------------------------------- text
def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("oes"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    """Lowercase alnum tokens, stopwords dropped, naive plural stemming ('limes' -> 'lime', 'berries' -> 'berry')."""
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _token_set(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            out.update(tokenize(t))
    return out


def text_score(product: Product, text: str) -> float:
    """0 = no match. Mean over query tokens of the best matching field weight, + phrase bonus."""
    q = tokenize(text)
    if not q:
        return 1.0
    name = _token_set(product.name)
    tags = _token_set(*product.tags, *product.dietary)
    cat = _token_set(product.category, product.subcategory)
    brand = _token_set(product.brand)
    desc = _token_set(product.description)
    total = 0.0
    matched = 0
    for tok in q:
        best = 0.0
        if tok in name:
            best = _W_NAME
        elif tok in tags:
            best = _W_TAG
        elif tok in cat:
            best = _W_CATEGORY
        elif tok in brand:
            best = _W_BRAND
        elif tok in desc:
            best = _W_DESC
        elif len(tok) >= 3 and any(n.startswith(tok) or tok.startswith(n) for n in name if len(n) >= 3):
            best = _W_PARTIAL
        if best > 0:
            matched += 1
        total += best
    if matched == 0:
        return 0.0
    score = total / len(q)
    phrase = " ".join(q)
    if len(q) > 1 and phrase in " ".join(tokenize(product.name)):
        score += _W_PHRASE
    return round(score, 4)


# ----------------------------------------------------------------------------------------------------------- state
@dataclass
class _SlotStats:
    scans: int = 0
    picks: int = 0
    oos_events: int = 0
    last_scan_at: datetime | None = None
    last_nonzero_qty: int = 0
    misplaced: list[Sku] = field(default_factory=list)
    mispricing_at: datetime | None = None


class FakeCatalogService:
    """Satisfies ``contracts.services.CatalogService``. Loads lazily on first call (never at construction)."""

    def __init__(
        self,
        services: Services | None = None,
        data_root: str | Path | None = None,
        today: date | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.services = services
        self._data_root = data_root
        self._today = today
        self._now = now or utcnow
        self._loaded = False
        self._products: dict[Sku, Product] = {}
        self._plans: dict[StoreId, Planogram] = {}
        self._online: OnlineCatalog | None = None
        self._stats: dict[tuple[StoreId, SlotId], _SlotStats] = {}
        self._scans_total: dict[StoreId, int] = {}

    # ------------------------------------------------------------------------------------------------- loading
    def _ensure(self) -> None:
        if not self._loaded:
            self.reset()

    def reset(self) -> None:
        """(Re)load products, planograms and the online store from disk; clears scan/pick history."""
        root = self._data_root
        self._products = data_io.load_products(root)
        self._plans = data_io.load_planograms(root)
        try:
            self._online = data_io.load_online_catalog(root)
        except data_io.DataError as exc:
            log.warning("no online store: %s", exc)
            self._online = None
        self._stats = {}
        self._scans_total = {sid: 0 for sid in self._plans}
        for sid, plan in self._plans.items():
            for s in plan.slots:
                self._stats[(sid, s.slot_id)] = _SlotStats(last_nonzero_qty=s.qty)
        self._loaded = True
        log.info(
            "catalog fake loaded %d products, stores=%s%s",
            len(self._products),
            ",".join(self._plans),
            ",online" if self._online else "",
        )

    def today(self) -> date:
        return self._today or self._now().date()

    def _all_store_ids(self) -> list[StoreId]:
        ids = list(self._plans)
        if self._online is not None:
            ids.append(ONLINE_STORE_ID)
        return ids

    def _plan(self, store_id: StoreId) -> Planogram:
        plan = self._plans.get(store_id)
        if plan is None:
            raise KeyError(f"no planogram for store {store_id!r} (known: {', '.join(self._plans) or 'none'})")
        return plan

    def _check_store(self, store_id: StoreId) -> None:
        if store_id not in self._plans and not (store_id == ONLINE_STORE_ID and self._online is not None):
            raise KeyError(f"unknown store {store_id!r}")

    # ------------------------------------------------------------------------------------------------- pricing
    def _list_price(self, slot: Slot) -> int:
        product = self._products.get(slot.sku)
        if slot.price_cents is not None:
            return slot.price_cents
        return product.price_cents if product else 0

    def _deal_active(self, terms: DealTerms) -> bool:
        return terms.valid_until is None or terms.valid_until >= self._now()

    def _slot_deal(self, store_id: StoreId, slot: Slot) -> Deal | None:
        """The slot's deal if valid and (for lot deals) its lot still has stock."""
        terms = slot.deal
        if terms is None or not self._deal_active(terms):
            return None
        if terms.lot_id is not None:
            lot = next((lt for lt in slot.lots if lt.lot_id == terms.lot_id), None)
            if lot is None or lot.qty <= 0:
                return None
        original = terms.original_cents if terms.original_cents is not None else self._list_price(slot)
        return Deal(
            deal_id=f"{store_id}:{slot.slot_id}",
            sku=slot.sku,
            store_id=store_id,
            **terms.model_dump(exclude={"original_cents"}),
            original_cents=original,
        )

    def _online_deal(self, sku: Sku) -> Deal | None:
        if self._online is None:
            return None
        item = self._online.item(sku)
        if item is None or item.deal is None or not self._deal_active(item.deal):
            return None
        base = item.price_cents if item.price_cents is not None else self._products[sku].price_cents
        original = item.deal.original_cents if item.deal.original_cents is not None else base
        return Deal(
            deal_id=f"{ONLINE_STORE_ID}:{sku}",
            sku=sku,
            store_id=ONLINE_STORE_ID,
            **item.deal.model_dump(exclude={"original_cents"}),
            original_cents=original,
        )

    # ------------------------------------------------------------------------------------------------- builders
    def _location(self, store_id: StoreId, plan: Planogram, slot: Slot) -> ProductLocation:
        fixture = plan.fixture(slot.fixture_id)
        aisle_id = slot.aisle_id or (fixture.aisle_id if fixture else None)
        aisle = plan.aisle(aisle_id) if aisle_id else None
        return ProductLocation(
            store_id=store_id,
            sku=slot.sku,
            slot_id=slot.slot_id,
            aisle_id=aisle_id,
            aisle_label=aisle.label if aisle else (fixture.label if fixture else None),
            fixture_id=slot.fixture_id,
            level=slot.level,
            index=slot.index,
            item_position=slot.item_position.model_copy(),
            approach_pose=slot.approach_pose.model_copy(),
            qty=slot.qty,
        )

    def _store_offer(self, store_id: StoreId, sku: Sku) -> StoreOffer | None:
        if store_id == ONLINE_STORE_ID:
            return self._online_offer(sku)
        plan = self._plans.get(store_id)
        if plan is None:
            return None
        slots = plan.slots_for(sku)
        if not slots:
            return None
        # Primary slot: the one with stock (most units), else the first listed.
        primary = max(slots, key=lambda s: (s.qty > 0, s.qty, -slots.index(s)))
        list_price = self._list_price(primary)
        deal = self._slot_deal(store_id, primary)
        lots = [lt.model_copy() for s in slots for lt in s.lots]
        return StoreOffer(
            store_id=store_id,
            sku=sku,
            price_cents=deal.price_cents if deal else list_price,
            list_price_cents=list_price,
            qty=sum(s.qty for s in slots),
            location=self._location(store_id, plan, primary),
            lots=lots,
            deal=deal,
            eta_hours=None,
        )

    def _online_offer(self, sku: Sku) -> StoreOffer | None:
        if self._online is None or sku not in self._products:
            return None
        item = self._online.item(sku)
        if item is None:
            return None
        list_price = item.price_cents if item.price_cents is not None else self._products[sku].price_cents
        deal = self._online_deal(sku)
        return StoreOffer(
            store_id=ONLINE_STORE_ID,
            sku=sku,
            price_cents=deal.price_cents if deal else list_price,
            list_price_cents=list_price,
            qty=item.qty,
            location=None,
            lots=[],
            deal=deal,
            eta_hours=item.eta_hours if item.eta_hours is not None else self._online.default_eta_hours,
        )

    def _offers(self, sku: Sku, store_ids: list[StoreId] | None) -> list[StoreOffer]:
        ids = self._all_store_ids() if store_ids is None else store_ids
        out = []
        for sid in ids:
            offer = self._store_offer(sid, sku)
            if offer is not None:
                out.append(offer)
        return out

    # ================================================================================================ Protocol
    async def list_stores(self) -> list[StoreInfo]:
        self._ensure()
        stores = [
            StoreInfo(
                store_id=sid,
                name=plan.name,
                kind="physical",
                address=plan.address,
                width_m=plan.bounds.w,
                depth_m=plan.bounds.h,
                has_planogram=True,
            )
            for sid, plan in self._plans.items()
        ]
        if self._online is not None:
            stores.append(
                StoreInfo(
                    store_id=ONLINE_STORE_ID,
                    name=self._online.name,
                    kind="online",
                    has_planogram=False,
                    shipping_cents=self._online.shipping_cents,
                    free_shipping_over_cents=self._online.free_shipping_over_cents,
                    default_eta_hours=self._online.default_eta_hours,
                )
            )
        return stores

    async def get_planogram(self, store_id: StoreId) -> Planogram:
        self._ensure()
        return self._plan(store_id).model_copy(deep=True)

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        self._ensure()
        store_ids = query.store_ids
        if store_ids is not None:
            store_ids = [s for s in store_ids if s in self._plans or s == ONLINE_STORE_ID]
        want_tags = {t.strip().lower() for t in query.tags}
        want_diet = {t.strip().lower() for t in query.dietary}
        category = query.category.strip().lower() if query.category else None

        ranked: list[tuple[tuple[Any, ...], SearchHit]] = []
        for sku, product in self._products.items():
            if category and category not in {product.category.lower(), (product.subcategory or "").lower()}:
                continue
            if not want_tags <= set(product.tags) or not want_diet <= set(product.dietary):
                continue
            score = text_score(product, query.text)
            if score <= 0:
                continue
            offers = self._offers(sku, store_ids)
            if query.in_stock_only:
                offers = [o for o in offers if o.in_stock]
            if query.max_price_cents is not None:
                offers = [o for o in offers if o.price_cents <= query.max_price_cents]
            if not offers:
                continue
            stocked = [o.price_cents for o in offers if o.in_stock]
            key = (-score, 0 if stocked else 1, min(stocked) if stocked else 0, sku)
            ranked.append((key, SearchHit(product=product.model_copy(deep=True), offers=offers, score=score)))
        ranked.sort(key=lambda kv: kv[0])
        return [hit for _, hit in ranked[: query.limit]]

    async def get_product(self, sku: Sku) -> Product | None:
        self._ensure()
        product = self._products.get(sku)
        return product.model_copy(deep=True) if product else None

    async def get_locations(self, sku: Sku, store_id: StoreId | None = None) -> list[ProductLocation]:
        self._ensure()
        ids = list(self._plans) if store_id is None else [store_id]
        out: list[ProductLocation] = []
        for sid in ids:
            plan = self._plans.get(sid)
            if plan is None:
                continue  # 'online' / unknown: no physical location
            out.extend(self._location(sid, plan, s) for s in plan.slots_for(sku))
        return out

    async def get_offers(self, sku: Sku, store_ids: list[StoreId] | None = None) -> list[StoreOffer]:
        self._ensure()
        return self._offers(sku, store_ids)

    async def get_deals(self, store_id: StoreId) -> list[Deal]:
        self._ensure()
        self._check_store(store_id)
        if store_id == ONLINE_STORE_ID:
            assert self._online is not None
            deals = [self._online_deal(i.sku) for i in self._online.items if i.sku in self._products]
        else:
            plan = self._plans[store_id]
            deals = [self._slot_deal(store_id, s) for s in plan.slots]
        return [d for d in deals if d is not None]

    async def get_near_expiry(self, store_id: StoreId, within_days: int = 3) -> list[NearExpiryItem]:
        self._ensure()
        self._check_store(store_id)
        if store_id == ONLINE_STORE_ID:
            return []
        return self._near_expiry(store_id, within_days)

    def _near_expiry(self, store_id: StoreId, within_days: int) -> list[NearExpiryItem]:
        today = self.today()
        out: list[NearExpiryItem] = []
        for slot in self._plans[store_id].slots:
            list_price = self._list_price(slot)
            deal = self._slot_deal(store_id, slot)
            for lot in slot.lots:
                if lot.expiry is None or lot.qty <= 0:
                    continue
                days_left = (lot.expiry - today).days
                if not 0 <= days_left <= within_days:
                    continue
                lot_deal = deal if deal is not None and deal.lot_id in (None, lot.lot_id) else None
                out.append(
                    NearExpiryItem(
                        store_id=store_id,
                        sku=slot.sku,
                        slot_id=slot.slot_id,
                        lot=lot.model_copy(),
                        days_left=days_left,
                        price_cents=lot_deal.price_cents if lot_deal else list_price,
                        original_cents=list_price,
                        deal=lot_deal,
                    )
                )
        out.sort(key=lambda n: (n.days_left, n.sku, n.lot.lot_id))
        return out

    # ------------------------------------------------------------------------------------------------- writes
    def _resolve_slot(self, plan: Planogram, report: ScanReport) -> Slot | None:
        if report.slot_id is not None:
            return plan.slot(report.slot_id)
        if report.pose is None:
            return None
        candidates = [s for s in plan.slots if report.expected_sku in (None, s.sku)]
        best = min(candidates, key=lambda s: s.approach_pose.distance_to(report.pose), default=None)
        if best is None or best.approach_pose.distance_to(report.pose) > POSE_MATCH_RADIUS_M:
            return None
        return best

    @staticmethod
    def _set_lot_total(slot: Slot, new_qty: int) -> None:
        """Keep sum(lots.qty) == qty: remove from the earliest-expiring lots first (FIFO), add to the latest."""
        if not slot.lots:
            return
        diff = new_qty - sum(lt.qty for lt in slot.lots)
        order = sorted(slot.lots, key=lambda lt: (lt.expiry or date.max, lt.lot_id))
        if diff < 0:
            for lt in order:
                take = min(lt.qty, -diff)
                lt.qty -= take
                diff += take
                if diff == 0:
                    break
        elif diff > 0:
            order[-1].qty += diff

    def _set_qty(self, store_id: StoreId, slot: Slot, new_qty: int) -> tuple[int, int]:
        stats = self._stats.setdefault((store_id, slot.slot_id), _SlotStats())
        before = slot.qty
        slot.qty = max(0, new_qty)
        self._set_lot_total(slot, slot.qty)
        if slot.qty > 0:
            stats.last_nonzero_qty = slot.qty
        elif before > 0:
            stats.oos_events += 1
        return before, slot.qty

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        self._ensure()
        plan = self._plans.get(report.store_id)
        if plan is None:
            return ScanApplyResult(
                store_id=report.store_id, slot_id=report.slot_id, applied=False, notes="no planogram for store"
            )
        slot = self._resolve_slot(plan, report)
        if slot is None:
            why = f"unknown slot {report.slot_id!r}" if report.slot_id else "no slot near scan pose"
            return ScanApplyResult(store_id=report.store_id, slot_id=report.slot_id, applied=False, notes=why)

        sid = report.store_id
        stats = self._stats.setdefault((sid, slot.slot_id), _SlotStats())
        stats.scans += 1
        stats.last_scan_at = report.ts
        self._scans_total[sid] = self._scans_total.get(sid, 0) + 1
        notes: list[str] = []
        if report.slot_id is None:
            notes.append(f"located by pose -> {slot.slot_id}")

        # Stock (only when the scan is about this slot's SKU).
        before = after = slot.qty
        if report.expected_sku in (None, slot.sku):
            lot_ids = {lt.lot_id for lt in slot.lots}
            gt = [d for d in report.detections if d.sku == slot.sku and d.lot_id in lot_ids]
            if report.count_is_exact and slot.lots and gt and len(gt) == report.observed_count:
                counts: dict[str, int] = {}
                for d in gt:
                    counts[d.lot_id or ""] = counts.get(d.lot_id or "", 0) + 1
                for lt in slot.lots:
                    lt.qty = counts.get(lt.lot_id, 0)
                before, after = self._set_qty(sid, slot, sum(lt.qty for lt in slot.lots))
            elif report.count_is_exact:
                before, after = self._set_qty(sid, slot, report.observed_count)
            elif report.observed_count == 0:
                before, after = self._set_qty(sid, slot, 0)
            elif slot.qty < report.observed_count:  # we see more than we thought: at least what is visible
                before, after = self._set_qty(sid, slot, report.observed_count)
        else:
            notes.append(f"expected_sku {report.expected_sku} != slot sku {slot.sku}; stock unchanged")

        # Freshness per lot.
        for lt in slot.lots:
            if lt.lot_id in report.lot_freshness:
                lt.freshness = report.lot_freshness[lt.lot_id]

        # Mispricing (tag matches list price or the active deal price -> fine).
        mispricing = False
        if report.shelf_tag_cents is not None:
            ok_prices = {self._list_price(slot)}
            deal = self._slot_deal(sid, slot)
            if deal is not None:
                ok_prices.add(deal.price_cents)
            if report.shelf_tag_cents in ok_prices:
                slot.shelf_tag_cents = None
                stats.mispricing_at = None
            else:
                mispricing = True
                slot.shelf_tag_cents = report.shelf_tag_cents
                stats.mispricing_at = report.ts

        # Misplaced items (location drift): detections of other SKUs in this slot.
        misplaced = sorted({d.sku for d in report.detections if d.sku is not None and d.sku != slot.sku})
        stats.misplaced = misplaced
        if misplaced:
            notes.append(f"misplaced: {', '.join(misplaced)}")

        return ScanApplyResult(
            store_id=sid,
            slot_id=slot.slot_id,
            applied=True,
            qty_before=before,
            qty_after=after,
            out_of_stock=after == 0,
            mispricing=mispricing,
            misplaced_skus=misplaced,
            notes="; ".join(notes),
        )

    async def record_pick(
        self, store_id: StoreId, slot_id: SlotId, sku: Sku, qty: int = 1, trip_id: TripId | None = None
    ) -> ScanApplyResult:
        self._ensure()
        plan = self._plans.get(store_id)
        slot = plan.slot(slot_id) if plan else None
        if slot is None:
            return ScanApplyResult(store_id=store_id, slot_id=slot_id, applied=False, notes="unknown store/slot")
        if slot.sku != sku:
            return ScanApplyResult(
                store_id=store_id,
                slot_id=slot_id,
                applied=False,
                qty_before=slot.qty,
                qty_after=slot.qty,
                out_of_stock=slot.qty == 0,
                notes=f"sku {sku} is not stocked in slot {slot_id} ({slot.sku})",
            )
        stats = self._stats.setdefault((store_id, slot_id), _SlotStats())
        stats.picks += max(qty, 0)
        notes = "picked from a slot recorded as empty (stale stock)" if slot.qty < qty else ""
        before, after = self._set_qty(store_id, slot, slot.qty - max(qty, 0))
        if trip_id:
            notes = f"{notes}; trip {trip_id}" if notes else f"trip {trip_id}"
        return ScanApplyResult(
            store_id=store_id,
            slot_id=slot_id,
            applied=True,
            qty_before=before,
            qty_after=after,
            out_of_stock=after == 0,
            notes=notes,
        )

    # ------------------------------------------------------------------------------------------------- merchant
    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard:
        self._ensure()
        plan = self._plan(store_id)
        now = self._now()
        oos: list[OutOfStockItem] = []
        mispricings: list[Mispricing] = []
        raw_cells: list[tuple[Slot, _SlotStats]] = []
        for slot in plan.slots:
            stats = self._stats.setdefault((store_id, slot.slot_id), _SlotStats())
            product = self._products.get(slot.sku)
            name = product.name if product else slot.sku
            loc = self._location(store_id, plan, slot)
            if slot.qty == 0:
                oos.append(
                    OutOfStockItem(
                        sku=slot.sku,
                        name=name,
                        slot_id=slot.slot_id,
                        aisle_label=loc.aisle_label,
                        last_qty=stats.last_nonzero_qty,
                        last_scan_at=stats.last_scan_at,
                    )
                )
            if slot.shelf_tag_cents is not None:
                ok = {self._list_price(slot)}
                deal = self._slot_deal(store_id, slot)
                if deal is not None:
                    ok.add(deal.price_cents)
                if slot.shelf_tag_cents not in ok:
                    mispricings.append(
                        Mispricing(
                            sku=slot.sku,
                            name=name,
                            slot_id=slot.slot_id,
                            system_price_cents=self._list_price(slot),
                            shelf_tag_cents=slot.shelf_tag_cents,
                            detected_at=stats.mispricing_at or now,
                        )
                    )
            raw_cells.append((slot, stats))

        def activity(st: _SlotStats) -> float:
            return st.scans + 2.0 * st.picks + 3.0 * st.oos_events

        peak = max((activity(st) for _, st in raw_cells), default=0.0)
        half = HEATMAP_CELL_M / 2
        heatmap = [
            HeatmapCell(
                cell_id=slot.slot_id,
                rect=Rect2D(
                    x=slot.item_position.x - half, y=slot.item_position.y - half, w=HEATMAP_CELL_M, h=HEATMAP_CELL_M
                ),
                slot_id=slot.slot_id,
                aisle_id=slot.aisle_id,
                scans=st.scans,
                picks=st.picks,
                oos_events=st.oos_events,
                value=round(activity(st) / peak, 4) if peak > 0 else 0.0,
            )
            for slot, st in raw_cells
        ]
        near = self._near_expiry(store_id, 3)
        n_slots = len(plan.slots)
        scanned = sum(1 for _, st in raw_cells if st.scans > 0)
        kpis = {
            "slots_total": float(n_slots),
            "on_shelf_availability": round((n_slots - len(oos)) / n_slots, 4) if n_slots else 1.0,
            "out_of_stock_count": float(len(oos)),
            "mispricing_count": float(len(mispricings)),
            "near_expiry_count": float(len(near)),
            "scan_coverage": round(scanned / n_slots, 4) if n_slots else 0.0,
            "picks_total": float(sum(st.picks for _, st in raw_cells)),
            "units_on_shelf": float(sum(s.qty for s in plan.slots)),
        }
        return MerchantDashboard(
            store_id=store_id,
            generated_at=now,
            scans_total=self._scans_total.get(store_id, 0),
            out_of_stock=oos,
            mispricings=mispricings,
            near_expiry=near,
            heatmap=heatmap,
            kpis=kpis,
        )
