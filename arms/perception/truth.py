"""Ground truth for the verifier when ``services.truth`` (the robot arm's ``TruthSource``) is not wired.

The robot arm owns ``TruthSource`` (2D world + fault overlay). This fallback rebuilds the same ``TruthSnapshot`` from
the v0.2 Protocols only (``robot.state`` + ``robot.list_instances`` + ``catalog.get_planogram`` / ``get_product``,
plus the fault overlay when ``services.faults`` carries one), so the evidence ring and the verifiers keep working in a
pipeline where the robot arm has not exported ``create_truth`` yet. ``truth_for`` prefers the wired source.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from contracts.cameras import robot_camera_id
from contracts.catalog import Product
from contracts.common import Pose2D, RobotId, SlotId
from contracts.evidence import SlotTruth, TruthSnapshot
from contracts.planogram import Slot
from contracts.services import Services
from contracts.sim import ItemInstance

FACING_TOLERANCE_M = 0.15
"""The front camera 'sees' every slot whose approach pose is within this distance of the robot (stacked shelf levels
share one approach pose)."""


def product_label(product: Product | None, sku: str) -> str:
    """Readable label text: '<brand> <name>' (the planner's TargetSpec.description uses the same words)."""
    if product is None:
        return sku
    return f"{product.brand or ''} {product.name}".strip()


class LocalTruth:
    """``TruthSource.truth`` over v0.2 Protocols (no ``ground_truth(frame_id)``: frames belong to the evidence ring)."""

    def __init__(self, services: Services) -> None:
        self.s = services
        self._geom: dict[str, dict[SlotId, Slot]] = {}
        self._products: dict[str, Product | None] = {}

    async def slots(self, store_id: str) -> dict[SlotId, Slot]:
        if store_id not in self._geom:
            catalog = self.s.catalog
            if catalog is None or not store_id:
                return {}
            plan = await catalog.get_planogram(store_id)
            self._geom[store_id] = {s.slot_id: s for s in plan.slots}
        return self._geom[store_id]

    async def product(self, sku: str) -> Product | None:
        if sku not in self._products:
            catalog = self.s.catalog
            try:
                self._products[sku] = await catalog.get_product(sku) if catalog is not None else None
            except Exception:  # noqa: BLE001 - a label falls back to the SKU
                self._products[sku] = None
        return self._products[sku]

    def _overlay(self) -> Any:
        return getattr(getattr(self.s, "faults", None), "overlay", None)

    async def label(self, unit: ItemInstance) -> str:
        overlay = self._overlay()
        mislabels = getattr(overlay, "mislabels", None) or {}
        if unit.instance_id in mislabels:
            return str(mislabels[unit.instance_id])
        return product_label(await self.product(unit.sku), unit.sku)

    @staticmethod
    def facing(slots: dict[SlotId, Slot], pose: Pose2D) -> list[SlotId]:
        return sorted(sid for sid, s in slots.items() if pose.distance_to(s.approach_pose) <= FACING_TOLERANCE_M)

    async def truth(self, robot_id: RobotId, slot_ids: Sequence[SlotId] = ()) -> TruthSnapshot:
        robot = self.s.require("robot")
        st = await robot.state(robot_id)
        units = await robot.list_instances(robot_id)
        by_id = {u.instance_id: u for u in units}
        holding = by_id.get(st.holding) if st.holding else None
        basket = [by_id[i] for i in st.basket if i in by_id] or [u for u in units if u.in_basket]
        overlay = self._overlay()
        virtual = getattr(overlay, "virtual_holding", None) or {}
        if robot_id in virtual:
            holding = virtual[robot_id]
        store_id = st.store_id or ""
        geom = await self.slots(store_id)
        facing = self.facing(geom, st.pose)
        wanted = [sid for sid in [*slot_ids, *facing] if sid and sid in geom]
        taken = {st.holding, *st.basket}
        slots: dict[SlotId, SlotTruth] = {}
        for sid in dict.fromkeys(wanted):
            g = geom[sid]
            p = await self.product(g.sku)
            slots[sid] = SlotTruth(
                slot_id=sid,
                sku=g.sku,
                approach_pose=g.approach_pose,
                item_position=g.item_position,
                instances=[u for u in units if u.slot_id == sid and not u.in_basket and u.instance_id not in taken],
                shelf_tag_cents=g.shelf_tag_cents or g.price_cents or (p.price_cents if p else None),
            )
        labels: dict[str, str] = {}
        for unit in [*(u for s in slots.values() for u in s.instances), *([holding] if holding else []), *basket]:
            labels[unit.instance_id] = await self.label(unit)
        return TruthSnapshot(
            robot_id=robot_id,
            store_id=store_id,
            pose=st.pose,
            battery_pct=st.battery_pct,
            holding=holding,
            basket=basket,
            slots=slots,
            visible_slots={robot_camera_id(robot_id, "front"): facing},
            labels=labels,
            source="isaac" if st.backend == "isaac" else "fake",
        )


async def truth_for(services: Services, local: LocalTruth, robot_id: RobotId, slot_ids: Sequence[SlotId] = ()) -> Any:
    """``services.truth.truth(...)`` when the robot arm wired one, else the local rebuild (same shape)."""
    src = getattr(services, "truth", None)
    if src is not None:
        return await src.truth(robot_id, list(slot_ids))
    return await local.truth(robot_id, list(slot_ids))


__all__ = ["FACING_TOLERANCE_M", "LocalTruth", "product_label", "truth_for"]
