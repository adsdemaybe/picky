"""Evidence cameras (``contracts.evidence.EvidenceCameras``): capture + RETAIN face-blurred frames for the loop.

Two implementations share one retention store (ring of ``ring`` frames, ``max_age_s`` seconds) and one sidecar per
frame (``GroundTruthSidecar``: what was really in front of the camera at capture time, from ``services.truth`` or the
local rebuild in ``truth.py``). Sidecars never leave the gateway and are never sent to a vision model; the verifiers
read them through :meth:`EvidenceStore.ground_truth`.

- :class:`FakeEvidenceCameras` (``create_evidence_fake``): deterministic synthetic frames (a fixed grey JPEG: no
  people, nothing to blur), frame id ``'<camera_id>:<tick>:<truth hash>'``, synthetic pixel boxes per unit. Base deps
  only. ``use_camera_service=True`` takes the image bytes from ``CameraService.snapshot`` instead.
- :class:`EvidenceRing` (``create_evidence``): real frames. 'front' with ``aim_slot_id`` while the robot is idle =
  ``RobotService.scan`` (aims the pan-tilt head; RAW, blurred here with ``perception.blur_faces``); everything else =
  ``CameraService.snapshot`` (already blurred), else ``RobotService.capture`` + blur. Frames are re-stamped on the
  GATEWAY clock (``FrameRef.ts`` contract: vhagar's clock may be skewed) and kept WITH their ``instances``.

Camera faults (``contracts.faults``, layer 'camera', consulted on ``services.faults`` when wired): ``camera_offline``
(the camera is left out), ``camera_occluded`` (blank frame + sidecar ``occluded``), ``stale_frame`` (the previous frame
of that camera is re-served with its old timestamp, so ``VerifyRequest.not_before`` rejects it).

Privacy (CLAUDE.md section 8): nothing is retained unless ``blurred=True`` is honest: ``blur_faces``, else the
"rendered by Isaac / the 2D fake" certification (every camera here is a sim camera, the same rule as the Cameras
API), else the whole frame is pixelated.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from contracts.cameras import RobotCameraKind, robot_camera_id
from contracts.common import BBox, FrameId, Pose2D, RobotId, SlotId, Timestamp, utcnow
from contracts.evidence import FrameRef, GroundTruthSidecar, TruthSnapshot
from contracts.faults import FaultContext
from contracts.services import Services
from contracts.sim import CameraFrame, RobotStatus
from utils.log import get_logger

from .truth import LocalTruth, truth_for

log = get_logger(__name__)

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
"""A 64x48 grey JPEG: the synthetic / occluded frame body (no people in it)."""

DEFAULT_RING = 500
DEFAULT_MAX_AGE_S = 600.0
"""Retention: FrameRef promises >= 10 min (evidence URLs in plan events stay valid for a whole trip)."""
SCAN_TIMEOUT_S = 5.0
CAMERA_FAULTS = ("camera_occluded", "camera_offline", "stale_frame")
RENDERED_BACKENDS = ("isaac", "fake")


def frame_ref(frame: CameraFrame, pose: Pose2D | None = None) -> FrameRef:
    return FrameRef(
        frame_id=frame.frame_id,
        camera_id=frame.camera_id or "",
        robot_id=frame.robot_id,
        w=frame.w,
        h=frame.h,
        pose=pose if pose is not None else frame.pose,
        ts=frame.ts,
    )


class EvidenceStore:
    """Retention + sidecars + camera faults shared by the fake and the real evidence cameras."""

    def __init__(self, services: Services, *, ring: int = DEFAULT_RING, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        self.s = services
        self.ring = ring
        self.max_age_s = max_age_s
        self.local = LocalTruth(services)
        self._frames: OrderedDict[FrameId, tuple[CameraFrame, float]] = OrderedDict()
        self._sidecars: dict[FrameId, GroundTruthSidecar] = {}
        self._last: dict[str, FrameId] = {}
        self._tick = 0
        self.captures = 0

    # ---- retention -----------------------------------------------------------------------------------------------
    def _retain(self, frame: CameraFrame, sidecar: GroundTruthSidecar | None) -> None:
        if not frame.blurred:  # defence in depth: nothing unblurred is ever retained
            raise ValueError(f"refusing to retain unblurred frame {frame.frame_id}")
        self._frames[frame.frame_id] = (frame, time.monotonic())
        if sidecar is not None:
            self._sidecars[frame.frame_id] = sidecar
        if frame.camera_id:
            self._last[frame.camera_id] = frame.frame_id
        self._prune()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.max_age_s
        while self._frames:
            fid, (_, mono) = next(iter(self._frames.items()))
            if len(self._frames) <= self.ring and mono >= cutoff:
                break
            self._frames.popitem(last=False)
            self._sidecars.pop(fid, None)

    async def get_frame(self, frame_id: FrameId) -> CameraFrame:
        """The retained BLURRED frame incl. ground-truth ``instances`` (gateway-internal). KeyError when unknown /
        expired (the JPEG route answers 404)."""
        self._prune()
        return self._frames[frame_id][0]

    async def ground_truth(self, frame_id: FrameId) -> GroundTruthSidecar:
        """The sidecar of a retained frame (KeyError when unknown / expired / captured without truth)."""
        self._prune()
        if frame_id not in self._frames:
            raise KeyError(frame_id)
        return self._sidecars[frame_id]

    def retained(self) -> int:
        self._prune()
        return len(self._frames)

    # ---- faults / truth ------------------------------------------------------------------------------------------
    async def _camera_faults(self, robot_id: RobotId, kind: str, ctxd: dict[str, Any]) -> dict[str, str]:
        faults = getattr(self.s, "faults", None)
        if faults is None:
            return {}
        fields = {k: v for k, v in ctxd.items() if k in FaultContext.model_fields and k not in ("robot_id", "camera")}
        try:
            fctx = FaultContext(robot_id=robot_id, camera=kind, **fields)  # type: ignore[arg-type]
            return {f.effect: f.fault_id for f in await faults.consult(fctx, list(CAMERA_FAULTS))}
        except Exception as exc:  # noqa: BLE001 - a broken fault injector never breaks a capture
            log.warning("evidence: fault consult failed (%s)", exc)
            return {}

    async def _truth(self, robot_id: RobotId, slot_ids: Sequence[SlotId]) -> TruthSnapshot | None:
        try:
            return await truth_for(self.s, self.local, robot_id, slot_ids)
        except Exception as exc:  # noqa: BLE001 - no truth = no sidecar; the frame is still retained
            log.debug("evidence: no ground truth for %s (%s)", robot_id, exc)
            return None

    # ---- capture -------------------------------------------------------------------------------------------------
    async def capture_refs(
        self,
        robot_id: RobotId,
        cameras: Sequence[RobotCameraKind],
        *,
        not_before: Timestamp | None = None,
        context: dict[str, Any] | None = None,
        aim_slot_id: SlotId | None = None,
    ) -> list[FrameRef]:
        """One fresh BLURRED, retained frame per camera (order = ``cameras``; offline cameras are left out)."""
        ctxd = dict(context or {})
        slot_ids: list[SlotId] = list(ctxd.get("slot_ids") or ([ctxd["slot_id"]] if ctxd.get("slot_id") else []))
        if aim_slot_id and aim_slot_id not in slot_ids:
            slot_ids = [aim_slot_id, *slot_ids]
        ctxd["slot_ids"] = slot_ids
        refs: list[FrameRef] = []
        for kind in cameras:
            cid = robot_camera_id(robot_id, kind)
            effects = await self._camera_faults(robot_id, kind, ctxd)
            if "camera_offline" in effects:
                continue
            last = self._last.get(cid)
            if "stale_frame" in effects and last is not None and last in self._frames:
                old = self._frames[last][0]
                refs.append(frame_ref(old))
                continue
            truth = await self._truth(robot_id, slot_ids)
            occluded = "camera_occluded" in effects
            if occluded and truth is not None:
                truth = truth.model_copy(update={"occluded": sorted({*truth.occluded, cid})})
            try:
                frame = await self._render(robot_id, kind, cid, truth, aim_slot_id if kind == "front" else None)
            except Exception as exc:  # noqa: BLE001 - a camera that can't deliver is left out (contract)
                log.warning("evidence: capture %s failed (%s: %s)", cid, getattr(exc, "code", type(exc).__name__), exc)
                continue
            if frame is None:
                continue
            if occluded:
                frame = frame.model_copy(update={"jpeg_b64": TINY_JPEG_B64, "w": 64, "h": 48, "instances": []})
            sidecar = None
            if truth is not None:
                sidecar = GroundTruthSidecar(
                    frame_id=frame.frame_id,
                    camera_id=cid,
                    robot_id=robot_id,
                    truth=truth,
                    in_view={} if occluded else self._in_view(kind, truth, frame),
                    occluded=occluded,
                    synthetic=self._synthetic,
                    faults_applied=sorted(effects.values()),
                )
            self._retain(frame, sidecar)
            self.captures += 1
            refs.append(frame_ref(frame, truth.pose if truth is not None else None))
        return refs

    _synthetic = True

    async def _render(
        self, robot_id: RobotId, kind: str, cid: str, truth: TruthSnapshot | None, aim_slot_id: SlotId | None
    ) -> CameraFrame | None:
        raise RuntimeError(f"{type(self).__name__} does not render frames")

    def _in_view(self, kind: str, truth: TruthSnapshot, frame: CameraFrame) -> dict[str, BBox | None]:
        return {i.instance_id: i.bbox for i in frame.instances}


class FakeEvidenceCameras(EvidenceStore):
    """Deterministic synthetic evidence frames + exact sidecars (``create_evidence_fake``; base deps only)."""

    def __init__(self, services: Services, *, ring: int = DEFAULT_RING, use_camera_service: bool = False) -> None:
        super().__init__(services, ring=ring)
        self.use_camera_service = use_camera_service
        self._synthetic = not use_camera_service

    async def _render(
        self, robot_id: RobotId, kind: str, cid: str, truth: TruthSnapshot | None, aim_slot_id: SlotId | None
    ) -> CameraFrame | None:
        if truth is None:
            raise RuntimeError("no ground truth (robot service not wired)")
        self._tick += 1
        fid = f"{cid}:{self._tick:05d}:{truth.truth_hash()[:6]}"
        cameras = getattr(self.s, "cameras", None)
        if self.use_camera_service and cameras is not None:
            snap = await cameras.snapshot(cid)
            if not snap.blurred:
                raise ValueError("CameraService returned an unblurred frame")
            return snap.model_copy(update={"frame_id": fid, "instances": [], "ts": utcnow()})
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
            slot_id=aim_slot_id,
            pose=truth.pose,
        )

    def _in_view(self, kind: str, truth: TruthSnapshot, frame: CameraFrame) -> dict[str, BBox | None]:
        w, h = frame.w, frame.h
        if kind == "wrist":
            if truth.holding is None:
                return {}
            return {truth.holding.instance_id: BBox(x=w * 0.35, y=h * 0.35, w=w * 0.3, h=h * 0.3)}
        if kind == "basket":
            return {
                u.instance_id: BBox(x=40.0 + (i % 6) * 95, y=60.0 + (i // 6) * 140, w=80, h=120)
                for i, u in enumerate(truth.basket)
            }
        front = robot_camera_id(truth.robot_id, "front")
        units = [
            u for sid in truth.visible_slots.get(front, []) if sid in truth.slots for u in truth.slots[sid].instances
        ]
        return {
            u.instance_id: (BBox(x=60.0 + i * 70, y=200, w=60, h=110) if i < 8 else None) for i, u in enumerate(units)
        }


class EvidenceRing(EvidenceStore):
    """Real evidence frames (``create_evidence``): CameraService snapshots / aimed RobotService scans, blurred,
    re-stamped on the gateway clock, retained with their ground-truth ``instances``."""

    _synthetic = False

    def __init__(self, services: Services, *, ring: int = DEFAULT_RING, max_age_s: float = DEFAULT_MAX_AGE_S) -> None:
        super().__init__(services, ring=ring, max_age_s=max_age_s)
        self._n = 0
        self._fallback_blurrer: Any = None

    async def _blur(self, frame: CameraFrame, backend: str | None) -> CameraFrame:
        """A frame that is safe to retain / send to a vision model (``blurred=True`` honestly)."""
        if frame.blurred:
            return frame
        blurrer = self.s.perception
        if blurrer is None:
            if self._fallback_blurrer is None:
                from .fake import FakePerception

                self._fallback_blurrer = FakePerception(self.s)
            blurrer = self._fallback_blurrer
        out = frame
        try:
            out = await blurrer.blur_faces(frame)
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence: blur_faces failed on %s (%s)", frame.frame_id, exc)
        if out.blurred:
            return out
        if backend in RENDERED_BACKENDS:  # rendered by Isaac / the 2D fake: there are no real faces to hide
            return out.model_copy(update={"blurred": True})
        from .cameras import obscure_frame

        log.warning("evidence: %s not face-checked; pixelating the whole frame", frame.frame_id)
        return await asyncio.to_thread(obscure_frame, out)

    async def _capture(self, robot_id: RobotId, kind: str, cid: str, aim_slot_id: SlotId | None) -> CameraFrame | None:
        robot = self.s.robot
        backend: str | None = None
        if robot is not None:
            try:
                st = await robot.state(robot_id)
                backend = st.backend
                if kind == "front" and aim_slot_id and st.status == RobotStatus.IDLE:
                    raw = await asyncio.wait_for(robot.scan(robot_id, aim_slot_id), SCAN_TIMEOUT_S)
                    return await self._blur(raw, backend)
            except Exception as exc:  # noqa: BLE001 - busy / timeout / unknown slot: plain capture instead
                log.info("evidence: aimed scan of %s for %s failed (%s); plain capture", aim_slot_id, cid, exc)
        cameras = self.s.cameras
        if cameras is not None:
            snap = await cameras.snapshot(cid)
            from .cameras import is_placeholder

            if is_placeholder(snap):
                return None  # the camera is offline: left out
            if snap.blurred:
                return snap
            return await self._blur(snap, backend)
        if robot is None:
            raise RuntimeError("no robot / cameras service wired")
        return await self._blur(await robot.capture(robot_id, kind), backend)

    async def _render(
        self, robot_id: RobotId, kind: str, cid: str, truth: TruthSnapshot | None, aim_slot_id: SlotId | None
    ) -> CameraFrame | None:
        frame = await self._capture(robot_id, kind, cid, aim_slot_id)
        if frame is None:
            return None
        self._n += 1
        return frame.model_copy(
            update={
                "frame_id": f"{cid}:{self._n:06d}",
                "camera": kind,
                "camera_id": cid,
                "robot_id": robot_id,
                "ts": utcnow(),  # GATEWAY clock (FrameRef.ts contract), never the worker's
                "pose": frame.pose or (truth.pose if truth is not None else None),
                "slot_id": frame.slot_id or aim_slot_id,
            }
        )


def create_evidence(services: Services) -> EvidenceRing:
    """Real evidence frames (CameraService snapshots / aimed scans). No key needed; never calls services here."""
    return EvidenceRing(services)


def create_evidence_fake(services: Services) -> FakeEvidenceCameras:
    """Deterministic synthetic evidence frames + exact sidecars (base deps only)."""
    return FakeEvidenceCameras(services)


__all__ = [
    "create_evidence",
    "create_evidence_fake",
    "DEFAULT_MAX_AGE_S",
    "DEFAULT_RING",
    "TINY_JPEG_B64",
    "EvidenceRing",
    "EvidenceStore",
    "FakeEvidenceCameras",
    "frame_ref",
]
