"""Wire protocol between the gateway and the Isaac Sim worker (multi-store, multi-robot), plus the SimLink Protocol.

Topology: ONE Isaac worker (GPU host "vhagar") hosts SEVERAL stores (one scene per store, side by side) and SEVERAL
rovers (SO-101 on a lift; contracts.common robot model). It multiplexes everything over one websocket by
``robot_id`` (robot commands / states / frames) and ``store_id`` (scenes, ceiling cameras). The 2D robot fake
(arms/robot) implements the same semantics in-process and is the always-available fallback.

Transport
- The sim worker connects OUTBOUND to the gateway at ``ws /ws/sim``. First message is ``SimHello``; auth is
  ``SimHello.token`` == PICKY_SIM_TOKEN (a ``?token=`` query param is optional and only an early reject).
- ``SimHello`` lists what the worker hosts: ``stores`` (store_id + ``loaded`` + ``data_hash`` = ``planogram_hash`` of
  the layout the scene was built from + ceiling camera ids), ``robots`` (robot_id, store_id, camera ids) and
  ``cameras`` (``CameraInfo`` for every camera). After ANY reset that changes what it hosts (a store built / rebuilt,
  robots spawned) the worker sends ``SimHosted`` with the full new lists; ``SimLink.subscribe`` handlers and
  ``SimLink.status()`` see both.
- Version check: the gateway compares ``SimHello.contracts_version`` major.minor with ``CONTRACTS_VERSION`` and answers
  ``SimWelcome(ok=False, error="contracts mismatch ...")`` (then closes) when they differ (so v0.1 workers are refused).
  Otherwise ``SimWelcome(ok=True)``, after which the worker immediately sends one ``RobotState`` PER ROBOT.
- Then the gateway sends ``SimCommand``s (each with a unique ``cmd_id``) and the worker sends exactly one
  ``CommandResult`` per command (echoing ``robot_id``), plus unsolicited ``RobotState`` (per robot, on change,
  <= 10 Hz), ``CameraFrame`` (only for the cameras selected by ``SetStreamsCmd``; default after welcome: every robot's
  ``front`` camera at 2 fps), ``InstanceInfo`` (after reset and whenever instances move), ``SimHosted`` and ``SimLog``.
  Every message is a JSON object with a ``type`` field.
- Tolerance: the two ends run from different checkouts. Top-level wire messages are ``WireModel`` (unknown fields
  ignored) and NESTED models (Planogram, Product, RobotSpec, Pose2D, ...) are ``Model`` (extra='forbid'), so both ends
  MUST parse with ``parse_sim_message`` / ``parse_gateway_to_sim``: they drop unknown keys at any depth and retry, so
  an additive (patch) change on one side never breaks the link. A command that still fails validation but whose raw
  JSON has a ``cmd_id`` is answered with ``command_error_result(raw, exc)`` (ok=False, error_code='internal') instead
  of being dropped (the gateway would otherwise wait out the timeout).

Which backend drives a store (decided by the robot arm, PER STORE, never per robot)
- A store is on Isaac iff a connected worker lists it ``loaded`` with ``data_hash == planogram_hash(<live
  planogram>)``; then ALL of that store's robots go to Isaac, otherwise ALL go to the 2D fake (never a mix: two worlds
  would diverge). On hello (and on ``SimHosted``), for every store the worker lists as stale or not loaded, the robot
  arm sends a store-level ``ResetCmd(robot_id=None, store_id, planogram, products, robots, cameras)`` before routing
  any of its robots there. A store moves from the fake back to Isaac only when all its robots are IDLE with an empty
  gripper and basket (between goals); the store-level reset then respawns them at their docks.
- Isaac loss (disconnect / worker crash): the in-flight command of each affected robot raises
  ``RobotError('disconnected')`` ONCE; the robot then continues on the 2D fake from its last ``RobotState`` (pose,
  basket instances, holding, battery), with NO reset, so the brain re-plans from where it is.

Command rules
- ``robot_id`` semantics are per command (there is NO default robot on the wire):
  navigate / scan / pick / place_in_basket / unload_basket / arm_joints: REQUIRED (missing or unknown -> ok=False,
  error_code='unknown_robot'). capture: ``camera_id`` (any camera incl. ceiling) or ``robot_id`` + ``camera``
  (neither -> unknown_robot). stop: None = EVERY robot (emergency stop). reset: None = (re)build the whole
  ``store_id`` scene (store_id required). list_instances / set_slot_stock: store-level (``store_id`` required, or the
  robot's store). set_streams: global (robot_id ignored).
- One MOTION command (navigate / scan / pick / place_in_basket / unload_basket / reset / arm_joints) runs at a time
  PER ROBOT; a
  second one for the same robot while busy resolves immediately with ``ok=False, error_code="busy"``. Different robots
  move concurrently. ``capture``, ``list_instances``, ``set_streams``, ``set_slot_stock`` and ``stop`` are always
  allowed. ``stop`` makes that robot's in-flight motion resolve ``ok=False, error_code="cancelled"``.
- Failures carry a machine-readable ``CommandResult.error_code`` (``SimErrorCode``). ``RobotService`` methods raise
  ``RobotError(code, message)`` with the same codes (the fake too), so callers branch on codes, not strings.
- ``navigate``: intermediate waypoint thetas are ignored (path heading); the final waypoint's position must be reached
  within ``tolerance_m`` and its theta within ``yaw_tolerance_rad``. Rovers in the same store avoid each other
  (``path_blocked`` if another rover sits on the path for > 10 s). Below ``BATTERY_MOTION_MIN_PCT`` motion is refused
  with ``battery_low`` (driving to the dock still works).
- ``pick`` in sim = scripted "magic grasp": drive the lift to the item height (``LIFT_MIN_M``..``LIFT_MAX_M``), move
  the SO-101 to the item, attach (FixedJoint), retract. Reach is ``contracts.common.slot_reachable(robot pose,
  slot.approach_pose, slot.item_position)``: it checks the SLOT, never the unit, so any instance of a reachable slot
  (incl. back rows) can be grasped; unreachable -> ``grasp_failed``. ``place_in_basket`` reparents the item into the
  basket at ``BASKET_OFFSET_LOCAL``; ``unload_basket`` (at checkout) removes every basket instance from the scene.
- ``arm_joints`` (0.2.2, worker capability ``ARM_JOINTS_CAPABILITY``): joint-level SO-101 control for learned
  policies; the robot arm runs the policy loop, one command per action chunk, and gets the joint state + the two
  policy-camera frames back in the same result (``ArmJointsCmd`` docstring). Grasps there are the "sticky grasp"
  physics substitute (attach on close within ~3 cm of the jaw center), reported in ``ArmJointState.events``.
  ``arm_ik`` (0.2.2, capability ``ARM_IK_CAPABILITY``, not a motion: always allowed) solves TCP targets on the
  worker's SO-101 model into joint rows (Cartesian policies), never moves.
- ``set_slot_stock`` makes a slot hold exactly ``qty`` units (lots in planogram order): units are spawned / removed
  deterministically (``instance_id(slot_id, n)``, removing the highest n = back rows first) so it matches catalog
  restocks and write-offs (``RobotService.sync_stock``).
- Disconnect: the gateway fails in-flight ``SimLink.send`` calls with ``ConnectionError`` (the robot arm maps it to
  ``RobotError("disconnected")``, see above); the worker keeps its scenes and aborts in-flight actions; results for
  unknown ``cmd_id``s are dropped. ``SimLink.send(timeout_s=None)`` uses ``SIM_DEFAULT_TIMEOUTS_S`` (else 15 s).

Frames: every pose / position on the wire is in the STORE frame (store-local; USD Z-up, metersPerUnit=1; each store is
a translation-only ``/World/Stores/<store_id>`` Xform, see contracts/planogram.py), never stage coordinates. Frames
from the sim are RAW (``blurred=False``); perception blurs faces before anything is stored, streamed or sent to an
LLM. Ground truth: scan results' ``frame.instances`` list EVERY non-basket instance of the scanned slot (``bbox`` None
when off-image or occluded) = the exact count; capture results may list only the visible instances; unsolicited
stream frames carry ``instances=[]``. ``CameraFrame.camera_pose`` is the camera pose actually used (the front camera
is a pan-tilt head aimed at the slot for scans). Camera ids and mounts: contracts/cameras.py.
"""

