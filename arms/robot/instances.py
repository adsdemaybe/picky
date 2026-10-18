"""Ground-truth item instances generated from a planogram (deterministic). Used by the robot fake. Instance ids,
per-unit freshness and the basket drop pose come from ``contracts.sim`` (``instance_id``, ``unit_freshness``,
``BASKET_OFFSET_LOCAL``), the same helpers the Isaac scene builder (``arms/sim/scene_spec.py``) uses, so a mid-trip
fallback from the sim robot to this fake keeps PickChoice ids, the top-3 ranking and the basket layout.

Instance id: ``f"{slot_id}#{n:02d}"`` with n counting units of the slot front-to-back, lot by lot (planogram order).
Placement (contracts/planogram.py rules): the front unit's base sits at ``slot.item_position``; units fill
``slot.facings`` columns across the pick face and then rows deeper into the shelf; ``ItemInstance.position`` is the
item CENTER (z + height/2). The pick face points along ``approach_pose.theta + pi``, so "deeper" = ``+theta``.
"""

import math

from contracts.catalog import Product, SimAsset
from contracts.common import BBox, Point3, Pose2D
from contracts.planogram import Planogram, Slot
from contracts.sim import BASKET_OFFSET_LOCAL, UNIT_FRESHNESS_JITTER, ItemInstance, instance_id, unit_freshness

FRESHNESS_JITTER = UNIT_FRESHNESS_JITTER
"""Per-unit deterministic spread around the lot freshness (``contracts.sim.UNIT_FRESHNESS_JITTER``)."""

__all__ = [
    "BASKET_OFFSET_LOCAL",
    "FRESHNESS_JITTER",
    "asset_for",
    "basket_position",
    "build_instances",
    "instance_id",
    "layout_bboxes",
    "resync_slot",
    "slot_instances",
    "unit_position",
]


def asset_for(products: dict[str, Product] | None, sku: str) -> SimAsset:
    prod = (products or {}).get(sku)
    return prod.asset if prod is not None else SimAsset()


def unit_position(slot: Slot, asset: SimAsset, k: int) -> Point3:
    """Center of the ``k``-th unit position of a slot (k=0 front-left column; columns across, then rows deeper)."""
    width, depth, height = asset.dims_m
    th = slot.approach_pose.theta
    deep = (math.cos(th), math.sin(th))  # into the shelf
    lateral = (-math.sin(th), math.cos(th))  # left when facing the shelf
    facings = max(slot.facings, 1)
    col, row = k % facings, k // facings
    off_lat = (col - (facings - 1) / 2.0) * width * 1.1
    off_deep = depth / 2.0 + row * depth * 1.05
    base = slot.item_position
    return Point3(
        x=round(base.x + deep[0] * off_deep + lateral[0] * off_lat, 4),
        y=round(base.y + deep[1] * off_deep + lateral[1] * off_lat, 4),
        z=round(base.z + height / 2.0, 4),
    )


def slot_instances(slot: Slot, asset: SimAsset | None = None, store_id: str | None = None) -> list[ItemInstance]:
    """All units currently on ``slot`` (qty from lots when present, else ``slot.qty``)."""
    asset = asset or SimAsset()
    units: list[tuple[str | None, float | None]] = []
    if slot.lots:
        for lot in slot.lots:
            units += [(lot.lot_id, lot.freshness)] * lot.qty
    else:
        units = [(None, None)] * slot.qty

    out: list[ItemInstance] = []
    for n, (lot_id, lot_fresh) in enumerate(units):
        iid = instance_id(slot.slot_id, n)
        out.append(
            ItemInstance(
                instance_id=iid,
                sku=slot.sku,
                store_id=store_id,
                slot_id=slot.slot_id,
                position=unit_position(slot, asset, n),
                freshness=unit_freshness(lot_fresh, iid),
                lot_id=lot_id,
            )
        )
    return out


