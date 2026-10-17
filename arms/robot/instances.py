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
    "slot_instances",
]


def asset_for(products: dict[str, Product] | None, sku: str) -> SimAsset:
    prod = (products or {}).get(sku)
    return prod.asset if prod is not None else SimAsset()


def slot_instances(slot: Slot, asset: SimAsset | None = None) -> list[ItemInstance]:
    """All units currently on ``slot`` (qty from lots when present, else ``slot.qty``)."""
    asset = asset or SimAsset()
    width, depth, height = asset.dims_m
    th = slot.approach_pose.theta
    deep = (math.cos(th), math.sin(th))  # into the shelf
    lateral = (-math.sin(th), math.cos(th))  # left when facing the shelf
    facings = max(slot.facings, 1)

    units: list[tuple[str | None, float | None]] = []
    if slot.lots:
        for lot in slot.lots:
            units += [(lot.lot_id, lot.freshness)] * lot.qty
    else:
        units = [(None, None)] * slot.qty

    out: list[ItemInstance] = []
    base = slot.item_position
    for n, (lot_id, lot_fresh) in enumerate(units):
        col, row = n % facings, n // facings
        off_lat = (col - (facings - 1) / 2.0) * width * 1.1
        off_deep = depth / 2.0 + row * depth * 1.05
        iid = instance_id(slot.slot_id, n)
        fresh = unit_freshness(lot_fresh, iid)
        out.append(
            ItemInstance(
                instance_id=iid,
                sku=slot.sku,
                slot_id=slot.slot_id,
                position=Point3(
                    x=round(base.x + deep[0] * off_deep + lateral[0] * off_lat, 4),
                    y=round(base.y + deep[1] * off_deep + lateral[1] * off_lat, 4),
                    z=round(base.z + height / 2.0, 4),
                ),
                freshness=fresh,
                lot_id=lot_id,
            )
        )
    return out


def build_instances(planogram: Planogram, products: dict[str, Product] | None = None) -> dict[str, list[ItemInstance]]:
    """slot_id -> instances on that slot (empty list for OOS slots)."""
    return {s.slot_id: slot_instances(s, asset_for(products, s.sku)) for s in planogram.slots}


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
