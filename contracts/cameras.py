"""Camera contracts: camera registry (robot + store ceiling cameras), mounts, vision observations. Owned by perception
(``CameraService`` in contracts/services.py); frames are ``contracts.sim.CameraFrame`` (also importable from here).

Video input everywhere: every robot streams a ``front`` mast camera, an SO-101 ``wrist`` camera and a ``basket``
camera; every store has ceiling cameras. Camera ids are fixed strings:

- robot cameras:   ``"<robot_id>.front"``, ``"<robot_id>.wrist"``, ``"<robot_id>.basket"``  (``robot_camera_id``)
- store cameras:   ``"<store_id>.ceiling-<n>"`` (n from 1)                                      (``store_camera_id``)
- 0.2.3 livestream cameras (listed by ``CameraService.list_cameras`` when enabled):
  - ``"<robot_id>.director"`` (kind ``director``): third-person chase view of the rover (arm + basket + the shelf it
    faces), rendered by the Isaac worker (``PICKY_SIM_DIRECTOR``) or the 2D fake (``director_camera_id``);
  - ``"<robot_id>.front.perceived"`` / ``"<robot_id>.wrist.perceived"`` (kind ``perceived``): that onboard camera's
    live frames with detection boxes drawn on them (Perceptron Mk1.5, ground truth as fallback), produced by
    ``CameraService`` itself; ``CameraInfo.derived_from`` names the source camera (``perceived_camera_id``).

Privacy (CLAUDE.md §8): frames from the sim / robot fake are RAW; ``CameraService`` blurs faces
(``perception.blur_faces``) BEFORE a frame is served (snapshot / MJPEG / WS), stored, or sent to an LLM, so every
frame leaving ``CameraService`` has ``blurred=True``.

This module is a leaf (imports only contracts.common) so contracts/sim.py can use ``CameraInfo`` in ``SimHello``.
"""

from typing import Any, Literal

from pydantic import Field, computed_field

from contracts.common import (
    BBox,
    CameraId,
    Cents,
    FrameId,
    GoalId,
    Model,
    ObservationId,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    WireModel,
    utcnow,
)

CameraKind = Literal["front", "wrist", "basket", "ceiling", "director", "perceived"]
"""``director`` / ``perceived`` (0.2.3) are livestream views: the chase camera and an annotated onboard stream."""
RobotCameraKind = Literal["front", "wrist", "basket"]
ROBOT_CAMERA_KINDS: tuple[RobotCameraKind, ...] = ("front", "wrist", "basket")
DIRECTOR_KIND: Literal["director"] = "director"
PERCEIVED_SUFFIX: Literal["perceived"] = "perceived"
PERCEIVED_SOURCE_KINDS: tuple[RobotCameraKind, ...] = ("front", "wrist")
"""Onboard cameras that have a ``.perceived`` (annotated) stream."""
CameraSource = Literal["isaac", "fake", "none"]
"""Who renders a camera right now: the Isaac worker, the 2D robot fake (Pillow), or nobody (offline)."""
ObservationMethod = Literal["vlm", "ground_truth", "cv", "fake"]


class CameraMount(WireModel):
    """Where a camera is and where it looks.

    ``frame='robot_base'``: x forward, y left, z up from the rover base center at floor level (moves with the robot).
    ``frame='gripper'``: rides the SO-101 wrist, origin at the gripper's ``gripper_frame_link`` (the TCP between the
    jaws), +x along the approach direction (out of the jaws), +y to the left jaw, +z up when the gripper is level; the
    camera sits at (x, y, z) in that frame. Numbers are nominal (arm at its scan pose).
    ``frame='world'``: store frame (ceiling cameras; and ``CameraFrame.camera_pose``, the pose actually used for a
    frame). ``yaw`` CCW from +x of the frame; ``pitch`` < 0 = down.

    The robot ``front`` camera is a PAN-TILT HEAD on top of the mast: the mount's yaw/pitch is its REST pose (used for
    streaming); ``scan`` (and ``capture`` aimed at a slot) points it with look-at at ``slot.item_position`` raised by
    half the item height, so bottom-shelf (z 0.1) and top-shelf (z 1.5) slots are fully in view from the 0.35-0.40 m
    approach distance. Frames record the pose used in ``CameraFrame.camera_pose``.
    """

    frame: Literal["robot_base", "gripper", "world"] = "robot_base"
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    hfov_deg: float = Field(70.0, gt=0, lt=180)
    width: int = Field(640, ge=16)
    height: int = Field(480, ge=16)