def build_instances(planogram: Planogram, products: dict[str, Product] | None = None) -> dict[str, list[ItemInstance]]:
    """slot_id -> instances on that slot (empty list for OOS slots); every unit carries the planogram's store_id."""
    return {s.slot_id: slot_instances(s, asset_for(products, s.sku), planogram.store_id) for s in planogram.slots}


def _n_of(inst: ItemInstance) -> int:
    try:
        return int(inst.instance_id.rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return 0


def resync_slot(
    units: list[ItemInstance], slot: Slot, asset: SimAsset | None, next_n: int, store_id: str | None = None
) -> tuple[list[ItemInstance], int]:
    """Make a slot hold exactly what the catalog says (``slot.qty`` / ``slot.lots``), deterministically.

    Existing units are kept where possible; surplus units are removed highest ``n`` first (back rows) and missing
    units are spawned with fresh ids ``instance_id(slot_id, next_n)``, ``next_n + 1``, ... (never reusing an id that
    may still sit in a basket). Lots: per-lot counts follow ``slot.lots`` (planogram order) when present, else only
    the total follows ``slot.qty``. Units are re-laid front-to-back. Returns (units, new next_n).
    """
    asset = asset or SimAsset()
    keep = sorted((u for u in units if not u.in_basket), key=_n_of)
    spawn: list[tuple[str | None, float | None]] = []
    if slot.lots:
        targets = {lot.lot_id: lot for lot in slot.lots}
        kept: list[ItemInstance] = []
        for lot in slot.lots:
            mine = [u for u in keep if u.lot_id == lot.lot_id]
            kept += mine[: lot.qty]
            spawn += [(lot.lot_id, lot.freshness)] * max(0, lot.qty - len(mine))
        keep = sorted((u for u in kept if u.lot_id in targets), key=_n_of)
    else:
        keep = keep[: slot.qty]
        spawn = [(None, None)] * max(0, slot.qty - len(keep))
    for lot_id, fresh in spawn:
        iid = instance_id(slot.slot_id, next_n)
        next_n += 1
        keep.append(
            ItemInstance(
                instance_id=iid,
                sku=slot.sku,
                store_id=store_id,
                slot_id=slot.slot_id,
                position=Point3(x=0.0, y=0.0, z=0.0),
                freshness=unit_freshness(fresh, iid),
                lot_id=lot_id,
            )
        )
    out = [
        u.model_copy(update={"position": unit_position(slot, asset, k), "sku": slot.sku}) for k, u in enumerate(keep)
    ]
    return out, next_n


def basket_position(pose: Pose2D, index: int) -> Point3:
    """World position of the ``index``-th basket item for a robot at ``pose`` (small grid on the basket floor)."""
    lx = BASKET_OFFSET_LOCAL[0] + (index % 3 - 1) * 0.09
    ly = BASKET_OFFSET_LOCAL[1] + ((index // 3) % 3 - 1) * 0.09
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    return Point3(
        x=round(pose.x + c * lx - s * ly, 4),
        y=round(pose.y + s * lx + c * ly, 4),
        z=BASKET_OFFSET_LOCAL[2] + 0.05 * (index // 9),
    )


def layout_bboxes(instances: list[ItemInstance], w: int, h: int, facings: int = 1) -> dict[str, BBox]:
    """Synthetic pixel boxes for ``instances`` in a w x h frame: one column per facing, deeper rows drawn smaller and
    higher (cheap perspective). Deterministic; only meant for crops/thumbnails in the fake."""
    out: dict[str, BBox] = {}
    if not instances:
        return out
    facings = max(1, min(facings, len(instances)))
    col_w = w / (facings + 1)
    for n, inst in enumerate(instances):
        col, row = n % facings, n // facings
        scale = 1.0 / (1.0 + 0.35 * row)
        bw, bh = col_w * 0.8 * scale, h * 0.45 * scale
        cx = col_w * (col + 1)
        bottom = h * 0.95 - row * h * 0.12
        out[inst.instance_id] = BBox(
            x=round(max(0.0, cx - bw / 2), 1),
            y=round(max(0.0, bottom - bh), 1),
            w=round(min(bw, w), 1),
            h=round(min(bh, h), 1),
        )
    return out
