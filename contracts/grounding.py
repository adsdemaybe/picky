"""Grounding + evidence primitives shared by plans, the VLA API and the verifier.

Coordinates follow Perceptron's convention: integers on a normalized 0-1000 grid, origin top-left, x right, y down
(``NormBox`` / ``NormPoint``); ``to_px`` converts with the frame's w/h. Every grounded fact carries the frame it was
read from (``FrameRef``, blurred) and who decided it (``ProviderCall`` / ``DecisionSource``), so the web app can show
"Perceptron Mk1.5" vs "Sim ground truth" vs "Robot state" honestly.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, computed_field, model_validator

from contracts.common import (
    BBox,
    InstanceId,
    Model,
    Point3,
    Pose2D,
    RobotId,
    Score01,
    Sku,
    SlotId,
    Timestamp,
    utcnow,
)
from contracts.evidence import FrameRef

NORM_MAX = 1000
GroundingId = str

VerifyMethod = Literal["perceptron", "vlm", "ground_truth", "state", "fake"]
"""perceptron = Perceptron grounded vision (THE verifier provider) · vlm = general VLM fallback (Gemini by default,
Claude when PICKY_LLM_PROVIDER=anthropic) · ground_truth = sim instances (sim only,
always labelled) · state = RobotState / blackboard facts, no camera · fake = deterministic fake verifier."""
DecisionSource = Literal["provider", "ground_truth", "state", "human"]
"""Who actually decided a verdict / grounding (shown in the UI next to the check)."""
VisionProviderName = Literal["perceptron", "gemini", "claude", "fake", "ground_truth", "state"]
"""perceptron = required real provider (Perceptron Mk1.5) · gemini / claude = optional VLM fallback (utils.llm) ·
fake = deterministic fake verifier · ground_truth / state = no model call."""


class NormBox(Model):
    """Box on the 0-1000 grid: (x1, y1) top-left, (x2, y2) bottom-right."""

    x1: int = Field(ge=0, le=NORM_MAX)
    y1: int = Field(ge=0, le=NORM_MAX)
    x2: int = Field(ge=0, le=NORM_MAX)
    y2: int = Field(ge=0, le=NORM_MAX)

    @model_validator(mode="after")
    def _ordered(self) -> "NormBox":
        if self.x2 < self.x1 or self.y2 < self.y1:
            raise ValueError("NormBox corners out of order")
        return self

    def to_px(self, w: int, h: int) -> BBox:
        return BBox(
            x=self.x1 * w / NORM_MAX,
            y=self.y1 * h / NORM_MAX,
            w=(self.x2 - self.x1) * w / NORM_MAX,
            h=(self.y2 - self.y1) * h / NORM_MAX,
        )

    @classmethod
    def from_px(cls, bbox: BBox, w: int, h: int) -> "NormBox":
        def c(v: float, size: int) -> int:
            return max(0, min(NORM_MAX, round(v * NORM_MAX / size)))

        return cls(x1=c(bbox.x, w), y1=c(bbox.y, h), x2=c(bbox.x + bbox.w, w), y2=c(bbox.y + bbox.h, h))

    def center(self) -> "NormPoint":
        return NormPoint(x=(self.x1 + self.x2) // 2, y=(self.y1 + self.y2) // 2)


class NormPoint(Model):
    x: int = Field(ge=0, le=NORM_MAX)
    y: int = Field(ge=0, le=NORM_MAX)


def box_iou(a: NormBox, b: NormBox) -> float:
    """Intersection over union of two boxes on the 0-1000 grid (0.0 for disjoint or degenerate boxes)."""
    ix = max(0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    union = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
    return inter / union if union > 0 else 0.0


def point_in_box(p: NormPoint, box: NormBox) -> bool:
    return box.x1 <= p.x <= box.x2 and box.y1 <= p.y <= box.y2


GRIPPER_ROI_WRIST = NormBox(x1=300, y1=350, x2=700, y2=900)
"""Where the space between the SO-101 jaws appears in the WRIST camera frame (0-1000 grid). The verifier's
gripper_holding rule: a detected box overlapping this ROI with IoU > HELD_ROI_MIN_IOU whose mention matches the product
= 'holding'. CALIBRATE on the Isaac wrist camera mount (contracts.cameras DEFAULT_ROBOT_CAMERA_MOUNTS) before trusting
mode 'decide'; the 2D fake draws the held unit at (350, 350)-(650, 650), inside this ROI."""
HELD_ROI_MIN_IOU = 0.2


class ProviderCall(Model):
    """One vision-model call (or the fake / ground-truth stand-in): attribution + debugging for every overlay."""

    provider: VisionProviderName
    model: str | None = Field(None, description="'perceptron-mk1.5' / 'perceptron-mk1' / 'gemini-3.8-flash' / None.")
    call: str = Field(description="'detect' · 'question:point' · 'question:box' · 'ocr' · 'multilook' · 'sidecar'.")
    prompt: str | None = Field(None, description="Short prompt shown in the UI: 'Point to the mild salsa jar'.")
    request_id: str | None = Field(None, description="Provider request id (PerceiveResult.request_id).")
    latency_ms: float = 0.0
    error_code: str | None = Field(None, description="SDK error code when the call failed ('rate_limit', 'timeout').")
    ts: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attribution(self) -> str:
        if self.provider == "perceptron":
            return f"Perceptron {self.model or ''}".strip()
        labels = {"ground_truth": "Sim ground truth", "state": "Robot state", "fake": "Fake verifier"}
        return labels.get(self.provider, self.model or self.provider)


EvidenceKind = Literal["box", "point", "text", "state"]
EvidenceRole = Literal["target", "held", "basket_item", "label", "gripper_roi", "slot", "distractor", "state"]


class Evidence(Model):
    """One grounded fact behind a verdict / grounding, drawn as an overlay on ``frame.url``."""

    kind: EvidenceKind
    role: EvidenceRole = "target"
    frame: FrameRef | None = None
    mention: str = Field("", description="What it is: 'Sol de Casa Mild Chunky Salsa 16 oz', 'empty gripper'.")
    box: NormBox | None = None
    point: NormPoint | None = None
    text: str | None = Field(None, description="OCR / answer text read off the image, or the state fact.")
    sku: Sku | None = None
    instance_id: InstanceId | None = Field(None, description="Sim ground-truth unit, when known.")
    confidence: Score01 = 1.0
    matches_expectation: bool | None = Field(None, description="Overlay colour: True green, False red, None grey.")


class SlotLocation(Model):
    """Where a slot is, resolved by the planner from the planogram (``catalog.get_locations`` / ``get_planogram``)."""

    slot_id: SlotId
    aisle_label: str | None = None
    level: int | None = None
    approach_pose: Pose2D = Field(description="Robot base pose facing the slot (Slot.approach_pose).")
    item_position: Point3 = Field(description="Front-center of the pick face (Slot.item_position).")
    lift_height_m: float | None = Field(None, description="Lift pre-position for this shelf level (LIFT_MIN..MAX).")


class TargetSpec(Model):
    """What a grounding / grasp is about. The planner fills ``description`` + ``label_hints`` from the catalog."""

    sku: Sku
    slot_id: SlotId | None = None
    instance_id: InstanceId | None = Field(None, description="A specific unit (the shopper's freshness pick).")
    description: str = Field(description="Visual query: 'Sol de Casa Mild Chunky Salsa, 16 oz jar'.")
    label_hints: list[str] = Field(default_factory=list, description="Text expected on the label: brand, size.")
    location: SlotLocation | None = None


class GroundedTarget(Model):
    """Result of grounding a TargetSpec in a frame: where to grasp. Stored on the plan blackboard (``bindings``)."""

    grounding_id: GroundingId
    target: TargetSpec
    frame: FrameRef | None = Field(None, description="Frame it was found in (None = state / ground truth only).")
    box: NormBox | None = None
    grasp_point: NormPoint | None = Field(None, description="Where to aim the gripper (box center if None).")
    confidence: Score01
    decided_by: DecisionSource = "provider"
    provider: ProviderCall | None = None
    instance_id: InstanceId | None = Field(None, description="Matched sim unit (ground truth / fake), if known.")
    candidates: list[Evidence] = Field(default_factory=list, description="Other boxes considered.")
    robot_id: RobotId | None = None
    robot_pose: Pose2D | None = Field(None, description="Robot pose when grounded (staleness check).")
    ts: Timestamp = Field(default_factory=utcnow)

    def is_fresh(self, now: datetime, pose: Pose2D | None, *, max_age_s: float, max_motion_m: float) -> bool:
        """Stale when old, or when the robot moved (the box lives in a camera frame that moved with it)."""
        if (now - self.ts).total_seconds() > max_age_s:
            return False
        if pose is not None and self.robot_pose is not None and pose.distance_to(self.robot_pose) > max_motion_m:
            return False
        return True
