"""Wire protocol between the gateway and the Isaac Sim worker, plus the SimLink Protocol.

Transport
- The sim worker connects OUTBOUND to the gateway at ``ws /ws/sim``. First message is ``SimHello``; auth is
  ``SimHello.token`` == PICKY_SIM_TOKEN (a ``?token=`` query param is optional and only an early reject).
- Version check: the gateway compares ``SimHello.contracts_version`` major.minor with ``CONTRACTS_VERSION`` and answers
  ``SimWelcome(ok=False, error="contracts mismatch ...")`` (then closes) when they differ, or when ``data_hash`` differs
  from ``planogram_hash`` of the planogram it serves (unless it pushes the planogram in ``ResetCmd``). Otherwise
  ``SimWelcome(ok=True)``, after which the worker immediately sends a ``RobotState`` so the gateway can resync.
- Then the gateway sends ``SimCommand``s (each with a unique ``cmd_id``) and the worker sends exactly one
  ``CommandResult`` per command, plus unsolicited ``RobotState`` (on change, <= 10 Hz), ``CameraFrame`` (live view,
  <= 10 fps), ``InstanceInfo`` (after reset and whenever instances move) and ``SimLog``. Every message is a JSON object
  with a ``type`` field. All wire models ignore unknown fields (``WireModel``), so patch-level changes are safe.

Command rules
- One MOTION command (navigate / scan / pick / place_in_basket / reset) runs at a time; a second one while busy
  resolves immediately with ``ok=False, error_code="busy"``. ``capture``, ``list_instances`` and ``stop`` are always
  allowed. ``stop`` makes the in-flight motion command resolve ``ok=False, error_code="cancelled"``.
- Failures carry a machine-readable ``CommandResult.error_code`` (``SimErrorCode``). ``RobotService`` methods raise
  ``RobotError(code, message)`` with the same codes (the fake too), so the gateway branches on codes, not strings.
- ``navigate``: intermediate waypoint thetas are ignored (path heading); the final waypoint's position must be reached
  within ``tolerance_m`` and its theta within ``yaw_tolerance_rad``.
- Disconnect: the gateway fails in-flight ``SimLink.send`` calls with ``ConnectionError`` (robot maps it to
  ``RobotError("disconnected")``); the worker keeps the scene and aborts in-flight actions; results for unknown
  ``cmd_id``s are dropped. ``SimLink.send(timeout_s=None)`` uses ``SIM_DEFAULT_TIMEOUTS_S`` (else 15 s).

Frames: the sim world IS the store frame (USD Z-up, metersPerUnit=1, USD x/y = store x/y, origin at the store's
bottom-left corner; see contracts/planogram.py). Frames from the sim are RAW (``blurred=False``); perception blurs
faces before anything is stored, streamed or sent to an LLM. Scan/capture frames carry the sim's ground truth
(``CameraFrame.instances`` with per-frame pixel ``bbox``) for the ground-truth perception bypass.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field, TypeAdapter

from contracts.catalog import Product
from contracts.common import (
    BBox,
    CameraName,
    CmdId,
    FrameId,
    InstanceId,
    LotId,
    Point3,
    Pose2D,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    WireModel,
    utcnow,
)
from contracts.planogram import Planogram

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
    "internal",
]
"""unknown_slot / no_instance (slot empty -> re-source online) · grasp_failed (retry / attach fallback) ·
path_blocked / timeout (re-plan or skip) · cancelled (StopCmd) · busy (motion already running) · not_ready (no scene) ·
disconnected (degrade to the robot fake) · internal."""

SIM_DEFAULT_TIMEOUTS_S: dict[str, float] = {"reset": 120.0, "navigate": 90.0}
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
    IDLE = "idle"
    NAVIGATING = "navigating"
    SCANNING = "scanning"
    PICKING = "picking"
    PLACING = "placing"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class ItemInstance(WireModel):
    """One physical unit in the sim (ground truth)."""

    instance_id: InstanceId
    sku: Sku
    slot_id: SlotId | None = None
    position: Point3 = Field(description="Item CENTER in the store/sim world frame (meters, z up).")
    freshness: Score01 | None = None
    lot_id: LotId | None = Field(None, description="Planogram Lot this unit belongs to.")
    in_basket: bool = False
    bbox: BBox | None = Field(None, description="Pixel box in the frame this instance rides with (None otherwise).")


# ---- shared ground truth: the robot fake (2D) and the Isaac scene MUST agree on these, because the gateway falls back
# from the sim robot to the fake mid-trip; ids picked from sim frames (PickChoice) must still exist afterwards.
BASKET_OFFSET_LOCAL: tuple[float, float, float] = (-0.35, 0.0, 0.55)
"""Center of the basket floor in the robot base frame (x forward, y left, z up), meters: the fixed drop pose."""
BASKET_SIZE_M: tuple[float, float, float] = (0.45, 0.35, 0.2)
"""Basket inner extent (x, y, z), meters."""
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
    type: Literal["robot_state"] = "robot_state"
    store_id: StoreId | None = None
    pose: Pose2D
    status: RobotStatus = RobotStatus.IDLE
    holding: InstanceId | None = None
    basket: list[InstanceId] = Field(default_factory=list)
    basket_skus: list[Sku] = Field(default_factory=list, description="SKU per basket instance (same order).")
    current_cmd_id: CmdId | None = None
    error: str | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class CameraFrame(WireModel):
    type: Literal["camera_frame"] = "camera_frame"
    frame_id: FrameId
    camera: CameraName = "head"
    jpeg_b64: str = Field(description="Base64 JPEG bytes (no data: prefix).")
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    blurred: bool = Field(False, description="True only after perception.blur_faces.")
    slot_id: SlotId | None = None
    pose: Pose2D | None = Field(None, description="Robot pose when captured.")
    instances: list[ItemInstance] = Field(
        default_factory=list, description="Sim ground truth visible in this frame, with bbox (sim / robot fake only)."
    )
    ts: Timestamp = Field(default_factory=utcnow)


class InstanceInfo(WireModel):
    """Unsolicited ground-truth update (after reset / when instances move), optionally with per-frame pixel boxes."""

    type: Literal["instances"] = "instances"
    slot_id: SlotId | None = None
    frame_id: FrameId | None = None
    instances: list[ItemInstance] = Field(default_factory=list)
    bboxes: dict[InstanceId, BBox] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------------------------
# Commands (gateway -> sim)
# --------------------------------------------------------------------------------------------------------------------
class _Cmd(WireModel):
    cmd_id: CmdId


class NavigateCmd(_Cmd):
    type: Literal["navigate"] = "navigate"
    waypoints: list[Pose2D] = Field(min_length=1, description="Intermediate thetas ignored; final theta reached.")
    speed_mps: float | None = None
    tolerance_m: float = 0.05
    yaw_tolerance_rad: float = 0.05


class ScanCmd(_Cmd):
    """Point the head camera at a slot and capture; the result's ``frame.instances`` carries ground truth + bboxes."""

    type: Literal["scan"] = "scan"
    slot_id: SlotId
    camera: CameraName = "head"