import copy
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field, TypeAdapter, ValidationError, field_validator

from contracts.cameras import CameraInfo, CameraMount
from contracts.catalog import Lot, Product
from contracts.common import (
    BBox,
    CameraId,
    CameraName,
    CmdId,
    FrameId,
    InstanceId,
    LotId,
    Model,
    Point3,
    Pose2D,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    WireModel,
    utcnow,
)
from contracts.planogram import Planogram, RobotSpec, StoreCameraSpec
from contracts.skill_server import SO101_JOINT_NAMES

SimErrorCode = Literal[
    "unknown_slot",
    "no_instance",
    "grasp_failed",
    "path_blocked",
    "timeout",
    "cancelled",
    "busy",
    "not_ready",
    "disconnected",
    "unknown_robot",
    "battery_low",
    "internal",
]
"""unknown_slot / no_instance (slot empty -> re-source online) · grasp_failed (out of reach / slipped: retry, attach
fallback, or skip) · path_blocked / timeout (re-plan or skip) · cancelled (StopCmd) · busy (motion already running for
that robot) · not_ready (no scene) · disconnected (Isaac lost: raised once, then the robot continues on the 2D fake
from its last state; retryable) · unknown_robot (robot / camera id missing or not hosted) · battery_low (refuses motion
below BATTERY_MOTION_MIN_PCT: send it to its dock) · internal (incl. a command the worker could not parse)."""

SIM_DEFAULT_TIMEOUTS_S: dict[str, float] = {"reset": 120.0, "navigate": 90.0, "arm_joints": 30.0}
"""Per-command default for ``SimLink.send(timeout_s=None)``; any other command: ``SIM_DEFAULT_TIMEOUT_S``."""
SIM_DEFAULT_TIMEOUT_S = 15.0


class RobotError(Exception):
    """Raised by every RobotService method on failure (real and fake). ``code`` is a SimErrorCode."""

    def __init__(self, code: SimErrorCode, message: str = "") -> None:
        self.code: SimErrorCode = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


# --------------------------------------------------------------------------------------------------------------------
# State + data
# --------------------------------------------------------------------------------------------------------------------
class RobotStatus(StrEnum):
    """Live motion status of one robot (also ``contracts.fleet.RobotStatus``)."""

    IDLE = "idle"
    NAVIGATING = "navigating"
    SCANNING = "scanning"
    PICKING = "picking"
    PLACING = "placing"
    CHARGING = "charging"
    ERROR = "error"
    DISCONNECTED = "disconnected"


