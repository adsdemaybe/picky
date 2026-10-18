"""Evidence frames + ground truth for the planner -> VLA -> verifier loop (new in 0.2.1; v0.2 cameras unchanged).

The v0.2 camera contract (``contracts.cameras`` + ``CameraService``) serves blurred snapshots / streams / ``observe``.
The loop needs three more things, and nothing else:

1. ``FrameRef``: a pointer to a RETAINED, face-BLURRED evidence frame. Plans, VLA statuses, verdicts and events never
   inline JPEG bytes; the web app loads ``FrameRef.url`` (``GET /api/evidence/{frame_id}.jpg``).
2. ``GroundTruthSidecar`` / ``TruthSnapshot``: what was really in front of a camera when a frame was captured (2D fake
   world or Isaac instances). The FAKE verifier decides from it exactly; a real verifier only scores agreement with it.
   A sidecar is NEVER sent to a vision model.
3. Two small Protocols: ``EvidenceCameras`` (capture + retain blurred evidence frames; perception arm, next to
   ``CameraService``) and ``TruthSource`` (ground truth; robot arm, which owns the 2D world and the fault overlay).

Privacy (CLAUDE.md section 8): ``FrameRef.blurred`` is ``Literal[True]``; implementations blur with
``PerceptionService.blur_faces`` (or serve ``CameraService.snapshot``, already blurred) BEFORE retaining a frame.
Synthetic fake frames contain no people; they are still marked blurred only because nothing in them needs blurring.

Leaf module: imports only v0.2 ``contracts`` (cameras, common, sim) + stdlib + pydantic. (Not in ``contracts.cameras``
because it needs ``contracts.sim.CameraFrame`` / ``ItemInstance`` and ``contracts.sim`` imports ``contracts.cameras``.)
"""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, computed_field

from contracts.cameras import RobotCameraKind
from contracts.common import (
    ApprovalId,
    BBox,
    CameraId,
    Cents,
    FrameId,
    InstanceId,
    Model,
    Point3,
    Pose2D,
    RobotId,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    utcnow,
)
from contracts.sim import CameraFrame, ItemInstance

FaultId = str
"""Id of an injected demo fault (``contracts.faults.FaultRule.fault_id``)."""


def fingerprint(payload: Any) -> str:
    """Stable sha256 (16 hex chars) of a JSON-able payload (idempotency body hashes, deterministic frame seeds)."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class FrameRef(Model):
    """Pointer to a face-BLURRED frame retained by ``EvidenceCameras`` (>= 10 min). Never carries image bytes."""

    frame_id: FrameId
    camera_id: CameraId
    robot_id: RobotId | None = None
    w: int = Field(640, ge=1)
    h: int = Field(480, ge=1)
    blurred: Literal[True] = True
    pose: Pose2D | None = Field(None, description="Robot pose at capture.")
    ts: Timestamp = Field(
        default_factory=utcnow,
        description="When the GATEWAY received / retained the frame, on the gateway clock (VerifyRequest.not_before = "
        "VlaOutcome.ended_at is also gateway clock). Never copy the Isaac worker's CameraFrame.ts: vhagar's clock may "
        "be skewed, and a worker clock behind by 0.5 s marks every capture_after frame stale.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def url(self) -> str:
        """Root-relative like ``CameraInfo.snapshot_url``: blurred JPEG served by the gateway."""
        return f"/api/evidence/{self.frame_id}.jpg"


class SlotTruth(Model):
    """Ground truth of one slot near the robot."""

    slot_id: SlotId
    sku: Sku
    approach_pose: Pose2D
    item_position: Point3
    instances: list[ItemInstance] = Field(default_factory=list, description="Units on the shelf now.")
    shelf_tag_cents: Cents | None = None


class TruthSnapshot(Model):
    """Ground truth for one robot at one instant: the 2D fake world (or Isaac ``frame.instances`` + RobotState) with the
    fault overlay applied, so "the VLA said success but the jar is still on the shelf" looks the same to the fake
    camera, the fake verifier and the VLA's own claim."""

    robot_id: RobotId
    store_id: StoreId
    pose: Pose2D
    battery_pct: float | None = None
    holding: ItemInstance | None = Field(None, description="What is ACTUALLY in the gripper.")
    basket: list[ItemInstance] = Field(default_factory=list)
    slots: dict[SlotId, SlotTruth] = Field(default_factory=dict, description="Requested slots + the one in view.")
    visible_slots: dict[CameraId, list[SlotId]] = Field(default_factory=dict)
    occluded: list[CameraId] = Field(default_factory=list, description="Cameras whose frames show nothing useful.")
    labels: dict[InstanceId, str] = Field(
        default_factory=dict, description="Readable label text per unit (default '<brand> <name> <size>')."
    )
    approvals: dict[ApprovalId, bool] = Field(default_factory=dict)
    source: Literal["fake", "isaac"] = "fake"
    ts: Timestamp = Field(default_factory=utcnow)

    def basket_counts(self) -> dict[Sku, int]:
        return dict(Counter(i.sku for i in self.basket))

    def truth_hash(self) -> str:
        """Deterministic seed: the same truth gives byte-identical fake frames (fake frame ids embed it)."""
        return fingerprint(self.model_dump(mode="json", exclude={"ts", "approvals"}))