DEFAULT_ROBOT_CAMERA_MOUNTS: dict[str, CameraMount] = {
    # pan-tilt head on the mast (rest pose here), looking forward and down at the shelf the rover faces
    "front": CameraMount(frame="robot_base", x=0.10, y=0.0, z=1.45, pitch=-0.55, hfov_deg=100.0),
    # SO-101 wrist camera (close-ups: labels, price tags, produce freshness, grasp check)
    "wrist": CameraMount(frame="gripper", x=0.0, y=0.0, z=0.0, pitch=0.0, hfov_deg=87.0),
    # looking straight down into the basket at the back of the rover (basket verification)
    "basket": CameraMount(frame="robot_base", x=-0.12, y=0.0, z=0.85, pitch=-1.5708, hfov_deg=90.0),
}
"""Shared by the Isaac scene, the 2D fake renderer and CameraInfo for robot cameras (keep them in sync)."""


def robot_camera_id(robot_id: RobotId, kind: str) -> CameraId:
    """'rover-ave-1', 'front' -> 'rover-ave-1.front' ('head' is accepted as 'front')."""
    kind = "front" if kind == "head" else kind
    return f"{robot_id}.{kind}"


def store_camera_id(store_id: StoreId, n: int = 1) -> CameraId:
    """'store_ave', 2 -> 'store_ave.ceiling-2'."""
    return f"{store_id}.ceiling-{n}"


def director_camera_id(robot_id: RobotId) -> CameraId:
    """'rover-ave-1' -> 'rover-ave-1.director' (0.2.3: the third-person chase camera of that rover)."""
    return f"{robot_id}.{DIRECTOR_KIND}"


def perceived_camera_id(robot_id: RobotId, kind: str = "front") -> CameraId:
    """'rover-ave-1', 'front' -> 'rover-ave-1.front.perceived' (0.2.3: that onboard camera with detection boxes)."""
    kind = "front" if kind == "head" else kind
    if kind not in PERCEIVED_SOURCE_KINDS:
        raise ValueError(f"no perceived stream for camera kind {kind!r}")
    return f"{robot_id}.{kind}.{PERCEIVED_SUFFIX}"


def perceived_source(camera_id: CameraId) -> CameraId | None:
    """'rover-ave-1.front.perceived' -> 'rover-ave-1.front'; None for any other camera id."""
    base, sep, name = camera_id.rpartition(".")
    if not sep or name != PERCEIVED_SUFFIX:
        return None
    owner, sep2, kind = base.rpartition(".")
    if not sep2 or not owner or kind not in PERCEIVED_SOURCE_KINDS:
        return None
    return base


def parse_camera_id(camera_id: CameraId) -> tuple[str, CameraKind]:
    """'rover-ave-1.wrist' -> ('rover-ave-1', 'wrist'); 'store_ave.ceiling-2' -> ('store_ave', 'ceiling');
    'rover-ave-1.director' -> ('rover-ave-1', 'director'); 'rover-ave-1.front.perceived' -> ('rover-ave-1',
    'perceived') (the source camera: ``perceived_source``).

    The owner is a robot_id for robot cameras and a store_id for ceiling cameras. Raises ValueError otherwise.
    """
    owner, sep, name = camera_id.rpartition(".")
    if not sep or not owner:
        raise ValueError(f"bad camera id {camera_id!r} (expected '<robot_id>.<kind>' or '<store_id>.ceiling-<n>')")
    if name in ROBOT_CAMERA_KINDS:
        return owner, name  # type: ignore[return-value]
    if name == "ceiling" or name.startswith("ceiling-"):
        return owner, "ceiling"
    if name == DIRECTOR_KIND:
        return owner, DIRECTOR_KIND
    if name == PERCEIVED_SUFFIX:
        source = perceived_source(camera_id)
        if source is not None:
            return source.rpartition(".")[0], PERCEIVED_SUFFIX
    raise ValueError(f"bad camera id {camera_id!r}: unknown camera {name!r}")


