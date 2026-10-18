"""A minimal, deterministic v0.2 world for contract tests: ``MiniCatalog`` (v0.2 CatalogService) + ``MiniRobot`` (v0.2
RobotService) for one small taco store, so the loop fakes can be exercised without any arm code.

The loop fakes (fake_planner / fake_vla / fake_verifier / fake_camera / loop) only talk to the v0.2 Protocols, so in the
pipeline they run unchanged over the real v0.2 fakes (arms.catalog / arms.robot); tests/test_integration_v02.py does
exactly that. Products / prices / brands mirror data/products.yaml (the taco demo SKUs + SALSA-HOT as the neighbour of
the mild salsa for the wrong-item scenario).
"""

import asyncio
import math
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from contracts.cameras import parse_camera_id, robot_camera_id
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
    StoreInfo,
    StoreOffer,
)
from contracts.common import (
    BATTERY_DRAIN_PCT_PER_M,
    BATTERY_MOTION_MIN_PCT,
    ONLINE_STORE_ID,
    Point3,
    Pose2D,
    Rect2D,
    slot_reachable,
)
from contracts.perception import ScanReport
from contracts.planogram import Aisle, Fixture, Planogram, Slot
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus, instance_id, unit_freshness

TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5mo"
    "q6T/2wBDARweHigjKE4rK06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAAwAEADASIAAhEB"
    "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS"
    "0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKz"
    "tLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgEC"
    "BAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpj"
    "ZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6"
    "/9oADAMBAAIRAxEAPwDVooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD/9k="
)
"""A 64x48 grey JPEG: the synthetic frame body (no people in it)."""

STORE_ID = "store_ave"
ROBOT_ID = "rover-ave-1"

_P = [
    # sku, name, brand, category, subcategory, price, size, tags
    ("TORT-CORN", "Corn Tortillas 30 ct", "La Milpa", "bakery", "tortillas", 279, "30 ct", ["tortillas", "taco"]),
    ("BEANS-BLACK", "Black Beans 15 oz", "Casa Verde", "pantry", "canned", 129, "15 oz", ["black beans", "beans"]),
    ("SALSA-MILD", "Mild Chunky Salsa 16 oz", "Sol de Casa", "pantry", "salsa", 349, "16 oz", ["salsa", "mild"]),
    ("SALSA-HOT", "Habanero Fire Salsa 16 oz", "Fuego Ridge", "pantry", "salsa", 499, "16 oz", ["salsa", "hot"]),
    ("BEEF-GROUND", "Ground Beef 85/15 1 lb", "Prairie Ranch", "meat", "beef", 549, "1 lb", ["ground beef", "beef"]),
    (
        "CHEESE-MEX",
        "Mexican Blend Shredded Cheese 8 oz",
        "Cascade Creamery",
        "dairy",
        "cheese",
        379,
        "8 oz",
        ["shredded cheese", "cheese"],
    ),
    ("SOUR-CREAM", "Sour Cream 16 oz", "Cascade Creamery", "dairy", "cultured", 249, "16 oz", ["sour cream"]),
    ("AVOCADO", "Hass Avocado", "Picky Farms", "produce", "fruit", 129, "1 ct", ["avocado"]),
    ("LIMES", "Lime", "Picky Farms", "produce", "fruit", 45, "1 ct", ["limes", "lime"]),
    ("ONION-WHITE", "White Onion", "Picky Farms", "produce", "vegetables", 89, "1 ct", ["onion", "onions"]),
    ("CILANTRO", "Cilantro Bunch", "Picky Farms", "produce", "herbs", 99, "1 bunch", ["cilantro", "herbs"]),
]
_DIETARY = {"BEANS-BLACK": ["vegetarian", "vegan"], "CILANTRO": ["vegetarian", "vegan"]}

