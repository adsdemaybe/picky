"""The fake camera API: deterministic evidence frames + an exact ground-truth sidecar per frame, with camera faults.

``FakeEvidenceCameras`` implements both ``EvidenceCameras`` (perception side: capture + retain blurred frames) and
``TruthSource`` (robot side: ground truth of the 2D world + the fault overlay), over the v0.2 Protocols only:
``services.robot`` (state / list_instances), ``services.catalog`` (planogram geometry, product labels) and, when
``use_camera_service`` is set, ``services.cameras.snapshot`` (v0.2 CameraService, already face-blurred) for the image
bytes. Otherwise frames are synthetic (a fixed grey JPEG: no people, nothing to blur) and fully deterministic:
frame_id = '<camera_id>:<tick>:<truth hash>'.

Camera faults (``faults.FAULT_LAYERS`` = camera): camera_occluded (the sidecar marks the frame occluded -> camera atoms
uncertain), camera_offline (the camera is left out), stale_frame (the previous frame of that camera is re-served with
its old timestamp -> rejected by VerifyRequest.not_before).
"""

from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from arms.agent.tests.loop_world.world import TINY_JPEG_B64
from contracts.cameras import RobotCameraKind, robot_camera_id
from contracts.catalog import Product
from contracts.common import BBox, Pose2D
from contracts.evidence import FrameRef, GroundTruthSidecar, SlotTruth, TruthSnapshot
from contracts.faults import FaultContext
from contracts.planogram import Slot
from contracts.sim import CameraFrame, ItemInstance

FACING_TOLERANCE_M = 0.15
"""The front camera 'sees' the slot whose approach pose is within this distance of the robot."""