class GroundTruthSidecar(Model):
    """Fake camera API: the exact ground truth behind ONE retained frame (``TruthSource.ground_truth(frame_id)``)."""

    frame_id: FrameId
    camera_id: CameraId
    robot_id: RobotId | None = None
    truth: TruthSnapshot
    in_view: dict[InstanceId, BBox | None] = Field(default_factory=dict, description="Pixel boxes (None = occluded).")
    occluded: bool = False
    stale_of: FrameId | None = Field(None, description="'stale_frame' fault: this frame re-serves an older one.")
    synthetic: bool = Field(True, description="Rendered by the fake (no people in it) vs a CameraService snapshot.")
    faults_applied: list[FaultId] = Field(default_factory=list)


class FrameAnnotations(Model):
    """GET /api/evidence/{frame_id}/annotations (P1): everything drawn on one evidence frame."""

    frame: FrameRef
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, description="grounding.Evidence dumps (boxes / points / texts) on this frame."
    )
    verify_ids: list[str] = Field(default_factory=list)
    grounding_ids: list[str] = Field(default_factory=list)


@runtime_checkable
class EvidenceCameras(Protocol):
    """Retained, blurred evidence frames (perception arm; ``Services.evidence``). Real: ``CameraService.snapshot``
    (blurred) + a retention ring; fake: deterministic synthetic frames + sidecars (``create_evidence_fake``)."""

    async def capture_refs(
        self,
        robot_id: RobotId,
        cameras: Sequence[RobotCameraKind],
        *,
        not_before: Timestamp | None = None,
        context: dict[str, Any] | None = None,
        aim_slot_id: SlotId | None = None,
    ) -> list[FrameRef]:
        """Capture one BLURRED frame per camera now, retain it, return refs (order = ``cameras``; an offline camera
        is left out). ``context`` (plan/step/attempt) lets the fake match camera faults.

        ``aim_slot_id``: the 'front' camera must look at this slot (grounding, item_on_shelf, shelf tags). A plain v0.2
        capture points the pan-tilt head straight ahead at its rest tilt, so on Isaac the real impl captures 'front'
        with ``RobotService.scan(robot_id, aim_slot_id)`` (aims the head, returns the frame with ground-truth
        ``instances`` + pixel bboxes) and every other camera with ``RobotService.capture``. scan is a motion command:
        only call it between VLA steps (it answers 'busy' while one runs; fall back to capture then)."""
        ...

    async def get_frame(self, frame_id: FrameId) -> CameraFrame:
        """A retained blurred frame (GET /api/evidence/{frame_id}.jpg). KeyError when unknown / expired.
        Real impl: retain the frame AS CAPTURED incl. ``instances`` (sim ground truth with pixel bboxes; scaled by
        CameraService). They never leave the gateway (the JPEG route serves bytes only, providers get bytes only); the
        real verifier uses them to map a Perceptron grasp point to an ``instance_id`` and to score shadow agreement."""
        ...


@runtime_checkable
class TruthSource(Protocol):
    """Ground truth of the simulated world (robot arm; ``Services.truth``). Fake: the 2D world + fault overlay.
    Isaac: RobotState + list_instances. Backs the fake verifier and the real verifier's 'ground_truth' fallback
    (always labelled as such in verdicts)."""

    async def truth(self, robot_id: RobotId, slot_ids: Sequence[SlotId] = ()) -> TruthSnapshot: ...

    async def ground_truth(self, frame_id: FrameId) -> GroundTruthSidecar:
        """The sidecar of a retained frame. KeyError when unknown / expired, AND for frames this source did not
        produce: the real robot-arm TruthSource does not see frames retained by the perception arm's EvidenceCameras.
        Verifiers must handle KeyError by falling back to ``truth(robot_id)`` (now) + ``EvidenceCameras.get_frame
        (frame_id).instances`` (what the frame showed)."""
        ...
