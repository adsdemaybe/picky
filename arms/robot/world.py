"""Per-store ground truth for the 2D fake: the live planogram, products and every unit on every shelf.

One ``StoreWorld`` per physical store, shared by every robot in it (a pick by Pico removes the unit for Rhea too).
Built from ``services.catalog.get_planogram`` + ``get_product`` at call time, else ``utils.data``; instances are
deterministic (``arms.robot.instances``), so a fallback from Isaac lands on the same ids.
"""

from dataclasses import dataclass, field

from arms.robot.instances import _n_of, asset_for, build_instances, resync_slot
from contracts.catalog import Lot, Product, SimAsset
from contracts.common import Sku, SlotId, StoreId
from contracts.planogram import Planogram, Slot
from contracts.services import Services
from contracts.sim import ItemInstance, RobotError, planogram_hash
from utils.log import get_logger

log = get_logger(__name__)


@dataclass
class StoreWorld:
    plan: Planogram
    products: dict[Sku, Product]
    shelves: dict[SlotId, list[ItemInstance]] = field(default_factory=dict)
    next_n: dict[SlotId, int] = field(default_factory=dict)
    data_hash: str = ""

    @classmethod
    def build(cls, plan: Planogram, products: dict[Sku, Product]) -> "StoreWorld":
        w = cls(plan=plan, products=products)
        w.rebuild()
        return w

    @property
    def store_id(self) -> StoreId:
        return self.plan.store_id

    def rebuild(self) -> None:
        self.shelves = build_instances(self.plan, self.products)
        self.next_n = {sid: len(units) for sid, units in self.shelves.items()}
        self.data_hash = planogram_hash(self.plan)

    def slot(self, slot_id: SlotId | None) -> Slot:
        slot = self.plan.slot(slot_id) if slot_id else None
        if slot is None:
            raise RobotError("unknown_slot", f"unknown slot {slot_id!r} in {self.store_id}")
        return slot

    def asset(self, sku: Sku) -> SimAsset:
        return asset_for(self.products, sku)

    def product(self, sku: Sku) -> Product | None:
        return self.products.get(sku)

    def units(self, slot_id: SlotId) -> list[ItemInstance]:
        return self.shelves.setdefault(slot_id, [])

    def take(self, slot_id: SlotId, iid: str) -> ItemInstance | None:
        """Remove a unit from its shelf (pick / adopting an Isaac basket); None if it is not there."""
        units = self.units(slot_id)
        for i, u in enumerate(units):
            if u.instance_id == iid:
                return units.pop(i)
        return None

    def find(self, iid: str) -> ItemInstance | None:
        slot_id = iid.rsplit("#", 1)[0]
        return next((u for u in self.shelves.get(slot_id, []) if u.instance_id == iid), None)

    def price_tag_cents(self, slot: Slot) -> int | None:
        """What the physical shelf tag shows (mispricings show the wrong tag)."""
        if slot.shelf_tag_cents is not None:
            return slot.shelf_tag_cents
        if slot.deal is not None:
            return slot.deal.price_cents
        if slot.price_cents is not None:
            return slot.price_cents
        prod = self.products.get(slot.sku)
        return prod.price_cents if prod else None

    def set_slot_stock(self, slot_id: SlotId, qty: int, lots: list[Lot] | None = None) -> list[ItemInstance]:
        """SetSlotStockCmd semantics: the slot holds exactly ``qty`` units (lots in planogram order)."""
        slot = self.slot(slot_id).model_copy(update={"qty": qty, "lots": list(lots or [])})
        units, nxt = resync_slot(self.units(slot_id), slot, self.asset(slot.sku), self._next(slot_id), self.store_id)
        self.shelves[slot_id], self.next_n[slot_id] = units, nxt
        return list(units)

    def _next(self, slot_id: SlotId) -> int:
        return max(self.next_n.get(slot_id, 0), max((_n_of(u) + 1 for u in self.units(slot_id)), default=0))

    def apply_planogram(self, plan: Planogram, slot_ids: list[SlotId] | None = None) -> list[SlotId]:
        """Adopt a newer planogram from the catalog (stock / prices) and resync the given slots (None = all).

        A changed LAYOUT (planogram_hash) rebuilds every shelf. Returns the slot ids whose units changed."""
        if planogram_hash(plan) != self.data_hash:
            log.info("store %s layout changed: rebuilding the fake scene", plan.store_id)
            self.plan = plan
            self.rebuild()
            return [s.slot_id for s in plan.slots]
        self.plan = plan
        wanted = set(slot_ids) if slot_ids is not None else None
        changed: list[SlotId] = []
        for slot in plan.slots:
            if wanted is not None and slot.slot_id not in wanted:
                continue
            before = [u.instance_id for u in self.units(slot.slot_id)]
            units, nxt = resync_slot(
                self.units(slot.slot_id), slot, self.asset(slot.sku), self._next(slot.slot_id), plan.store_id
            )
            self.shelves[slot.slot_id] = units
            self.next_n[slot.slot_id] = nxt
            if [u.instance_id for u in units] != before:
                changed.append(slot.slot_id)
        return changed


async def load_scene(services: Services, store_id: StoreId) -> tuple[Planogram, dict[Sku, Product]]:
    """Live planogram + products: catalog first (call time), then data/. RobotError('not_ready') if unknown."""
    catalog = services.catalog
    if catalog is not None:
        try:
            plan = await catalog.get_planogram(store_id)
            products: dict[Sku, Product] = {}
            for sku in sorted({s.sku for s in plan.slots}):
                try:
                    prod = await catalog.get_product(sku)
                except KeyError:
                    prod = None
                if prod is not None:
                    products[sku] = prod
            return plan, products
        except KeyError as exc:
            raise RobotError("not_ready", f"unknown store {store_id!r}") from exc
        except Exception as exc:
            log.warning("catalog unavailable for the robot scene (%s); falling back to data/", exc)
    from utils.data import DataError, load_planogram, load_products

    try:
        plan = load_planogram(store_id)
    except (DataError, KeyError, FileNotFoundError) as exc:
        raise RobotError("not_ready", f"unknown store {store_id!r}: {exc}") from exc
    try:
        all_products = load_products()
    except DataError:
        all_products = {}
    skus = {s.sku for s in plan.slots}
    return plan, {k: v for k, v in all_products.items() if k in skus}


async def live_planogram(services: Services, store_id: StoreId) -> Planogram:
    catalog = services.catalog
    if catalog is not None:
        try:
            return await catalog.get_planogram(store_id)
        except KeyError as exc:
            raise RobotError("not_ready", f"unknown store {store_id!r}") from exc
        except Exception as exc:
            log.warning("catalog get_planogram failed (%s); using data/", exc)
    from utils.data import DataError, load_planogram

    try:
        return load_planogram(store_id)
    except (DataError, KeyError, FileNotFoundError) as exc:
        raise RobotError("not_ready", f"unknown store {store_id!r}: {exc}") from exc


__all__ = ["StoreWorld", "live_planogram", "load_scene"]