class FakeEvidenceCameras:
    def __init__(self, services: Any, *, ring: int = 500, use_camera_service: bool = False) -> None:
        self.s = services
        self.ring = ring
        self.use_camera_service = use_camera_service
        self._frames: OrderedDict[str, CameraFrame] = OrderedDict()
        self._sidecars: dict[str, GroundTruthSidecar] = {}
        self._last: dict[str, str] = {}
        self._tick = 0
        self._geom: dict[str, dict[str, Slot]] = {}
        self._products: dict[str, Product | None] = {}

    # ---- helpers -----------------------------------------------------------------------------------------------------
    async def _slots(self, store_id: str) -> dict[str, Slot]:
        if store_id not in self._geom:
            plan = await self.s.require("catalog").get_planogram(store_id)
            self._geom[store_id] = {s.slot_id: s for s in plan.slots}
        return self._geom[store_id]

    async def _product(self, sku: str) -> Product | None:
        if sku not in self._products:
            self._products[sku] = await self.s.require("catalog").get_product(sku)
        return self._products[sku]

    async def _label(self, unit: ItemInstance) -> str:
        overlay = self._overlay()
        if overlay is not None and unit.instance_id in overlay.mislabels:
            return overlay.mislabels[unit.instance_id]
        p = await self._product(unit.sku)
        return f"{p.brand or ''} {p.name}".strip() if p else unit.sku

    def _overlay(self) -> Any:
        faults = getattr(self.s, "faults", None)
        return getattr(faults, "overlay", None)

    @staticmethod
    def facing(slots: dict[str, Slot], pose: Pose2D) -> list[str]:
        """Every slot the front camera sees from ``pose`` (stacked shelf levels share one approach pose)."""
        return sorted(sid for sid, s in slots.items() if pose.distance_to(s.approach_pose) <= FACING_TOLERANCE_M)

    # ---- TruthSource -------------------------------------------------------------------------------------------------
    async def truth(self, robot_id: str, slot_ids: Sequence[str] = ()) -> TruthSnapshot:
        robot = self.s.require("robot")
        st = await robot.state(robot_id)
        units = await robot.list_instances(robot_id)
        by_id = {u.instance_id: u for u in units}
        holding = by_id.get(st.holding) if st.holding else None
        basket = [by_id[i] for i in st.basket if i in by_id] or [u for u in units if u.in_basket]
        overlay = self._overlay()
        if overlay is not None and robot_id in overlay.virtual_holding:
            holding = overlay.virtual_holding[robot_id]
        store_id = st.store_id or ""
        geom = await self._slots(store_id)
        facing = self.facing(geom, st.pose)
        wanted = [sid for sid in [*slot_ids, *facing] if sid and sid in geom]
        counter = [u for u in units if u.on_counter]
        taken = {st.holding, *st.basket, *(u.instance_id for u in counter)}
        slots: dict[str, SlotTruth] = {}
        for sid in dict.fromkeys(wanted):
            g = geom[sid]
            p = await self._product(g.sku)
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
            labels[unit.instance_id] = await self._label(unit)
        return TruthSnapshot(
            robot_id=robot_id,
            store_id=store_id,
            pose=st.pose,
            battery_pct=st.battery_pct,
            holding=holding,
            basket=basket,
            counter=counter,
            slots=slots,
            visible_slots={robot_camera_id(robot_id, "front"): facing},
            labels=labels,
            source="isaac" if st.backend == "isaac" else "fake",
        )

    async def ground_truth(self, frame_id: str) -> GroundTruthSidecar:
        return self._sidecars[frame_id]

    # ---- EvidenceCameras ---------------------------------------------------------------------------------------------
    async def capture_refs(
        self,
        robot_id: str,
        cameras: Sequence[RobotCameraKind],
        *,
        not_before: Any = None,
        context: dict[str, Any] | None = None,
        aim_slot_id: str | None = None,
    ) -> list[FrameRef]:
        ctxd = dict(context or {})
        if aim_slot_id and aim_slot_id not in (ctxd.get("slot_ids") or []):
            ctxd["slot_ids"] = [aim_slot_id, *(ctxd.get("slot_ids") or [])]
        faults = getattr(self.s, "faults", None)
        refs: list[FrameRef] = []
        for kind in cameras:
            cid = robot_camera_id(robot_id, kind)
            effects: dict[str, str] = {}
            if faults is not None:
                fctx = FaultContext(
                    robot_id=robot_id, camera=kind, **{k: v for k, v in ctxd.items() if k in FaultContext.model_fields}
                )
                for f in await faults.consult(fctx, ["camera_occluded", "camera_offline", "stale_frame"]):
                    effects[f.effect] = f.fault_id
            if "camera_offline" in effects:
                continue
            if "stale_frame" in effects and cid in self._last and self._last[cid] in self._frames:
                old = self._sidecars[self._last[cid]]
                refs.append(self._ref(self._frames[self._last[cid]], old.truth.pose))
                continue
            truth = await self.truth(
                robot_id, ctxd.get("slot_ids") or ([ctxd["slot_id"]] if ctxd.get("slot_id") else [])
            )
            occluded = "camera_occluded" in effects
            if occluded:
                truth = truth.model_copy(update={"occluded": [cid]})
            frame = await self._render(cid, robot_id, kind, truth)
            in_view = {} if occluded else self._in_view(kind, truth, frame.w, frame.h)
            self._frames[frame.frame_id] = frame
            self._sidecars[frame.frame_id] = GroundTruthSidecar(
                frame_id=frame.frame_id,
                camera_id=cid,
                robot_id=robot_id,
                truth=truth,
                in_view=in_view,
                occluded=occluded,
                synthetic=not self.use_camera_service,
                faults_applied=sorted(effects.values()),
            )
            self._last[cid] = frame.frame_id
            while len(self._frames) > self.ring:
                old_id, _ = self._frames.popitem(last=False)
                self._sidecars.pop(old_id, None)
            refs.append(self._ref(frame, truth.pose))
        return refs

    async def get_frame(self, frame_id: str) -> CameraFrame:
        return self._frames[frame_id]

    # ---- rendering ---------------------------------------------------------------------------------------------------
    async def _render(self, cid: str, robot_id: str, kind: str, truth: TruthSnapshot) -> CameraFrame:
        self._tick += 1
        fid = f"{cid}:{self._tick:05d}:{truth.truth_hash()[:6]}"
        cameras = getattr(self.s, "cameras", None)
        if self.use_camera_service and cameras is not None:
            snap = await cameras.snapshot(cid)
            if not snap.blurred:
                raise ValueError("CameraService returned an unblurred frame")
            return snap.model_copy(update={"frame_id": fid, "instances": []})
        return CameraFrame(
            frame_id=fid,
            camera=kind,  # type: ignore[arg-type]
            camera_id=cid,
            robot_id=robot_id,
            store_id=truth.store_id,
            jpeg_b64=TINY_JPEG_B64,
            w=640,
            h=480,
            blurred=True,  # synthetic: no people in it, nothing to blur
            pose=truth.pose,
        )

    @staticmethod
    def _in_view(kind: str, truth: TruthSnapshot, w: int, h: int) -> dict[str, BBox | None]:
        if kind == "wrist":
            return (
                {truth.holding.instance_id: BBox(x=w * 0.35, y=h * 0.35, w=w * 0.3, h=h * 0.3)} if truth.holding else {}
            )
        if kind == "basket":
            return {
                u.instance_id: BBox(x=40.0 + (i % 6) * 95, y=60.0 + (i // 6) * 140, w=80, h=120)
                for i, u in enumerate(truth.basket)
            }
        units = (
            [
                u
                for sid in truth.visible_slots.get(robot_camera_id(truth.robot_id, "front"), [])
                for u in truth.slots[sid].instances
            ]
            if truth.slots
            else []
        )
        return {
            u.instance_id: (BBox(x=60.0 + i * 70, y=200, w=60, h=110) if i < 8 else None) for i, u in enumerate(units)
        }

    @staticmethod
    def _ref(frame: CameraFrame, pose: Pose2D | None) -> FrameRef:
        return FrameRef(
            frame_id=frame.frame_id,
            camera_id=frame.camera_id or "",
            robot_id=frame.robot_id,
            w=frame.w,
            h=frame.h,
            pose=pose,
            ts=frame.ts,
        )