RobotBackend = Literal["isaac", "fake"]
"""What drives a robot right now: the Isaac worker or the in-process 2D fake (fallback)."""


class ItemInstance(WireModel):
    """One physical unit in the sim (ground truth)."""

    instance_id: InstanceId = Field(description="'<slot_id>#nn'; globally unique because slot ids are.")
    sku: Sku
    store_id: StoreId | None = Field(None, description="Store the unit is in (multi-store workers set it).")
    slot_id: SlotId | None = None
    position: Point3 = Field(description="Item CENTER in the STORE frame (store-local meters, z up).")
    freshness: Score01 | None = None
    lot_id: LotId | None = Field(None, description="Planogram Lot this unit belongs to.")
    in_basket: bool = False
    bbox: BBox | None = Field(None, description="Pixel box in the frame this instance rides with (None otherwise).")


# ---- shared ground truth: the robot fake (2D) and the Isaac scene MUST agree on these, because the gateway falls back
# from the sim robot to the fake mid-trip; ids picked from sim frames (PickChoice) must still exist afterwards.
BASKET_OFFSET_LOCAL: tuple[float, float, float] = (-0.12, 0.0, 0.40)
"""Center of the basket floor in the robot base frame (x forward, y left, z up), meters: the fixed drop pose.
The basket rides on the back half of the rover (inside ROBOT_RADIUS_M), within SO-101 reach of the lift mast."""
BASKET_SIZE_M: tuple[float, float, float] = (0.20, 0.30, 0.16)
"""Basket inner extent (x, y, z), meters (~12 grocery units in two layers: RobotSpec.basket_capacity)."""
UNIT_FRESHNESS_JITTER = 0.08
"""Per-unit spread (+-) around the lot freshness, so the produce top-3 ranking is meaningful."""


def instance_id(slot_id: SlotId, n: int) -> InstanceId:
    """Ground-truth id of the ``n``-th unit of a slot (front units first, lot by lot in planogram order)."""
    return f"{slot_id}#{n:02d}"


def unit_freshness(lot_freshness: float | None, instance_id: InstanceId) -> float | None:
    """Freshness of one unit: lot freshness +- UNIT_FRESHNESS_JITTER, deterministic per instance id, in [0, 1]."""
    if lot_freshness is None:
        return None
    h = hashlib.sha256(instance_id.encode("utf-8")).digest()
    jitter = (int.from_bytes(h[:4], "big") / 0xFFFFFFFF) * 2.0 - 1.0
    return round(min(1.0, max(0.0, lot_freshness + UNIT_FRESHNESS_JITTER * jitter)), 3)


class RobotState(WireModel):
    """Live state of ONE robot. Every producer sets ``robot_id`` and ``store_id`` (the field is optional only so a
    malformed message still parses; consumers drop states without a robot_id). ``battery_pct`` is owned by the robot
    arm (contracts.common battery model); a worker-reported value is advisory."""

    type: Literal["robot_state"] = "robot_state"
    robot_id: RobotId | None = None
    store_id: StoreId | None = None
    pose: Pose2D
    status: RobotStatus = RobotStatus.IDLE
    holding: InstanceId | None = None
    basket: list[InstanceId] = Field(default_factory=list)
    basket_skus: list[Sku] = Field(default_factory=list, description="SKU per basket instance (same order).")
    lift_height_m: float | None = Field(None, description="SO-101 shoulder height on the lift (LIFT_MIN_M..MAX).")
    battery_pct: float | None = Field(None, ge=0, le=100)
    charging: bool = False
    backend: RobotBackend | None = Field(None, description="Set by the robot arm: 'isaac' or 'fake'.")
    current_cmd_id: CmdId | None = None
    error: str | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class CameraFrame(WireModel):
    """One camera frame. Robot cameras set ``robot_id``; every producer sets ``camera_id`` (contracts/cameras.py)."""

    type: Literal["camera_frame"] = "camera_frame"
    frame_id: FrameId
    camera: CameraName = "front"
    camera_id: CameraId | None = Field(None, description="'rover-ave-1.front' / 'store_ave.ceiling-1'.")
    robot_id: RobotId | None = None
    store_id: StoreId | None = None
    jpeg_b64: str = Field(description="Base64 JPEG bytes (no data: prefix).")
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    blurred: bool = Field(False, description="True only after perception.blur_faces.")
    slot_id: SlotId | None = None
    pose: Pose2D | None = Field(None, description="Robot pose when captured.")
    camera_pose: CameraMount | None = Field(
        None, description="Camera pose + intrinsics actually used (frame='world', store frame; pan-tilt aim for scans)."
    )
    instances: list[ItemInstance] = Field(
        default_factory=list,
        description="Sim ground truth (sim / robot fake only): scan results = EVERY non-basket unit of the slot (bbox "
        "None when off-image) = exact count; capture = visible units; unsolicited stream frames = [].",
    )
    ts: Timestamp = Field(default_factory=utcnow)