# fixture -> (rect, levels z, slot skus at (level, index))
_LAYOUT: dict[str, tuple[str, str, Rect2D, list[float], list[tuple[int, str]]]] = {
    "MINI-F1": (
        "shelf",
        "MINI-A1",
        Rect2D(x=2.0, y=3.0, w=8.0, h=0.6),
        [0.3, 0.8, 1.3],
        [(1, "TORT-CORN"), (1, "BEANS-BLACK"), (1, "SALSA-MILD"), (1, "SALSA-HOT")],
    ),
    "MINI-F2": (
        "fridge",
        "MINI-A2",
        Rect2D(x=2.0, y=6.0, w=6.0, h=0.6),
        [0.6, 1.2],
        [(0, "BEEF-GROUND"), (0, "CHEESE-MEX"), (0, "SOUR-CREAM")],
    ),
    "MINI-F3": (
        "produce_bin",
        "MINI-A2",
        Rect2D(x=8.5, y=6.0, w=3.0, h=0.8),
        [0.9],
        [(0, "AVOCADO"), (0, "LIMES"), (0, "ONION-WHITE"), (0, "CILANTRO")],
    ),
}


def build_products() -> list[Product]:
    out = []
    for n, (sku, name, brand, cat, sub, price, size, tags) in enumerate(_P):
        out.append(
            Product(
                sku=sku,
                name=name,
                brand=brand,
                category=cat,
                subcategory=sub,
                price_cents=price,
                size=size,
                tags=tags,
                dietary=_DIETARY.get(sku, []),
                barcode=f"20000000{n:05d}",
                perishable=cat in ("produce", "meat", "dairy"),
            )
        )
    return out


def build_planogram(qty: int = 8) -> Planogram:
    fixtures: list[Fixture] = []
    slots: list[Slot] = []
    for fid, (kind, aisle, rect, levels, entries) in _LAYOUT.items():
        fixtures.append(
            Fixture(
                fixture_id=fid,
                kind=kind,  # type: ignore[arg-type]
                aisle_id=aisle,
                rect=rect,
                yaw=-math.pi / 2,
                levels=len(levels),
                level_z_m=levels,
            )
        )
        for idx, (level, sku) in enumerate(entries):
            x = round(rect.x + 0.4 + idx * 0.7, 3)
            n = 10 if sku in ("AVOCADO", "LIMES") else qty
            fresh = 0.9 if kind == "produce_bin" else None
            slot_id = f"{fid}-{level}-{idx:02d}"
            slots.append(
                Slot(
                    slot_id=slot_id,
                    fixture_id=fid,
                    aisle_id=aisle,
                    sku=sku,
                    level=level,
                    index=idx,
                    item_position=Point3(x=x, y=rect.y, z=levels[level]),
                    approach_pose=Pose2D(x=x, y=round(rect.y - 0.38, 3), theta=math.pi / 2),
                    capacity=12,
                    qty=n,
                    lots=[Lot(lot_id=f"{slot_id}-L1", qty=n, freshness=fresh)],
                )
            )
    return Planogram(
        store_id=STORE_ID,
        name="Picky Ave (mini)",
        bounds=Rect2D(x=0, y=0, w=12.5, h=8.0),
        entrance=Pose2D(x=0.8, y=0.8, theta=math.pi / 2),
        checkout=Pose2D(x=11.5, y=1.0, theta=-math.pi / 2),
        robot_home=Pose2D(x=1.2, y=1.0, theta=math.pi / 2),
        fixtures=fixtures,
        aisles=[
            Aisle(aisle_id="MINI-A1", label="Aisle 1 (pantry)", rect=Rect2D(x=1.0, y=1.5, w=10.5, h=1.5)),
            Aisle(aisle_id="MINI-A2", label="Aisle 2 (fresh)", rect=Rect2D(x=1.0, y=4.0, w=11.0, h=2.0)),
        ],
        slots=slots,
    )


def _tokens(text: str) -> set[str]:
    out = set()
    for t in re.findall(r"[a-z]+", text.lower()):
        out.add(t[:-1] if len(t) > 3 and t.endswith("s") else t)
    return out