class PickCmd(_Cmd):
    type: Literal["pick"] = "pick"
    slot_id: SlotId
    sku: Sku
    instance_id: InstanceId | None = Field(None, description="None = any instance of the SKU in the slot.")


class PlaceInBasketCmd(_Cmd):
    type: Literal["place_in_basket"] = "place_in_basket"
    instance_id: InstanceId | None = Field(None, description="None = whatever the gripper holds.")


class CaptureCmd(_Cmd):
    type: Literal["capture"] = "capture"
    camera: CameraName = "head"


class ListInstancesCmd(_Cmd):
    type: Literal["list_instances"] = "list_instances"
    slot_id: SlotId | None = Field(None, description="None = all instances (incl. basket).")


class ResetCmd(_Cmd):
    """(Re)build the scene. When ``planogram``/``products`` are given the worker builds from them (what the gateway's
    catalog serves) instead of its own data/ checkout, so data drift between machines cannot happen."""

    type: Literal["reset"] = "reset"
    store_id: StoreId | None = None
    seed: int | None = None
    planogram: Planogram | None = None
    products: list[Product] | None = None


class StopCmd(_Cmd):
    type: Literal["stop"] = "stop"


SimCommand = Annotated[
    NavigateCmd | ScanCmd | PickCmd | PlaceInBasketCmd | CaptureCmd | ListInstancesCmd | ResetCmd | StopCmd,
    Field(discriminator="type"),
]

MOTION_COMMANDS: frozenset[str] = frozenset({"navigate", "scan", "pick", "place_in_basket", "reset"})
"""Command types that are mutually exclusive (a second one while busy -> error_code='busy')."""