class InstanceInfo(WireModel):
    """Unsolicited ground-truth update (after reset / when instances move), optionally with per-frame pixel boxes."""

    type: Literal["instances"] = "instances"
    store_id: StoreId | None = None
    robot_id: RobotId | None = Field(None, description="Set when the change came from that robot (pick/place).")
    slot_id: SlotId | None = None
    frame_id: FrameId | None = None
    instances: list[ItemInstance] = Field(default_factory=list)
    bboxes: dict[InstanceId, BBox] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------------------------
# Commands (gateway -> sim)
# --------------------------------------------------------------------------------------------------------------------
class _Cmd(WireModel):
    cmd_id: CmdId
    robot_id: RobotId | None = Field(
        None, description="Target robot (multiplexing key). Meaning of None is per command (module docstring)."
    )


class NavigateCmd(_Cmd):
    type: Literal["navigate"] = "navigate"
    waypoints: list[Pose2D] = Field(min_length=1, description="Intermediate thetas ignored; final theta reached.")
    speed_mps: float | None = None
    tolerance_m: float = 0.05
    yaw_tolerance_rad: float = 0.05


class ScanCmd(_Cmd):
    """Aim the front pan-tilt camera at a slot and capture; the result's ``frame.instances`` lists EVERY non-basket
    unit of the slot (bbox None when off-image) and ``frame.camera_pose`` the pose used."""

    type: Literal["scan"] = "scan"
    slot_id: SlotId
    camera: CameraName = "front"


class PickCmd(_Cmd):
    type: Literal["pick"] = "pick"
    slot_id: SlotId
    sku: Sku
    instance_id: InstanceId | None = Field(None, description="None = any instance of the SKU in the slot.")


class PlaceInBasketCmd(_Cmd):
    type: Literal["place_in_basket"] = "place_in_basket"
    instance_id: InstanceId | None = Field(None, description="None = whatever the gripper holds.")


class CaptureCmd(_Cmd):
    """Capture one frame: ``camera_id`` (any camera, incl. store ceiling cameras) wins over robot_id + camera."""

    type: Literal["capture"] = "capture"
    camera: CameraName = "front"
    camera_id: CameraId | None = None


class UnloadBasketCmd(_Cmd):
    """Motion command (at checkout): remove every instance from the robot's basket; the result's ``instances`` lists
    what was unloaded."""

    type: Literal["unload_basket"] = "unload_basket"


class ListInstancesCmd(_Cmd):
    """Ground truth in one store: ``store_id`` (or the robot's store) + that robot's basket."""

    type: Literal["list_instances"] = "list_instances"
    store_id: StoreId | None = None
    slot_id: SlotId | None = Field(None, description="None = all instances (incl. baskets).")


class ResetCmd(_Cmd):
    """(Re)build a store scene and/or respawn a robot.

    - ``robot_id`` set: put that robot at its dock with an empty basket and ``battery_pct`` from its spec, building
      ``store_id``'s scene first if it is not loaded (motion command for that robot). ``store_id`` must be the robot's
      own store (else unknown_robot).
    - ``robot_id`` None: (re)build ``store_id``'s scene (store_id REQUIRED) and respawn every robot in it (cancels
      their motions); this is how a stale ``data_hash`` is fixed. Followed by ``SimHosted``.
    When ``planogram`` / ``products`` / ``robots`` / ``cameras`` are given the worker builds from them (what the
    gateway's catalog and data/fixtures/robots.yaml serve) instead of its own data/ checkout, so data drift between
    machines cannot happen. Robot cameras use DEFAULT_ROBOT_CAMERA_MOUNTS.
    """

    type: Literal["reset"] = "reset"
    store_id: StoreId | None = None
    seed: int | None = None
    planogram: Planogram | None = None
    products: list[Product] | None = None
    robots: list[RobotSpec] | None = Field(None, description="Robots of that store (dock pose, cameras, battery).")
    cameras: list[StoreCameraSpec] | None = Field(None, description="Ceiling cameras of that store (RobotsFile).")


class StopCmd(_Cmd):
    """Cancel ``robot_id``'s motion and hold position (``robot_id`` None = every robot: emergency stop)."""

    type: Literal["stop"] = "stop"


class SetStreamsCmd(_Cmd):
    """Replace the set of cameras the worker streams unsolicited ``CameraFrame``s for (bandwidth control).

    Default after welcome: every robot's ``front`` camera at 2 fps. ``camera_ids`` empty = stream nothing.
    """

    type: Literal["set_streams"] = "set_streams"
    camera_ids: list[CameraId] = Field(default_factory=list)
    fps: float = Field(2.0, gt=0, le=15)


class SetSlotStockCmd(_Cmd):
    """Store-level, non-motion: make ``slot_id`` hold exactly ``qty`` units (catalog restock / write-off sync).

    Units are spawned / removed deterministically (``instance_id(slot_id, n)``, highest n = back rows removed first);
    ``lots`` (planogram order) assign lot_id / freshness to the units. Result ``instances`` = the slot's units now."""

    type: Literal["set_slot_stock"] = "set_slot_stock"
    store_id: StoreId
    slot_id: SlotId
    sku: Sku
    qty: int = Field(ge=0)
    lots: list[Lot] = Field(default_factory=list)