class MiniCatalog:
    """v0.2 CatalogService over one in-memory store (+ 'online'). Unused staff/stock-management methods are minimal."""

    def __init__(self, services: Services | None = None) -> None:
        self.services = services
        self.products = {p.sku: p for p in build_products()}
        self.planogram = build_planogram()
        self.picks: list[tuple[str, str, int]] = []
        self.scans: list[ScanReport] = []

    async def list_stores(self) -> list[StoreInfo]:
        return [
            StoreInfo(store_id=STORE_ID, name=self.planogram.name, width_m=12.5, depth_m=8.0),
            StoreInfo(
                store_id=ONLINE_STORE_ID,
                name="Picky Online",
                kind="online",
                has_planogram=False,
                shipping_cents=599,
                free_shipping_over_cents=3500,
                default_eta_hours=24,
            ),
        ]

    async def get_planogram(self, store_id: str) -> Planogram:
        if store_id != STORE_ID:
            raise KeyError(store_id)
        return self.planogram.model_copy(deep=True)

    def _location(self, slot: Slot) -> ProductLocation:
        aisle = self.planogram.aisle(slot.aisle_id or "")
        return ProductLocation(
            store_id=STORE_ID,
            sku=slot.sku,
            slot_id=slot.slot_id,
            aisle_id=slot.aisle_id,
            aisle_label=aisle.label if aisle else None,
            fixture_id=slot.fixture_id,
            level=slot.level,
            index=slot.index,
            item_position=slot.item_position,
            approach_pose=slot.approach_pose,
            qty=slot.qty,
        )

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        want = _tokens(query.text)
        stores = query.store_ids if query.store_ids is not None else [STORE_ID, ONLINE_STORE_ID]
        hits: list[SearchHit] = []
        for p in self.products.values():
            hay = _tokens(
                " ".join([p.sku.replace("-", " "), p.name, p.brand or "", p.category, p.subcategory or "", *p.tags])
            )
            score = len(want & hay) if want else 1
            if score == 0 or (query.category and query.category not in (p.category, p.subcategory)):
                continue
            if any(d not in p.dietary for d in query.dietary):
                continue
            offers = [o for o in await self.get_offers(p.sku, stores)]
            if query.in_stock_only and not any(o.in_stock for o in offers):
                continue
            hits.append(SearchHit(product=p, offers=offers, score=float(score)))
        hits.sort(key=lambda h: (-h.score, h.product.price_cents, h.product.sku))
        return hits[: query.limit]

    async def get_product(self, sku: str) -> Product | None:
        return self.products.get(sku)

    async def get_locations(self, sku: str, store_id: str | None = None) -> list[ProductLocation]:
        if store_id not in (None, STORE_ID):
            return []
        return [self._location(s) for s in self.planogram.slots_for(sku)]

    async def get_offers(self, sku: str, store_ids: list[str] | None = None) -> list[StoreOffer]:
        p = self.products.get(sku)
        if p is None:
            return []
        out: list[StoreOffer] = []
        for sid in store_ids or [STORE_ID, ONLINE_STORE_ID]:
            if sid == STORE_ID:
                for s in self.planogram.slots_for(sku):
                    out.append(
                        StoreOffer(
                            store_id=sid,
                            sku=sku,
                            price_cents=s.price_cents or p.price_cents,
                            qty=s.qty,
                            location=self._location(s),
                            lots=list(s.lots),
                        )
                    )
            elif sid == ONLINE_STORE_ID:
                out.append(StoreOffer(store_id=sid, sku=sku, price_cents=p.price_cents + 20, qty=99, eta_hours=24))
        return out

    async def get_deals(self, store_id: str) -> list[Deal]:
        return []

    async def get_near_expiry(self, store_id: str, within_days: int = 3) -> list[NearExpiryItem]:
        return []

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        self.scans.append(report)
        slot = self.planogram.slot(report.slot_id or "")
        if slot is None:
            return ScanApplyResult(store_id=report.store_id, slot_id=report.slot_id, applied=False)
        before = slot.qty
        if report.count_is_exact:
            slot.qty = report.observed_count
            slot.lots = [
                Lot(
                    lot_id=slot.lots[0].lot_id if slot.lots else f"{slot.slot_id}-L1",
                    qty=slot.qty,
                    freshness=slot.lots[0].freshness if slot.lots else None,
                )
            ]
        elif report.observed_count == 0:
            slot.qty = 0
        return ScanApplyResult(
            store_id=report.store_id,
            slot_id=slot.slot_id,
            qty_before=before,
            qty_after=slot.qty,
            out_of_stock=slot.qty == 0,
        )

    async def record_pick(
        self,
        store_id: str,
        slot_id: str,
        sku: str,
        qty: int = 1,
        trip_id: str | None = None,
        robot_id: str | None = None,
        lot_id: str | None = None,
    ) -> ScanApplyResult:
        slot = self.planogram.slot(slot_id)
        if slot is None:
            raise KeyError(slot_id)
        before = slot.qty
        slot.qty = max(0, slot.qty - qty)
        if slot.lots:
            lot = next((lt for lt in slot.lots if lt.lot_id == lot_id), slot.lots[0])
            lot.qty = max(0, lot.qty - qty)
        self.picks.append((slot_id, sku, qty))
        return ScanApplyResult(
            store_id=store_id, slot_id=slot_id, qty_before=before, qty_after=slot.qty, out_of_stock=slot.qty == 0
        )

    async def merchant_dashboard(self, store_id: str) -> MerchantDashboard:
        return MerchantDashboard(store_id=store_id)

    async def list_categories(self, store_id: str | None = None) -> list[CategoryInfo]:
        return []

    async def category_products(self, category_id: str, store_id: str | None = None) -> list[SearchHit]:
        return []

    async def stock_levels(
        self, store_id: str, status: Any = None, category: Any = None, q: Any = None
    ) -> list[StockLevel]:
        return []

    async def stock_alerts(self, store_id: str) -> list[StockAlert]:
        return []

    async def restock(self, store_id: str, request: RestockRequest) -> StockChangeResult:
        raise ValueError("MiniCatalog: restock is not part of the mini world")

    async def adjust_stock(self, store_id: str, adjustment: StockAdjustment) -> StockChangeResult:
        raise ValueError("MiniCatalog: adjust_stock is not part of the mini world")

    async def stock_history(
        self,
        store_id: str,
        sku: Any = None,
        limit: int = 100,
        before: Any = None,
        reason: Any = None,
        actor: Any = None,
    ) -> list[StockMovement]:
        return []


