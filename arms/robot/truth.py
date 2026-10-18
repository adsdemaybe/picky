"""RobotTruth: ``contracts.evidence.TruthSource`` (``Services.truth``) over the RobotService the gateway wired.

What is REALLY in front of a robot right now, with the fault overlay applied, so "the VLA said success but the jar is
still on the shelf" looks the same to the fake camera, the fake verifier and the VLA's own claim:

- pose / battery / holding / basket from ``robot.state`` + ``robot.list_instances`` (2D fake world, or the Isaac
  scene through ``ListInstancesCmd`` when the store runs on Isaac: same code, ``source`` says which);
- slot geometry, shelf tags and labels from the live planogram + products (``services.catalog`` at call time, else
  ``utils.data``); the slots asked for plus every slot the front camera faces from the current pose;
- the overlay (``services.faults.overlay``): a virtual wrong unit in the gripper / basket (Isaac 'wrong_item') and
  mislabels. On the 2D fake a wrong unit is really held, so the overlay is empty there.

``ground_truth(frame_id)`` serves sidecars registered with ``remember`` (ring of 500). The robot arm retains no
evidence frames itself, so for a frame perception retained and did not register this raises KeyError, as the contract
says; verifiers then fall back to ``truth()`` + ``EvidenceCameras.get_frame(frame_id).instances``.
"""

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from arms.robot.world import live_planogram, load_scene
from contracts.cameras import robot_camera_id
from contracts.catalog import Product
from contracts.common import Pose2D, RobotId, Sku, SlotId, StoreId
from contracts.evidence import GroundTruthSidecar, SlotTruth, TruthSnapshot
from contracts.planogram import Planogram, Slot
from contracts.services import Services
from contracts.sim import ItemInstance, RobotError
from utils.log import get_logger

log = get_logger(__name__)

FACING_TOLERANCE_M = 0.15
"""The front camera faces every slot whose approach pose is within this distance of the robot (stacked levels share
one approach pose)."""
SIDECAR_RING = 500


def facing_slots(plan: Planogram, pose: Pose2D, tolerance_m: float = FACING_TOLERANCE_M) -> list[SlotId]:
    return sorted(s.slot_id for s in plan.slots if pose.distance_to(s.approach_pose) <= tolerance_m)


def shelf_tag_cents(slot: Slot, product: Product | None) -> int | None:
    """What the physical tag shows (mispricings show the wrong tag); same rule as the 2D fake's renderer."""
    if slot.shelf_tag_cents is not None:
        return slot.shelf_tag_cents
    if slot.deal is not None:
        return slot.deal.price_cents
    if slot.price_cents is not None:
        return slot.price_cents
    return product.price_cents if product else None


def label_text(product: Product | None, sku: Sku) -> str:
    """Readable label: '<brand> <name> <size>' (the verifier matches label hints against it)."""
    if product is None:
        return sku
    return " ".join(p for p in (product.brand, product.name, product.size) if p)


class RobotTruth:
    """``TruthSource``. Construct via ``create_truth(services)``; calls services only at call time."""

    def __init__(self, services: Services | None = None, *, facing_tolerance_m: float = FACING_TOLERANCE_M) -> None:
        self.services = services or Services()
        self.facing_tolerance_m = facing_tolerance_m
        self._products: dict[StoreId, dict[Sku, Product]] = {}
        self._sidecars: OrderedDict[str, GroundTruthSidecar] = OrderedDict()

    # ---- TruthSource -------------------------------------------------------------------------------------------------
    async def truth(self, robot_id: RobotId, slot_ids: Sequence[SlotId] = ()) -> TruthSnapshot:
        """KeyError for an unknown robot; RuntimeError when no RobotService is wired."""
        robot = self.services.require("robot")
        try:
            st = await robot.state(robot_id)
        except RobotError as exc:
            if exc.code == "unknown_robot":
                raise KeyError(robot_id) from exc
            raise
        store_id = st.store_id or ""
        units = await robot.list_instances(robot_id)
        by_id = {u.instance_id: u for u in units}
        overlay = self._overlay()

        holding = by_id.get(st.holding) if st.holding else None
        if overlay is not None and robot_id in overlay.virtual_holding:
            holding = overlay.virtual_holding[robot_id]
        basket = [by_id[i] for i in st.basket if i in by_id] or [u for u in units if u.in_basket]
        if overlay is not None:
            basket = [*basket, *overlay.virtual_basket.get(robot_id, [])]

        plan = await live_planogram(self.services, store_id)
        products = await self._product_map(store_id)
        geom = {s.slot_id: s for s in plan.slots}
        facing = facing_slots(plan, st.pose, self.facing_tolerance_m)
        wanted = [sid for sid in dict.fromkeys([*slot_ids, *facing]) if sid in geom]
        taken = {i for i in (st.holding, holding.instance_id if holding else None) if i}
        taken |= {u.instance_id for u in basket}
        slots: dict[SlotId, SlotTruth] = {}
        for sid in wanted:
            g = geom[sid]
            slots[sid] = SlotTruth(
                slot_id=sid,
                sku=g.sku,
                approach_pose=g.approach_pose,
                item_position=g.item_position,
                instances=[u for u in units if u.slot_id == sid and not u.in_basket and u.instance_id not in taken],
                shelf_tag_cents=shelf_tag_cents(g, products.get(g.sku)),
            )

        labels: dict[str, str] = {}
        shown: list[ItemInstance] = [*(u for s in slots.values() for u in s.instances), *basket]
        if holding is not None:
            shown.append(holding)
        for unit in shown:
            text = overlay.mislabels.get(unit.instance_id) if overlay is not None else None
            labels[unit.instance_id] = text or label_text(products.get(unit.sku), unit.sku)

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

    async def ground_truth(self, frame_id: str) -> GroundTruthSidecar:
        return self._sidecars[frame_id]

    # ---- extras ------------------------------------------------------------------------------------------------------
    def remember(self, sidecar: GroundTruthSidecar) -> None:
        """Register the sidecar of a frame someone else retained (e.g. a fake EvidenceCameras), so
        ``ground_truth(frame_id)`` can serve it. Keeps the newest ``SIDECAR_RING``."""
        self._sidecars[sidecar.frame_id] = sidecar
        self._sidecars.move_to_end(sidecar.frame_id)
        while len(self._sidecars) > SIDECAR_RING:
            self._sidecars.popitem(last=False)

    def _overlay(self) -> Any:
        return getattr(self.services.faults, "overlay", None) if self.services.faults is not None else None

    async def _product_map(self, store_id: StoreId) -> dict[Sku, Product]:
        if store_id not in self._products:
            try:
                _, products = await load_scene(self.services, store_id)
            except RobotError as exc:
                log.warning("no products for %s (%s): labels fall back to SKUs", store_id, exc)
                return {}
            self._products[store_id] = products
        return self._products[store_id]


__all__ = ["FACING_TOLERANCE_M", "RobotTruth", "facing_slots", "label_text", "shelf_tag_cents"]