# ---- joint-level SO-101 control for learned policies (contracts 0.2.2; worker capability 'arm_joints') -------------
ARM_JOINTS_MAX_STEPS = 50
"""Most joint-target rows one ``ArmJointsCmd`` carries (one action chunk; SmolVLA = 50, Perceptron = 20 / 30)."""
ARM_JOINTS_HZ = 30.0
"""Default rate the worker applies the rows at (LeRobot SO-100/101 datasets: 30 fps)."""
ARM_JOINTS_CAPABILITY = "arm_joints"
"""``SimHello.capabilities`` entry of a worker that implements ``ArmJointsCmd`` (older workers answer 'internal')."""
PolicyCameraName = Literal["side", "wrist"]
POLICY_CAMERAS: tuple[PolicyCameraName, ...] = ("side", "wrist")
"""The two policy cameras (LeRobot ``observation.images.side`` / ``observation.images.wrist``, 640x480 RGB). They are
NOT hosted cameras (no camera id, never streamed): the worker renders them only for ``ArmJointsCmd`` results."""


class ArmJointsCmd(_Cmd):
    """Motion command (0.2.2): drive the rover's SO-101 JOINT BY JOINT, for learned policies (T1 / T2).

    The robot arm runs the policy loop (observation -> action chunk) and sends one command per chunk; the worker only
    executes joints and renders the policy cameras, so it stays policy-agnostic and no policy secrets live on it:

        ArmJointsCmd(targets=[], lift_height_m=h)         -> enter joint mode at h, first observation
        ArmJointsCmd(targets=chunk[:k])  (k <= 50 rows)    -> rows applied at ``hz``, observation after the last row
        ArmJointsCmd(targets=[], end=True, capture=[])     -> leave joint mode (arm back to the scripted stow pose)

    Joint mode starts from the arm's current scripted pose (state = those joint angles) and the lift then stays at
    ``lift_height_m`` (pretrained SO-101 policies drive the 6 arm joints only, never the lift or the base). Each row is
    6 USD joint DEGREES in ``SO101_JOINT_NAMES`` order (clamped to the joint limits). Grasping is a physics
    substitute, the honest "sticky grasp": when the gripper closes past its threshold with a unit within ~3 cm of the
    jaw center, the worker attaches that unit (``ArmJointState.events`` 'sticky_grasp:<id>', ``RobotState.holding``);
    opening releases it into the basket when the jaws are over it, else back onto its shelf spot / the floor. Any
    other motion command (navigate / scan / pick / place / unload / reset) leaves joint mode first; ``stop`` cancels
    the rows still pending (the arm holds its pose). Result: ``CommandResult.arm`` + ``policy_frames`` (RAW frames:
    the backend blurs before a frame leaves it) + ``state``; ``instance_id`` = the attached unit, if any."""

    type: Literal["arm_joints"] = "arm_joints"
    targets: list[list[float]] = Field(
        default_factory=list,
        max_length=ARM_JOINTS_MAX_STEPS,
        description="Joint targets, one row of 6 USD degrees per step (SO101_JOINT_NAMES order). [] = observe only.",
    )
    hz: float = Field(ARM_JOINTS_HZ, gt=0, le=240, description="Rows per second (kinematic: no dynamics).")
    lift_height_m: float | None = Field(
        None, description="Move the lift here first (SO-101 shoulder height, LIFT_MIN_M..LIFT_MAX_M); None = keep."
    )
    capture: list[PolicyCameraName] = Field(
        default_factory=lambda: list(POLICY_CAMERAS), description="Policy cameras rendered after the last row."
    )
    capture_each_step: bool = Field(False, description="Render ``capture`` after EVERY row (dataset recording).")
    capture_every: int = Field(
        0,
        ge=0,
        le=ARM_JOINTS_MAX_STEPS,
        description="Also render ``capture`` after every n-th row while the rows play (the live picture feed while "
        "the arm moves: 10 at 30 Hz = 3 frames/s); 0 = only after the last row. The last frame is always the "
        "observation after the last row.",
    )
    end: bool = Field(False, description="After the rows: leave joint mode (the scripted rig takes the arm back).")
    exec_id: str | None = Field(None, description="VLA exec id (logs only).")

    @field_validator("targets")
    @classmethod
    def _six_finite(cls, rows: list[list[float]]) -> list[list[float]]:
        for i, row in enumerate(rows):
            if len(row) != len(SO101_JOINT_NAMES):
                raise ValueError(f"targets[{i}] has {len(row)} values, expected {len(SO101_JOINT_NAMES)}")
            if not all(math.isfinite(v) for v in row):
                raise ValueError(f"targets[{i}] has a non-finite value")
        return rows


ARM_IK_CAPABILITY = "arm_ik"
"""``SimHello.capabilities`` entry of a worker that implements ``ArmIkCmd`` (0.2.2)."""
ARM_IK_MAX_TARGETS = 8
IK_TOLERANCE_M = 0.015
"""``IkSolution.ok``: the solved TCP is within this of the target."""


class IkTarget(Model):
    """One TCP target for ``ArmIkCmd``: the jaw center at ``point`` (STORE frame)."""

    point: Point3
    level: bool = Field(True, description="Keep the gripper level (side grasp, as the scripted rig and the demos).")
    gripper_deg: float | None = Field(None, description="Gripper joint of the solution (None = the seed's).")
    wrist_roll_deg: float | None = Field(None, description="Wrist roll of the solution (None = the seed's).")