class CameraInfo(WireModel):
    """One camera, as listed by ``CameraService.list_cameras`` / GET /api/cameras (and in ``SimHello.cameras``).

    ``snapshot_url`` / ``stream_url`` are ROOT-RELATIVE to the gateway base URL ('/api/cameras/...'): the web app
    prefixes its API base (e.g. ``http://localhost:8000``) or proxies ``/api`` to the gateway (Next.js rewrite).
    Both accept ``?w=<px>`` (scale to that width); add ``?t=<ms>`` to snapshot URLs to defeat caching when polling."""

    camera_id: CameraId
    kind: CameraKind
    name: str = Field(description="Display name, e.g. 'Pico · front' or 'Ave ceiling 1'.")
    store_id: StoreId | None = None
    robot_id: RobotId | None = Field(None, description="Owning robot; None for store ceiling cameras.")
    mount: CameraMount = Field(default_factory=CameraMount)
    source: CameraSource = "fake"
    online: bool = True
    fps: float | None = Field(None, description="Measured live frame rate, if streaming.")
    last_frame_at: Timestamp | None = None
    derived_from: CameraId | None = Field(
        None, description="0.2.3, kind 'perceived': the onboard camera whose live frames it annotates."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def snapshot_url(self) -> str:
        return f"/api/cameras/{self.camera_id}/snapshot.jpg"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stream_url(self) -> str:
        """MJPEG (multipart/x-mixed-replace): usable directly as ``<img src=...>``."""
        return f"/api/cameras/{self.camera_id}/stream.mjpg"


class ObservedItem(Model):
    """One thing the vision step saw in a frame."""

    label: str = Field(description="What it is, in words: 'Casa Verde Mild Salsa 16 oz jar'.")
    sku: Sku | None = Field(None, description="Catalog SKU when identified.")
    count: int = Field(1, ge=0)
    confidence: Score01 = 1.0
    bbox: BBox | None = None
    slot_id: SlotId | None = None
    shelf_tag_cents: Cents | None = Field(None, description="Price read off the shelf tag next to it, if any.")
    freshness: Score01 | None = Field(None, description="Produce only: 1.0 = perfectly fresh.")


class VisionObservation(Model):
    """Answer of ``CameraService.observe`` (the robot brain's ``look`` tool, POST /api/cameras/{id}/observe).

    Computed from a face-BLURRED frame. Real: ``claude-haiku-5-5`` vision via utils.llm (+ ground truth hints in
    sim); fake: built from the frame's ground-truth ``instances``.
    """

    observation_id: ObservationId
    camera_id: CameraId
    robot_id: RobotId | None = None
    store_id: StoreId | None = None
    goal_id: GoalId | None = None
    frame_id: FrameId | None = None
    question: str | None = Field(None, description="What was asked ('Is the mild salsa in stock?'); None = describe.")
    answer: str = Field(description="Natural-language description / answer to the question.")
    items: list[ObservedItem] = Field(default_factory=list)
    people: int = Field(0, ge=0, description="People (faces) seen; their faces were blurred before analysis.")
    blurred: bool = Field(True, description="Always True: observations are made on blurred frames only.")
    method: ObservationMethod = "fake"
    model: str | None = Field(None, description="Vision model id when method='vlm'.")
    latency_ms: float = 0.0
    ts: Timestamp = Field(default_factory=utcnow)


def __getattr__(name: str) -> Any:  # lazy re-export: contracts.sim imports this module (no import cycle)
    if name == "CameraFrame":
        from contracts.sim import CameraFrame

        return CameraFrame
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