class _Rover:
    def __init__(self, robot_id: str, store_id: str, pose: Pose2D, capacity: int) -> None:
        self.robot_id = robot_id
        self.store_id = store_id
        self.pose = pose
        self.status = RobotStatus.IDLE
        self.holding: ItemInstance | None = None
        self.basket: list[ItemInstance] = []
        self.battery = 90.0
        self.lift = 0.3
        self.capacity = capacity
        self.busy = False
        self.stop_evt = asyncio.Event()


async def _empty() -> AsyncIterator[Any]:
    if False:  # pragma: no cover
        yield None


class MiniRobot:
    """v0.2 RobotService: teleporting 2D rover(s) over ``services.catalog``'s planogram (read at call time).
    ``motion_s`` = seconds each navigate takes (cancellation / timeout tests); 0 = instant."""

    def __init__(
        self,
        services: Services,
        *,
        motion_s: float = 0.0,
        basket_capacity: int = 20,
        robot_ids: tuple[str, ...] = (ROBOT_ID,),
    ) -> None:
        self.services = services
        self.robot_ids = robot_ids
        self.motion_s = motion_s
        self.basket_capacity = basket_capacity
        self.calls: Counter[str] = Counter()
        self._rovers: dict[str, _Rover] = {}
        self._shelves: dict[str, list[ItemInstance]] | None = None
        self._slots: dict[str, Slot] = {}

    async def _load(self) -> None:
        if self._shelves is not None:
            return
        plan = await self.services.require("catalog").get_planogram(STORE_ID)
        self._shelves = {}
        for s in plan.slots:
            self._slots[s.slot_id] = s
            units = []
            for n in range(1, s.qty + 1):
                iid = instance_id(s.slot_id, n)
                lot = s.lots[0] if s.lots else None
                units.append(
                    ItemInstance(
                        instance_id=iid,
                        sku=s.sku,
                        store_id=STORE_ID,
                        slot_id=s.slot_id,
                        position=Point3(
                            x=s.item_position.x, y=s.item_position.y + 0.05 * ((n - 1) // 2), z=s.item_position.z + 0.05
                        ),
                        freshness=unit_freshness(lot.freshness if lot else None, iid),
                        lot_id=lot.lot_id if lot else None,
                    )
                )
            self._shelves[s.slot_id] = units
        home = plan.robot_home or plan.entrance
        for n, rid in enumerate(self.robot_ids):  # extra rovers park 0.7 m apart along +x (multi-robot race tests)
            pose = home.model_copy(update={"x": round(home.x + 0.7 * n, 3)})
            self._rovers[rid] = _Rover(rid, STORE_ID, pose, self.basket_capacity)

    async def _rover(self, robot_id: str) -> _Rover:
        await self._load()
        rv = self._rovers.get(robot_id)
        if rv is None:
            raise RobotError("unknown_robot", robot_id)
        return rv

    def _snapshot(self, rv: _Rover) -> RobotState:
        return RobotState(
            robot_id=rv.robot_id,
            store_id=rv.store_id,
            pose=rv.pose,
            status=rv.status,
            holding=rv.holding.instance_id if rv.holding else None,
            basket=[u.instance_id for u in rv.basket],
            basket_skus=[u.sku for u in rv.basket],
            lift_height_m=rv.lift,
            battery_pct=round(rv.battery, 2),
            backend="fake",
        )

    def _motion(self, rv: _Rover) -> None:
        if rv.busy:
            raise RobotError("busy", f"{rv.robot_id} is already moving")

    async def list_robots(self) -> list[RobotState]:
        await self._load()
        return [self._snapshot(rv) for rv in self._rovers.values()]

    async def state(self, robot_id: str) -> RobotState:
        return self._snapshot(await self._rover(robot_id))

    async def reset(self, robot_id: str, store_id: str | None = None) -> RobotState:
        rv = await self._rover(robot_id)
        rv.holding, rv.basket = None, []
        return self._snapshot(rv)

    async def navigate(self, robot_id: str, waypoints: list[Pose2D]) -> RobotState:
        self.calls["navigate"] += 1
        rv = await self._rover(robot_id)
        self._motion(rv)
        if rv.battery < BATTERY_MOTION_MIN_PCT:
            raise RobotError("battery_low", f"{rv.battery:.0f}%")
        rv.busy, rv.status = True, RobotStatus.NAVIGATING
        rv.stop_evt.clear()
        try:
            if self.motion_s > 0:
                try:
                    await asyncio.wait_for(rv.stop_evt.wait(), timeout=self.motion_s)
                    raise RobotError("cancelled", "stopped")
                except TimeoutError:
                    pass
            dist = 0.0
            prev = rv.pose
            for wp in waypoints:
                dist += prev.distance_to(wp)
                prev = wp
            rv.pose = waypoints[-1]
            rv.battery = max(0.0, rv.battery - dist * BATTERY_DRAIN_PCT_PER_M)
        finally:
            rv.busy, rv.status = False, RobotStatus.IDLE
        return self._snapshot(rv)

    async def stop(self, robot_id: str) -> RobotState:
        self.calls["stop"] += 1
        rv = await self._rover(robot_id)
        rv.stop_evt.set()
        return self._snapshot(rv)

    def _frame(self, rv: _Rover, kind: str, instances: list[ItemInstance], slot_id: str | None = None) -> CameraFrame:
        self.calls["frame"] += 1
        return CameraFrame(
            frame_id=f"mini-{rv.robot_id}-{kind}-{self.calls['frame']:05d}",
            camera=kind,  # type: ignore[arg-type]
            camera_id=robot_camera_id(rv.robot_id, kind),
            robot_id=rv.robot_id,
            store_id=rv.store_id,
            jpeg_b64=TINY_JPEG_B64,
            w=64,
            h=48,
            blurred=False,
            slot_id=slot_id,
            pose=rv.pose,
            instances=[u.model_copy() for u in instances],
        )

    def facing_slot(self, pose: Pose2D, max_dist_m: float = 0.15) -> str | None:
        best = min(self._slots.values(), key=lambda s: pose.distance_to(s.approach_pose), default=None)
        return best.slot_id if best is not None and pose.distance_to(best.approach_pose) <= max_dist_m else None

    async def scan(self, robot_id: str, slot_id: str) -> CameraFrame:
        self.calls["scan"] += 1
        rv = await self._rover(robot_id)
        if slot_id not in self._slots:
            raise RobotError("unknown_slot", slot_id)
        assert self._shelves is not None
        return self._frame(rv, "front", self._shelves[slot_id], slot_id)

    async def capture(self, robot_id: str, camera: str = "front") -> CameraFrame:
        rv = await self._rover(robot_id)
        assert self._shelves is not None
        kind = "front" if camera == "head" else camera
        if kind == "wrist":
            return self._frame(rv, kind, [rv.holding] if rv.holding else [])
        if kind == "basket":
            return self._frame(rv, kind, rv.basket)
        sid = self.facing_slot(rv.pose)
        return self._frame(rv, "front", self._shelves.get(sid, []) if sid else [], sid)

    async def capture_camera(self, camera_id: str) -> CameraFrame:
        owner, kind = parse_camera_id(camera_id)
        if kind == "ceiling":
            rv = await self._rover(ROBOT_ID)
            return self._frame(rv, "ceiling", [])
        return await self.capture(owner, kind)

    async def list_instances(self, robot_id: str, slot_id: str | None = None) -> list[ItemInstance]:
        rv = await self._rover(robot_id)
        assert self._shelves is not None
        if slot_id is not None:
            if slot_id not in self._shelves:
                raise RobotError("unknown_slot", slot_id)
            return [u.model_copy() for u in self._shelves[slot_id]]
        out = [u.model_copy() for units in self._shelves.values() for u in units]
        if rv.holding is not None:
            out.append(rv.holding.model_copy())
        return out + [u.model_copy() for u in rv.basket]

    async def pick(self, robot_id: str, slot_id: str, sku: str, instance_id: str | None = None) -> ItemInstance:
        self.calls["pick"] += 1
        rv = await self._rover(robot_id)
        assert self._shelves is not None
        self._motion(rv)
        slot = self._slots.get(slot_id)
        if slot is None:
            raise RobotError("unknown_slot", slot_id)
        if slot.sku != sku:
            raise RobotError("no_instance", f"slot {slot_id} holds {slot.sku}, not {sku}")
        if rv.holding is not None:
            raise RobotError("grasp_failed", f"gripper already holds {rv.holding.instance_id}")
        if not slot_reachable(rv.pose, slot.approach_pose, slot.item_position):
            raise RobotError("grasp_failed", f"{slot_id} out of reach")
        units = self._shelves[slot_id]
        unit = next((u for u in units if u.instance_id == instance_id), None) if instance_id else None
        unit = unit or (units[0] if units else None)
        if unit is None:
            raise RobotError("no_instance", f"{slot_id} is empty")
        units.remove(unit)
        rv.holding = unit
        rv.lift = min(1.2, max(0.2, slot.item_position.z + 0.05))
        return unit.model_copy()

    async def place_in_basket(self, robot_id: str) -> RobotState:
        self.calls["place_in_basket"] += 1
        rv = await self._rover(robot_id)
        self._motion(rv)
        if rv.holding is None:
            raise RobotError("no_instance", "gripper is empty")
        if len(rv.basket) >= rv.capacity:
            raise RobotError("grasp_failed", "basket full")
        rv.basket.append(rv.holding.model_copy(update={"in_basket": True}))
        rv.holding = None
        rv.lift = 0.3
        return self._snapshot(rv)

    async def unload_basket(self, robot_id: str) -> list[ItemInstance]:
        self.calls["unload_basket"] += 1
        rv = await self._rover(robot_id)
        out, rv.basket = rv.basket, []
        return out

    async def sync_stock(self, store_id: str, slot_ids: list[str] | None = None) -> None:
        return None

    def frames(self, camera_id: str | None = None, robot_id: str | None = None) -> AsyncIterator[CameraFrame]:
        return _empty()

    def states(self, robot_id: str | None = None) -> AsyncIterator[RobotState]:
        return _empty()
