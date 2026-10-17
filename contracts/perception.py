"""Perception contracts: detections, scan reports, freshness ranking, basket verification. Owned by perception.

Input frames come from ``RobotService.scan/capture``; in sim/fake mode ``CameraFrame.instances`` carries the ground
truth (sku, instance_id, lot_id, freshness, pixel bbox) for the ground-truth bypass and for crops/thumbnails.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import Field

from contracts.common import (
    BBox,
    Cents,
    FrameId,
    InstanceId,
    LotId,
    Model,
    Pose2D,
    ScanId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    TripId,
    utcnow,
)

DetectionMethod = Literal["barcode", "embedding", "vlm", "ground_truth"]

PICK_CHOICE_MAX = 3
"""PickChoice holds at most this many candidates (the shopper-facing top-3)."""


class Detection(Model):
    sku: Sku | None = Field(None, description="None = unidentified object.")
    confidence: Score01
    method: DetectionMethod
    bbox: BBox | None = None
    instance_id: InstanceId | None = None
    lot_id: LotId | None = Field(None, description="Lot of the detected unit (ground truth), if known.")
    text: str | None = Field(None, description="Raw barcode / label text read, if any.")


class ScanReport(Model):
    """Result of scanning one slot; catalog.apply_scan turns it into stock/price/location updates."""

    scan_id: ScanId
    store_id: StoreId
    slot_id: SlotId | None = None
    expected_sku: Sku | None = None
    detections: list[Detection] = Field(default_factory=list)
    observed_count: int = Field(0, ge=0, description="Units of expected_sku seen.")
    count_is_exact: bool = Field(
        False,
        description="True = observed_count is the slot's total (ground truth); False = visible front facings only "
        "(real CV): catalog then only lowers qty to 0 when observed_count == 0, never to the facing count.",
    )
    mismatch: bool = Field(False, description="Wrong/misplaced item or empty slot vs the planogram.")
    freshness: dict[InstanceId, Score01] = Field(default_factory=dict)
    lot_freshness: dict[LotId, Score01] = Field(
        default_factory=dict, description="Mean freshness per Lot (what catalog stores)."
    )
    shelf_tag_cents: Cents | None = Field(None, description="Price read off the shelf tag (mispricing check).")
    pose: Pose2D | None = Field(None, description="Robot pose at capture (frame.pose); locates slot-less scans.")
    frame_id: FrameId | None = None
    trip_id: TripId | None = None
    latency_ms: float = 0.0
    notes: str = ""
    ts: Timestamp = Field(default_factory=utcnow)


class FreshnessCandidate(Model):
    instance_id: InstanceId
    sku: Sku
    score: Score01 = Field(description="1.0 = freshest.")
    rank: int = Field(ge=1)
    reason: str = Field("", description="'Firm, deep green, no bruising'.")
    bbox: BBox | None = None
    thumbnail_b64: str | None = Field(None, description="Face-free JPEG crop, base64.")


class PickChoice(Model):
    """Top-k produce candidates for the shopper to choose from (rank 1 = recommended)."""

    choice_id: str
    slot_id: SlotId
    sku: Sku
    candidates: list[FreshnessCandidate] = Field(max_length=PICK_CHOICE_MAX)
    frame_id: FrameId | None = None

    @property
    def recommended(self) -> FreshnessCandidate | None:
        return min(self.candidates, key=lambda c: c.rank) if self.candidates else None


class BasketCheck(Model):
    """Expected vs observed basket contents as SKU multisets."""

    expected: dict[Sku, int] = Field(default_factory=dict)
    observed: dict[Sku, int] = Field(default_factory=dict)
    ok: bool
    missing: dict[Sku, int] = Field(default_factory=dict)
    unexpected: dict[Sku, int] = Field(default_factory=dict)
    method: DetectionMethod = "ground_truth"
    frame_id: FrameId | None = None

    @classmethod
    def compare(
        cls,
        expected: Mapping[Sku, int] | Iterable[Sku],
        observed: Mapping[Sku, int] | Iterable[Sku],
        *,
        method: DetectionMethod = "ground_truth",
        frame_id: FrameId | None = None,
    ) -> "BasketCheck":
        exp = Counter(expected) if not isinstance(expected, Mapping) else Counter(dict(expected))
        obs = Counter(observed) if not isinstance(observed, Mapping) else Counter(dict(observed))
        missing = {k: v for k, v in sorted((exp - obs).items())}
        unexpected = {k: v for k, v in sorted((obs - exp).items())}
        return cls(
            expected=dict(sorted(exp.items())),
            observed=dict(sorted(obs.items())),
            ok=not missing and not unexpected,
            missing=missing,
            unexpected=unexpected,
            method=method,
            frame_id=frame_id,
        )