class IkSolution(Model):
    q_deg: list[float] = Field(description="6 USD joint degrees (SO101_JOINT_NAMES order), clamped to the limits.")
    error_m: float = Field(ge=0, description="Solved TCP to target distance.")
    ok: bool = Field(description="error_m <= IK_TOLERANCE_M (else the closest reachable pose).")
    tcp: Point3 | None = Field(None, description="Solved TCP, store frame.")


class ArmIkCmd(_Cmd):
    """NON-motion (0.2.2, capability ``ARM_IK_CAPABILITY``): inverse kinematics on the worker's SO-101 model (the
    rover USD's joint frames), so policies that act in Cartesian space (e.g. a VLM pointing at pixels) turn targets
    into the joint rows of an ``ArmJointsCmd``. Solves only, never moves. Targets are solved in order, each seeded
    with the previous solution (``seed_deg`` / the current joints first, then a few fixed seeds); the lift is
    ``lift_height_m`` (None = the current lift). Result: ``CommandResult.ik`` (one ``IkSolution`` per target)."""

    type: Literal["arm_ik"] = "arm_ik"
    targets: list[IkTarget] = Field(min_length=1, max_length=ARM_IK_MAX_TARGETS)
    lift_height_m: float | None = None
    seed_deg: list[float] | None = Field(None, description="6 USD degrees; None = the arm's current joints.")

    @field_validator("seed_deg")
    @classmethod
    def _seed_six(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and (len(v) != len(SO101_JOINT_NAMES) or not all(math.isfinite(x) for x in v)):
            raise ValueError(f"seed_deg needs {len(SO101_JOINT_NAMES)} finite values")
        return v


SimCommand = Annotated[
    NavigateCmd
    | ScanCmd
    | PickCmd
    | PlaceInBasketCmd
    | UnloadBasketCmd
    | CaptureCmd
    | ListInstancesCmd
    | ResetCmd
    | StopCmd
    | SetStreamsCmd
    | SetSlotStockCmd
    | ArmJointsCmd
    | ArmIkCmd,
    Field(discriminator="type"),
]

MOTION_COMMANDS: frozenset[str] = frozenset(
    {"navigate", "scan", "pick", "place_in_basket", "unload_basket", "reset", "arm_joints"}
)
"""Command types that are mutually exclusive PER ROBOT (a second one while busy -> error_code='busy')."""


# --------------------------------------------------------------------------------------------------------------------
# Messages (sim -> gateway) + handshake
# --------------------------------------------------------------------------------------------------------------------
class PolicyFrame(Model):
    """One policy-camera frame of an ``ArmJointsCmd`` result (RAW, like every sim frame: blur before it leaves the
    backend). Not a hosted camera: no camera_id, no ground truth."""

    camera: PolicyCameraName
    step: int = Field(ge=0, description="Rows of this command applied before the render (0 = before the first).")
    jpeg_b64: str = Field(description="Base64 JPEG bytes (no data: prefix).")
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    blurred: bool = False
    camera_pose: CameraMount | None = Field(None, description="Pose + intrinsics used (frame='world' = store frame).")
    roll: float = Field(
        0.0,
        description="Camera roll about its viewing axis (rad, > 0 turns the image's right edge up); with camera_pose "
        "the exact pinhole for casting a pixel ray (the wrist camera rolls with the wrist).",
    )
    ts: Timestamp = Field(default_factory=utcnow)


class ArmJointState(Model):
    """Joint-level arm state after an ``ArmJointsCmd`` (0.2.2)."""

    joint_names: list[str] = Field(default_factory=lambda: list(SO101_JOINT_NAMES))
    q_deg: list[float] = Field(description="Current joint angles, USD degrees, SO101_JOINT_NAMES order.")
    joint_mode: bool = Field(description="False after end=True (the scripted rig owns the arm again).")
    lift_height_m: float
    tcp: Point3 | None = Field(None, description="Jaw center in the STORE frame.")
    gripper_closed: bool = False
    attached: InstanceId | None = Field(None, description="Unit held by the sticky grasp (= RobotState.holding).")
    steps_applied: int = Field(0, ge=0, description="Rows of this command applied (< len(targets) when stopped).")
    events: list[str] = Field(
        default_factory=list,
        description="This command's grasp events: 'sticky_grasp:<id>' (physics substitute: the closing jaws attached "
        "the unit), 'release_basket:<id>', 'put_back:<id>', 'drop:<id>', 'close_empty' (closed on air).",
    )


class CommandResult(WireModel):
    type: Literal["result"] = "result"
    cmd_id: CmdId
    robot_id: RobotId | None = Field(None, description="Echo of the command's robot_id.")
    ok: bool
    error_code: SimErrorCode | None = Field(None, description="Set when ok=False.")
    error: str | None = Field(None, description="Human-readable detail.")
    duration_ms: float = 0.0
    state: RobotState | None = None
    frame: CameraFrame | None = Field(None, description="scan/capture results (ground truth in frame.instances).")
    instances: list[ItemInstance] = Field(
        default_factory=list, description="list_instances / pick / unload_basket / set_slot_stock results."
    )
    instance_id: InstanceId | None = Field(None, description="pick/place: the instance handled.")
    arm: ArmJointState | None = Field(None, description="arm_joints results (0.2.2): joint state + grasp events.")
    policy_frames: list[PolicyFrame] = Field(
        default_factory=list, description="arm_joints results (0.2.2): the requested policy-camera frames, in order."
    )
    ik: list[IkSolution] = Field(default_factory=list, description="arm_ik results (0.2.2): one per target.")


class SimStoreInfo(WireModel):
    """A store scene hosted by a worker."""

    store_id: StoreId
    data_hash: str | None = Field(None, description="planogram_hash() of the layout the scene was built from.")
    camera_ids: list[CameraId] = Field(default_factory=list, description="Ceiling cameras ('store_ave.ceiling-1').")
    loaded: bool = True


class SimRobotInfo(WireModel):
    """A rover hosted by a worker."""

    robot_id: RobotId
    store_id: StoreId
    kind: str = "rover_so101"
    camera_ids: list[CameraId] = Field(default_factory=list, description="'<robot_id>.front|wrist|basket'.")


class SimHello(WireModel):
    type: Literal["hello"] = "hello"
    token: str = Field(description="PICKY_SIM_TOKEN (the auth).")
    worker_id: str = "isaac-0"
    stores: list[SimStoreInfo] = Field(default_factory=list)
    robots: list[SimRobotInfo] = Field(default_factory=list)
    cameras: list[CameraInfo] = Field(default_factory=list, description="Every camera the worker can render.")
    capabilities: list[str] = Field(default_factory=list, description="e.g. ['navigate','pick','camera:front']")
    sim_version: str | None = None
    contracts_version: str | None = None
    store_id: StoreId | None = Field(None, description="Legacy single-store field (folded into store_ids()).")
    data_hash: str | None = Field(None, description="Legacy: planogram_hash of ``store_id``; use stores[].data_hash.")

    def store_ids(self) -> list[StoreId]:
        """Hosted store ids (v0.1 ``store_id`` folded in)."""
        ids = [s.store_id for s in self.stores]
        if self.store_id and self.store_id not in ids:
            ids.append(self.store_id)
        return ids

    def robot_ids(self) -> list[RobotId]:
        return [r.robot_id for r in self.robots]


class SimHosted(WireModel):
    """Worker -> gateway after any reset that changes what it hosts: the FULL new lists (replace, not merge)."""

    type: Literal["hosted"] = "hosted"
    worker_id: str = "isaac-0"
    stores: list[SimStoreInfo] = Field(default_factory=list)
    robots: list[SimRobotInfo] = Field(default_factory=list)
    cameras: list[CameraInfo] = Field(default_factory=list)

    def store_ids(self) -> list[StoreId]:
        return [st.store_id for st in self.stores]

    def robot_ids(self) -> list[RobotId]:
        return [r.robot_id for r in self.robots]


class SimWelcome(WireModel):
    type: Literal["welcome"] = "welcome"
    ok: bool
    session_id: str | None = None
    error: str | None = None


class SimLog(WireModel):
    type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    robot_id: RobotId | None = None
    message: str
    ts: Timestamp = Field(default_factory=utcnow)


SimMessage = Annotated[
    SimHello | SimHosted | CommandResult | RobotState | CameraFrame | InstanceInfo | SimLog,
    Field(discriminator="type"),
]
"""Anything the sim worker sends to the gateway."""

GatewayToSim = Annotated[
    NavigateCmd
    | ScanCmd
    | PickCmd
    | PlaceInBasketCmd
    | UnloadBasketCmd
    | CaptureCmd
    | ListInstancesCmd
    | ResetCmd
    | StopCmd
    | SetStreamsCmd
    | SetSlotStockCmd
    | ArmJointsCmd
    | ArmIkCmd
    | SimWelcome,
    Field(discriminator="type"),
]
"""Anything the gateway sends to the sim worker."""

SIM_COMMAND_ADAPTER: TypeAdapter[Any] = TypeAdapter(SimCommand)
SIM_MESSAGE_ADAPTER: TypeAdapter[Any] = TypeAdapter(SimMessage)
GATEWAY_TO_SIM_ADAPTER: TypeAdapter[Any] = TypeAdapter(GatewayToSim)


LENIENT_PARSE_PASSES = 8


def _drop_at(data: Any, loc: tuple[Any, ...]) -> bool:
    """Delete the key at error ``loc`` (skipping union tags / model names that are not in the data). True if done."""
    node = data
    path = list(loc)
    for i, part in enumerate(path):
        last = i == len(path) - 1
        if isinstance(node, dict) and part in node:
            if last:
                del node[part]
                return True
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            if last:
                return False
            node = node[part]
        # else: a discriminator tag ('reset') or validator name in the loc: skip it
    return False


def lenient_validate(adapter: TypeAdapter[Any], raw: str | bytes | dict[str, Any]) -> Any:
    """Validate ``raw`` with ``adapter``, dropping unknown keys at ANY depth (``extra_forbidden`` errors of nested
    ``Model``s) and retrying up to LENIENT_PARSE_PASSES times; any other error raises the ValidationError."""
    data: Any = json.loads(raw) if isinstance(raw, str | bytes) else copy.deepcopy(raw)
    for _ in range(LENIENT_PARSE_PASSES):
        try:
            return adapter.validate_python(data)
        except ValidationError as exc:
            extras = [e for e in exc.errors() if e.get("type") == "extra_forbidden"]
            if not extras or not any([_drop_at(data, tuple(e["loc"])) for e in extras]):
                raise
    return adapter.validate_python(data)


def parse_sim_message(raw: str | bytes | dict[str, Any]) -> SimMessage:
    """Parse a sim->gateway message (JSON text or dict); unknown keys at any depth are dropped (lenient_validate)."""
    return lenient_validate(SIM_MESSAGE_ADAPTER, raw)


def parse_gateway_to_sim(raw: str | bytes | dict[str, Any]) -> GatewayToSim:
    """Parse a gateway->sim message (command or welcome); unknown keys at any depth are dropped."""
    return lenient_validate(GATEWAY_TO_SIM_ADAPTER, raw)


def command_error_result(raw: str | bytes | dict[str, Any], exc: Exception) -> "CommandResult | None":
    """Worker side: the reply for a command that failed to parse, if its raw JSON names a ``cmd_id`` (else None)."""
    try:
        data = json.loads(raw) if isinstance(raw, str | bytes) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cmd_id"), str):
        return None
    robot_id = data.get("robot_id") if isinstance(data.get("robot_id"), str) else None
    return CommandResult(
        cmd_id=data["cmd_id"], robot_id=robot_id, ok=False, error_code="internal", error=f"bad command: {exc}"[:500]
    )


_STOCK_FIELDS = {"qty", "lots", "price_cents", "shelf_tag_cents", "deal", "capacity", "low_threshold"}


def planogram_hash(planogram: Planogram) -> str:
    """sha256 hex of the planogram LAYOUT (bounds, poses, fixtures, aisles, slot geometry + sku).

    Stock, prices, deals, capacity and low_threshold are excluded, so picks / scans / restocks / threshold edits
    don't change it. Compared with ``SimStoreInfo.data_hash``.
    """
    data = planogram.model_dump(mode="json", exclude={"slots": {"__all__": _STOCK_FIELDS}})
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def versions_compatible(a: str | None, b: str | None) -> bool:
    """True when two CONTRACTS_VERSION strings share major.minor (patch differences are wire-safe)."""
    if not a or not b:
        return False
    return a.split(".")[:2] == b.split(".")[:2]


def default_timeout_s(cmd: Any) -> float:
    """Timeout ``SimLink.send`` uses when ``timeout_s`` is None."""
    return SIM_DEFAULT_TIMEOUTS_S.get(getattr(cmd, "type", ""), SIM_DEFAULT_TIMEOUT_S)


def message_robot_id(msg: Any) -> RobotId | None:
    """robot_id a wire message is about (commands, results, states, frames, logs); None for store-level messages."""
    return getattr(msg, "robot_id", None)


class SimWorkerStatus(Model):
    """One connected (or last-seen) Isaac worker."""

    worker_id: str
    session_id: str | None = None
    connected: bool = True
    connected_at: Timestamp | None = None
    last_message_at: Timestamp | None = None
    sim_version: str | None = None
    contracts_version: str | None = None
    stores: list[SimStoreInfo] = Field(default_factory=list)
    robots: list[SimRobotInfo] = Field(default_factory=list)
    cameras: list[CameraInfo] = Field(default_factory=list)
    streaming_camera_ids: list[CameraId] = Field(default_factory=list, description="Current SetStreamsCmd set.")


class SimStatus(Model):
    """GET /api/sim/status: is Isaac connected, what it hosts, and which robots run on the 2D fake instead."""

    connected: bool = False
    workers: list[SimWorkerStatus] = Field(default_factory=list)
    isaac_store_ids: list[StoreId] = Field(default_factory=list, description="Stores driven by Isaac now.")
    fake_store_ids: list[StoreId] = Field(default_factory=list, description="Stores on the 2D fake (fallback).")
    isaac_robot_ids: list[RobotId] = Field(default_factory=list, description="Robots driven by Isaac now.")
    fake_robot_ids: list[RobotId] = Field(default_factory=list, description="Robots on the 2D fake (fallback).")
    note: str = ""


SimMessageHandler = Callable[[SimMessage], Awaitable[None]]


@runtime_checkable
class SimLink(Protocol):
    """Gateway-provided link to the connected sim worker(s) (used by the robot arm's real RobotService).

    Multiplexed: commands are routed by ``robot_id`` (or ``store_id`` / ``camera_id``) to the worker hosting it.
    """

    def is_connected(self, robot_id: RobotId | None = None) -> bool:
        """None: any worker connected. robot_id: a connected worker lists that robot (latest SimHello / SimHosted)."""
        ...

    async def send(self, cmd: SimCommand, timeout_s: float | None = None) -> CommandResult:
        """Send a SimCommand and await its CommandResult.

        Raises TimeoutError, or ConnectionError when no worker hosts the target / disconnected mid-command.
        ``timeout_s=None`` means ``default_timeout_s(cmd)``. A failed command is NOT an exception: it returns
        ``ok=False`` + error_code.
        """
        ...

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        """Call ``handler(msg)`` for every unsolicited SimMessage of every worker (RobotState / CameraFrame /
        InstanceInfo / SimLog, each with its robot_id/store_id) AND for each accepted ``SimHello`` and every
        ``SimHosted`` (so the robot arm learns which stores/robots/cameras Isaac hosts and can pick the backend per
        store); returns an unsubscribe function. Fans out to every subscriber (robot, cameras, ...)."""
        ...

    def status(self) -> SimStatus:
        """Connected workers and what they host (from their latest SimHello / SimHosted). The gateway completes
        isaac_* / fake_* (from the robot arm's per-store choice) for GET /api/sim/status."""
        ...