# --------------------------------------------------------------------------------------------------------------------
# Messages (sim -> gateway) + handshake
# --------------------------------------------------------------------------------------------------------------------
class CommandResult(WireModel):
    type: Literal["result"] = "result"
    cmd_id: CmdId
    ok: bool
    error_code: SimErrorCode | None = Field(None, description="Set when ok=False.")
    error: str | None = Field(None, description="Human-readable detail.")
    duration_ms: float = 0.0
    state: RobotState | None = None
    frame: CameraFrame | None = Field(None, description="scan/capture results (ground truth in frame.instances).")
    instances: list[ItemInstance] = Field(default_factory=list, description="list_instances/pick results.")
    instance_id: InstanceId | None = Field(None, description="pick/place: the instance handled.")


class SimHello(WireModel):
    type: Literal["hello"] = "hello"
    token: str = Field(description="PICKY_SIM_TOKEN (the auth).")
    store_id: StoreId
    worker_id: str = "isaac-0"
    capabilities: list[str] = Field(default_factory=list, description="e.g. ['navigate','pick','camera:head']")
    sim_version: str | None = None
    contracts_version: str | None = None
    data_hash: str | None = Field(None, description="planogram_hash() of the layout the scene was built from.")


class SimWelcome(WireModel):
    type: Literal["welcome"] = "welcome"
    ok: bool
    session_id: str | None = None
    error: str | None = None


class SimLog(WireModel):
    type: Literal["log"] = "log"
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str
    ts: Timestamp = Field(default_factory=utcnow)


SimMessage = Annotated[
    SimHello | CommandResult | RobotState | CameraFrame | InstanceInfo | SimLog,
    Field(discriminator="type"),
]
"""Anything the sim worker sends to the gateway."""

GatewayToSim = Annotated[
    NavigateCmd
    | ScanCmd
    | PickCmd
    | PlaceInBasketCmd
    | CaptureCmd
    | ListInstancesCmd
    | ResetCmd
    | StopCmd
    | SimWelcome,
    Field(discriminator="type"),
]
"""Anything the gateway sends to the sim worker."""

SIM_COMMAND_ADAPTER: TypeAdapter[Any] = TypeAdapter(SimCommand)
SIM_MESSAGE_ADAPTER: TypeAdapter[Any] = TypeAdapter(SimMessage)
GATEWAY_TO_SIM_ADAPTER: TypeAdapter[Any] = TypeAdapter(GatewayToSim)


def parse_sim_message(raw: str | bytes | dict[str, Any]) -> SimMessage:
    """Parse a sim->gateway message (JSON text or dict) into the right model."""
    if isinstance(raw, dict):
        return SIM_MESSAGE_ADAPTER.validate_python(raw)
    return SIM_MESSAGE_ADAPTER.validate_json(raw)


def parse_gateway_to_sim(raw: str | bytes | dict[str, Any]) -> GatewayToSim:
    """Parse a gateway->sim message (command or welcome)."""
    if isinstance(raw, dict):
        return GATEWAY_TO_SIM_ADAPTER.validate_python(raw)
    return GATEWAY_TO_SIM_ADAPTER.validate_json(raw)


_STOCK_FIELDS = {"qty", "lots", "price_cents", "shelf_tag_cents", "deal", "capacity"}


def planogram_hash(planogram: Planogram) -> str:
    """sha256 hex of the planogram LAYOUT (bounds, poses, fixtures, aisles, slot geometry + sku).

    Stock, prices and deals are excluded, so picks/scans don't change it. Compared with ``SimHello.data_hash``.
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


SimMessageHandler = Callable[[SimMessage], Awaitable[None]]


@runtime_checkable
class SimLink(Protocol):
    """Gateway-provided link to the connected sim worker (used by the robot arm's real RobotService)."""

    def is_connected(self) -> bool: ...

    async def send(self, cmd: SimCommand, timeout_s: float | None = None) -> CommandResult:
        """Send a SimCommand and await its CommandResult.

        Raises TimeoutError, or ConnectionError when not connected / disconnected mid-command. ``timeout_s=None``
        means ``default_timeout_s(cmd)``. A failed command is NOT an exception: it returns ``ok=False`` + error_code.
        """
        ...

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        """Call ``handler(msg)`` for every unsolicited SimMessage; returns an unsubscribe function."""
        ...
