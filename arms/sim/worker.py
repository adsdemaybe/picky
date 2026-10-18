"""Isaac-free core of the sim worker: MULTI-STORE, MULTI-ROVER command handling, per-robot action state machines,
ground truth and cameras.

``arms/sim/app.py`` owns the process: a websocket thread fills ``inbox`` and drains the outbox; the main loop calls
``WorkerCore.submit(cmd)`` for every queued command, ``WorkerCore.tick(dt)`` once per loop iteration and captures the
cameras ``WorkerCore.stream_due(now)`` names. Rendering / USD go through a ``SimBackend`` (Isaac in app.py, the Pillow
``SketchBackend`` in sketch.py, ``NullBackend`` here), so the protocol logic is unit-tested on a laptop.

Wire semantics implemented here (contracts/sim.py is the spec):
- Stores: each ``StoreScene`` is built from a planogram (``ResetCmd`` data, else utils.data) at its own
  ``/World/Stores/<store>`` offset; every pose / position on the wire is store-local.
- ``robot_id`` per command: navigate / scan / pick / place_in_basket / unload_basket REQUIRE a hosted robot
  (``unknown_robot`` otherwise); stop None = every robot; reset None = rebuild ``store_id``'s scene and respawn its
  robots; capture by ``camera_id`` or robot + camera; list_instances / set_slot_stock are store-level.
- One MOTION command per robot (``busy``); robots move concurrently; stop cancels with ``cancelled``.
- Motion is kinematic (pure pursuit, navigation.py) and grasping is the scripted "magic grasp": lift to the item
  height, extend the SO-101, attach, retract (reach = ``contracts.common.slot_reachable`` on the SLOT), then place
  into the basket at ``BASKET_OFFSET_LOCAL``. Rovers in one store avoid each other (``path_blocked`` after 10 s).
- Battery is advisory (the robot arm owns it): drains per meter / per idle minute, charges idle on the dock; below
  ``BATTERY_MOTION_MIN_PCT`` navigate (except to the dock), scan and pick are refused with ``battery_low``.
- Cameras: '<robot>.front' (pan-tilt head; scans aim it at the slot), '.wrist' (rides the SO-101 TCP), '.basket'
  (looks down into the basket), '<store>.ceiling-<n>'. Scan frames list EVERY non-basket unit of the slot (bbox None
  when off-image); captures list the visible units; stream frames carry no instances. Frames are RAW
  (``blurred=False``): perception blurs faces in the gateway.
- Optional visuals (app.py wires them on the GPU host): ``assets`` (``arms.sim.assets.SkuAssets``, per-item grocery
  models: unit dims + render mode per SKU, also on restock) and ``rover_usd`` (LeKiwi + SO-101 visual puppet). Neither
  changes ids, counts or any wire message.
"""

import base64
import functools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from arms.sim.assets import SkuAssets
from arms.sim.geometry import (
    ARM_FULL_EXT_M,
    ARM_STOW_EXT_M,
    HELD_CLEARANCE_M,
    LIFT_REST_M,
    CameraPose,
    clamp_lift,
    held_position,
    mount_camera,
    pan_reach_for,
    project_box,
    robot_camera_pose,
    to_store,
)
from arms.sim.navigation import PurePursuit
from arms.sim.scene_spec import (
    BASE_SIZE_M,
    BASKET_OFFSET_M,
    BASKET_SIZE_M,
    PrimSpec,
    RoverSpec,
    SceneSpec,
    Vec3,
    basket_layout,
    build_rover_spec,
    build_scene_spec,
    camera_infos,
    default_robots,
    default_store_cameras,
    instance_id_for,
    instance_index,
    layout_for,
    slot_lateral_room,
    store_offsets,
    unit_instance,
    unit_lots,
    unit_prim,
)
from contracts import CONTRACTS_VERSION
from contracts.cameras import ROBOT_CAMERA_KINDS, CameraInfo, parse_camera_id, robot_camera_id, store_camera_id
from contracts.catalog import Product
from contracts.common import (
    ARM_MOUNT_X_M,
    BATTERY_CHARGE_PCT_PER_MIN,
    BATTERY_DRAIN_PCT_PER_M,
    BATTERY_IDLE_DRAIN_PCT_PER_MIN,
    BATTERY_MOTION_MIN_PCT,
    DOCK_TOLERANCE_M,
    ROBOT_RADIUS_M,
    Point3,
    Pose2D,
    normalize_camera,
    slot_reachable,
)
from contracts.planogram import Planogram, RobotSpec, Slot, StoreCameraSpec
from contracts.sim import (
    CameraFrame,
    CommandResult,
    InstanceInfo,
    ItemInstance,
    RobotState,
    RobotStatus,
    SimErrorCode,
    SimHello,
    SimHosted,
    SimLog,
    SimRobotInfo,
    SimStoreInfo,
    planogram_hash,
    unit_freshness,
)

# ---- timing (seconds) / speeds. Arm motions are eased joint-space interpolations that read well on video
# (arms/sim/tools/demo_video): reach -> close -> lift off -> retract, then swing to the basket -> release -> back.
SCAN_S = 0.6
GRASP_REACH_S = 1.0
GRASP_CLOSE_S = 0.4
GRASP_RAISE_S = 0.3
GRASP_RETRACT_S = 0.9
GRASP_LIFT_OFF_M = 0.025
"""The unit is lifted this far off the shelf after the jaws close (and lowered back while the arm retracts)."""
PLACE_SWING_S = 1.1
PLACE_RELEASE_S = 0.25
PLACE_DROP_S = 0.35
PLACE_BACK_S = 0.9
PLACE_S = PLACE_SWING_S + PLACE_RELEASE_S + PLACE_DROP_S + PLACE_BACK_S
UNLOAD_S = 1.2
"""Unload without a checkout counter in front of the robot: the units just leave the scene after this long."""
UNLOAD_SLOW_UNITS = 3
"""Checkout unload: the first units get the full-speed arm motion, the rest a quicker one (it is a batch)."""
UNLOAD_MOVE_S = (0.9, 0.55)
UNLOAD_GRAB_S = (0.25, 0.15)
UNLOAD_RELEASE_S = (0.2, 0.15)
COUNTER_APPROACH_MAX_M = 1.0
"""Unload drives up to the checkout counter in front of it when the gap is at most this (and backs off after)."""
COUNTER_GAP_M = 0.05
COUNTER_SPEED_MPS = 0.25
CONVEYOR_SPEED_MPS = 0.35
LEFTOVER_S = 120.0
"""Unloaded units stay on the counter (visual only, not ground truth) this long (sim seconds)."""
BASKET_RELEASE_MIN_Y_M = 0.085
"""The arm swings around the lift mast: its TCP stays this far off the robot's centre line over the basket."""
LIFT_SPEED_MPS = 0.3
BLOCKED_TIMEOUT_S = 10.0
STATE_MIN_PERIOD_S = 0.1  # RobotState <= 10 Hz per robot
DEFAULT_STREAM_FPS = 2.0
MAX_STREAM_FPS = 15.0
BASKET_DROP_LIFT_M = BASKET_OFFSET_M[2] + 0.25
CAPABILITIES = [
    "multi_store",
    "multi_robot",
    "navigate",
    "scan",
    "pick",
    "place_in_basket",
    "unload_basket",
    "capture",
    "list_instances",
    "reset",
    "stop",
    "set_streams",
    "set_slot_stock",
    "camera:front",
    "camera:wrist",
    "camera:basket",
    "camera:ceiling",
]

# 1x1 grey baseline JPEG: last-resort placeholder when Pillow is missing.
PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////"
    "////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


# --------------------------------------------------------------------------------------------------------------------
# Backend seam
# --------------------------------------------------------------------------------------------------------------------
class SimBackend(Protocol):
    """What the core needs from a renderer (store-local coordinates everywhere). Isaac in app.py; Null / Sketch."""

    camera_source: str

    def build_store(self, spec: SceneSpec, offset: Vec3) -> None:
        """(Re)build one store under its ``/World/Stores/<store>`` Xform at ``offset`` (removes the old one)."""
        ...

    def set_store_offset(self, store_id: str, offset: Vec3) -> None: ...

    def spawn_robot(self, rover: RoverSpec) -> None: ...

    def remove_robot(self, robot_id: str) -> None: ...

    def set_robot_pose(self, robot_id: str, pose: Pose2D, lift_height_m: float, arm_ext_m: float) -> None: ...

    # optional: ``set_robot_gripper(robot_id, closed)`` (visual jaws; called before each set_robot_pose)

    def add_instance(self, prim: PrimSpec) -> None: ...

    def remove_instance(self, store_id: str, instance_id: str) -> None: ...

    def set_instance_pose(
        self, store_id: str, instance_id: str, position: Point3, yaw: float | None = None
    ) -> None: ...

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        """JPEG bytes + (w, h) of the camera at ``cam`` (store frame), or None when no frame is ready yet."""
        ...


@functools.lru_cache(maxsize=8)
def grey_jpeg(w: int, h: int) -> tuple[bytes, int, int]:
    """Plain grey JPEG of the given size (Pillow), else the 1x1 placeholder."""
    try:
        import io

        from PIL import Image

        out = io.BytesIO()
        Image.new("RGB", (w, h), (128, 128, 128)).save(out, format="JPEG", quality=60)
        return out.getvalue(), w, h
    except ImportError:
        return PLACEHOLDER_JPEG, 1, 1


class NullBackend:
    """No renderer: records what it is told (tests) and returns a grey frame. ``app.py --backend null``."""

    camera_source = "isaac"

    def __init__(self) -> None:
        self.stores: dict[str, SceneSpec] = {}
        self.offsets: dict[str, Vec3] = {}
        self.rovers: dict[str, RoverSpec] = {}
        self.robot_poses: dict[str, tuple[Pose2D, float, float]] = {}
        self.instance_prims: dict[tuple[str, str], PrimSpec] = {}
        self.positions: dict[tuple[str, str], Point3] = {}
        self.yaws: dict[tuple[str, str], float] = {}
        self.renders: list[tuple[str, str]] = []
        self.grippers: dict[str, bool] = {}

    def build_store(self, spec: SceneSpec, offset: Vec3) -> None:
        sid = spec.store_id
        self.stores[sid] = spec
        self.offsets[sid] = offset
        for key in [k for k in self.instance_prims if k[0] == sid]:
            self.instance_prims.pop(key, None)
            self.positions.pop(key, None)
            self.yaws.pop(key, None)
        for p in spec.product_prims():
            self.add_instance(p)

    def set_store_offset(self, store_id: str, offset: Vec3) -> None:
        self.offsets[store_id] = offset

    def spawn_robot(self, rover: RoverSpec) -> None:
        self.rovers[rover.robot_id] = rover
        self.robot_poses[rover.robot_id] = (rover.dock, LIFT_REST_M, ARM_STOW_EXT_M)

    def remove_robot(self, robot_id: str) -> None:
        self.rovers.pop(robot_id, None)
        self.robot_poses.pop(robot_id, None)

    def set_robot_pose(self, robot_id: str, pose: Pose2D, lift_height_m: float, arm_ext_m: float) -> None:
        self.robot_poses[robot_id] = (pose, lift_height_m, arm_ext_m)

    def set_robot_gripper(self, robot_id: str, closed: bool) -> None:
        self.grippers[robot_id] = closed

    def add_instance(self, prim: PrimSpec) -> None:
        key = (prim.store_id or "", prim.instance_id or "")
        self.instance_prims[key] = prim
        self.positions[key] = Point3(x=prim.center[0], y=prim.center[1], z=prim.center[2])
        self.yaws[key] = prim.yaw

    def remove_instance(self, store_id: str, instance_id: str) -> None:
        self.instance_prims.pop((store_id, instance_id), None)
        self.positions.pop((store_id, instance_id), None)
        self.yaws.pop((store_id, instance_id), None)

    def set_instance_pose(self, store_id: str, instance_id: str, position: Point3, yaw: float | None = None) -> None:
        self.positions[(store_id, instance_id)] = position
        if yaw is not None:
            self.yaws[(store_id, instance_id)] = yaw

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        self.renders.append((camera_id, store_id))
        return grey_jpeg(cam.w, cam.h)


# --------------------------------------------------------------------------------------------------------------------
# World state
# --------------------------------------------------------------------------------------------------------------------
class _Fail(Exception):
    def __init__(self, code: SimErrorCode, message: str = "") -> None:
        self.code: SimErrorCode = code
        self.message = message or code
        super().__init__(self.message)


@dataclass
class StoreScene:
    spec: SceneSpec
    planogram: Planogram
    products: dict[str, Product]
    cameras: list[StoreCameraSpec]
    instances: dict[str, ItemInstance] = field(default_factory=dict)
    """Every unit of the store (shelves + baskets + grippers), by instance id."""

    @property
    def store_id(self) -> str:
        return self.spec.store_id

    def slot(self, slot_id: str) -> Slot | None:
        return self.planogram.slot(slot_id)

    def item_dims(self, iid: str) -> Vec3:
        return self.spec.item_dims.get(iid, (0.08, 0.08, 0.15))


class Rover:
    """Live state of one hosted robot."""

    def __init__(self, robot: RobotSpec, model_usd: str | None = None) -> None:
        self.robot = robot
        self.spec: RoverSpec = build_rover_spec(robot, model_usd=model_usd)
        self.pose: Pose2D = robot.dock
        self.lift: float = LIFT_REST_M
        self.arm_ext: float = ARM_STOW_EXT_M
        self.pan: float = 0.0
        """SO-101 pan about its vertical axis (0 = straight ahead, > 0 = toward the robot's left)."""
        self.grip: float = 0.0
        """Jaw opening, 0 = closed .. 1 = open (visual)."""
        self.holding: str | None = None
        self.basket: list[str] = []
        self.carry: str | None = None
        """Unload: the basket unit the arm is moving to the counter (visual; still listed until the unload ends)."""
        self.free: dict[str, tuple[Point3, float]] = {}
        """Units animated on their own (falling into the basket, set down on the counter): store pose + yaw."""
        self.battery: float = robot.battery_pct
        self.active: _Action | None = None
        self.active_started = 0.0
        self.error: str | None = None
        self.blocked_s = 0.0
        self.last_key: tuple[Any, ...] | None = None
        self.last_t = -1e9

    @property
    def robot_id(self) -> str:
        return self.robot.robot_id

    @property
    def store_id(self) -> str:
        return self.robot.store_id

    @property
    def on_dock(self) -> bool:
        return self.pose.distance_to(self.robot.dock) <= DOCK_TOLERANCE_M

    @property
    def status(self) -> RobotStatus:
        if self.error:
            return RobotStatus.ERROR
        if self.active is not None:
            return self.active.status
        return RobotStatus.CHARGING if self.on_dock else RobotStatus.IDLE


# --------------------------------------------------------------------------------------------------------------------
# Actions (non-blocking state machines: step(dt) -> CommandResult when done, else None; raise _Fail on failure)
# --------------------------------------------------------------------------------------------------------------------
class _Action:
    status = RobotStatus.IDLE

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        self.core, self.rover, self.cmd, self.elapsed = core, rover, cmd, 0.0

    def step(self, dt: float) -> CommandResult | None:  # pragma: no cover - interface
        raise NotImplementedError

    def _approach_lift(self, target: float, dt: float) -> bool:
        """Move the lift toward ``target`` at LIFT_SPEED_MPS; True once there."""
        r = self.rover
        delta = target - r.lift
        stepz = LIFT_SPEED_MPS * dt
        r.lift = target if abs(delta) <= stepz else r.lift + math.copysign(stepz, delta)
        return abs(target - r.lift) < 1e-6

    def _relax_arm(self, dt: float, rate: float = 2.5) -> None:
        """Ease a panned / open arm back to straight ahead + closed (navigate, scan)."""
        r = self.rover
        k = min(1.0, rate * dt)
        r.pan = 0.0 if abs(r.pan) < 1e-3 else r.pan * (1 - k)
        r.grip = 0.0 if r.grip < 1e-3 else r.grip * (1 - k)


def smooth(t: float) -> float:
    """Smoothstep on [0, 1] (zero velocity at both ends)."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def smoother(t: float) -> float:
    """Smootherstep on [0, 1] (zero velocity + acceleration at both ends)."""
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (6 * t - 15) + 10)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lift_move_s(a: float, b: float) -> float:
    """Duration of an eased lift move (peak speed ~1.5 x LIFT_SPEED_MPS)."""
    return max(0.35, abs(b - a) / LIFT_SPEED_MPS)


class _Phases:
    """A fixed sequence of (name, seconds); ``at(t)`` -> (name, local progress 0..1), or None after the end."""

    def __init__(self, phases: list[tuple[str, float]]) -> None:
        self.phases = [(n, max(0.0, s)) for n, s in phases]
        self.total = sum(s for _, s in self.phases)

    def at(self, t: float) -> tuple[str, float] | None:
        acc = 0.0
        for name, s in self.phases:
            if t < acc + s:
                return name, (t - acc) / s if s > 0 else 1.0
            acc += s
        return None


class _Navigate(_Action):
    status = RobotStatus.NAVIGATING

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        super().__init__(core, rover, cmd)
        scene = core.scene_of(rover)
        final = cmd.waypoints[-1]
        b = scene.spec.bounds
        if not b.contains(final.x, final.y):
            raise _Fail("path_blocked", f"final waypoint ({final.x:.2f}, {final.y:.2f}) is outside {scene.store_id}")
        to_dock = math.hypot(final.x - rover.robot.dock.x, final.y - rover.robot.dock.y) <= DOCK_TOLERANCE_M
        if rover.battery < BATTERY_MOTION_MIN_PCT and not to_dock:
            raise _Fail("battery_low", f"battery {rover.battery:.1f}% < {BATTERY_MOTION_MIN_PCT}%: send it to its dock")
        speed = min(cmd.speed_mps or rover.robot.max_speed_mps, rover.robot.max_speed_mps)
        self.pp = PurePursuit(
            rover.pose,
            list(cmd.waypoints),
            speed_mps=speed,
            tolerance_m=cmd.tolerance_m,
            yaw_tolerance_rad=cmd.yaw_tolerance_rad,
        )
        self.budget_s = 3.0 * self.pp.length_m / max(speed, 0.05) + 15.0
        rover.blocked_s = 0.0

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed > self.budget_s:
            raise _Fail("timeout", f"navigate exceeded {self.budget_s:.0f}s")
        r = self.rover
        self._approach_lift(LIFT_REST_M, dt)
        r.arm_ext = ARM_STOW_EXT_M
        self._relax_arm(dt)
        new = self.pp.step(r.pose, dt)
        blocker = self.core.blocking_rover(r, new)
        if blocker is not None:
            r.blocked_s += dt
            if r.blocked_s > BLOCKED_TIMEOUT_S:
                raise _Fail("path_blocked", f"{blocker.robot_id} blocks the path for > {BLOCKED_TIMEOUT_S:.0f}s")
            self.core.set_pose(r, r.pose)
            return None
        r.blocked_s = 0.0
        moved = r.pose.distance_to(new)
        r.battery = max(0.0, r.battery - moved * BATTERY_DRAIN_PCT_PER_M)
        self.core.set_pose(r, new)
        if self.pp.done:
            return self.core.result(self.cmd, ok=True, rover=r)
        return None


class _Scan(_Action):
    status = RobotStatus.SCANNING

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        super().__init__(core, rover, cmd)
        self.slot = core.require_slot(rover, cmd.slot_id)
        if rover.battery < BATTERY_MOTION_MIN_PCT:
            raise _Fail("battery_low", f"battery {rover.battery:.1f}%: send it to its dock")

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed < SCAN_S:
            return None
        cam = normalize_camera(self.cmd.camera)
        kind = cam if cam in ROBOT_CAMERA_KINDS else "front"
        frame = self.core.capture(robot_camera_id(self.rover.robot_id, kind), slot=self.slot, scan=True)
        if frame is None:
            raise _Fail("not_ready", "renderer has no frame yet")
        return self.core.result(self.cmd, ok=True, rover=self.rover, frame=frame)


class _Pick(_Action):
    """Magic grasp: lift to the item height, extend the SO-101, attach, retract."""

    status = RobotStatus.PICKING

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        super().__init__(core, rover, cmd)
        slot = core.require_slot(rover, cmd.slot_id)
        if rover.battery < BATTERY_MOTION_MIN_PCT:
            raise _Fail("battery_low", f"battery {rover.battery:.1f}%: send it to its dock")
        if rover.holding is not None:
            raise _Fail("grasp_failed", f"gripper already holds {rover.holding}")
        if not slot_reachable(rover.pose, slot.approach_pose, slot.item_position):
            raise _Fail(
                "grasp_failed",
                f"slot {cmd.slot_id} out of reach from ({rover.pose.x:.2f}, {rover.pose.y:.2f}); "
                "navigate to its approach pose first",
            )
        scene = core.scene_of(rover)
        held = core.held_ids(scene.store_id)
        candidates = [
            i
            for i in scene.instances.values()
            if i.slot_id == cmd.slot_id and i.sku == cmd.sku and not i.in_basket and i.instance_id not in held
        ]
        if cmd.instance_id is not None:
            candidates = [i for i in candidates if i.instance_id == cmd.instance_id]
        if not candidates:
            raise _Fail("no_instance", f"no {cmd.instance_id or cmd.sku} in slot {cmd.slot_id}")
        ap = slot.approach_pose
        self.target = min(
            candidates,
            key=lambda i: ((i.position.x - ap.x) ** 2 + (i.position.y - ap.y) ** 2, i.position.z, i.instance_id),
        )
        self.lift_target = clamp_lift(self.target.position.z)
        self.t_lift = abs(self.lift_target - rover.lift) / LIFT_SPEED_MPS
        # reach only as far as the unit: the TCP stops HELD_CLEARANCE_M short of its near face, so the held position
        # at the attach equals where the unit stood (no jump) and the wrist camera never drives into it (at a fixed
        # ARM_FULL_EXT_M it ended up inside the front unit)
        pose, tp = rover.pose, self.target.position
        along = (tp.x - pose.x) * math.cos(pose.theta) + (tp.y - pose.y) * math.sin(pose.theta)
        depth = scene.item_dims(self.target.instance_id)[1]
        self.reach_ext = max(ARM_STOW_EXT_M, min(ARM_FULL_EXT_M, along - depth / 2 - HELD_CLEARANCE_M - ARM_MOUNT_X_M))
        self.attached = False
        self.lift0, self.ext0, self.pan0, self.grip0 = rover.lift, rover.arm_ext, rover.pan, rover.grip
        self.phases = _Phases(
            [
                ("lift", lift_move_s(rover.lift, self.lift_target)),
                ("reach", GRASP_REACH_S),
                ("close", GRASP_CLOSE_S),
                ("raise", GRASP_RAISE_S),
                ("retract", GRASP_RETRACT_S),
            ]
        )

    def _attach(self) -> None:
        if self.attached:
            return
        scene = self.core.scene_of(self.rover)
        if self.target.instance_id not in scene.instances:
            raise _Fail("no_instance", f"{self.target.instance_id} vanished during the grasp")
        self.rover.holding = self.target.instance_id
        self.attached = True

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        r = self.rover
        at = self.phases.at(self.elapsed)
        if at is not None:
            name, u = at
            if name == "lift":  # lift to the unit's height while the jaws open, arm folded straight ahead
                s = smooth(u)
                r.lift = lerp(self.lift0, self.lift_target, s)
                r.arm_ext = lerp(self.ext0, ARM_STOW_EXT_M, s)
                r.pan = lerp(self.pan0, 0.0, s)
                r.grip = lerp(self.grip0, 1.0, s)
            elif name == "reach":
                r.lift, r.pan, r.grip = self.lift_target, 0.0, 1.0
                r.arm_ext = lerp(ARM_STOW_EXT_M, self.reach_ext, smoother(u))
            elif name == "close":
                r.arm_ext = self.reach_ext
                r.grip = 1.0 - smooth(u)
            elif name == "raise":  # jaws closed: the unit lifts off the shelf
                self._attach()
                r.grip = 0.0
                r.lift = self.lift_target + GRASP_LIFT_OFF_M * smooth(u)
            else:  # retract, settling back to the unit's height
                self._attach()
                s = smoother(u)
                r.arm_ext = lerp(self.reach_ext, ARM_STOW_EXT_M, s)
                r.lift = self.lift_target + GRASP_LIFT_OFF_M * (1 - s)
            self.core.sync_carried(r)
            return None
        self._attach()
        r.arm_ext, r.lift, r.pan, r.grip = ARM_STOW_EXT_M, self.lift_target, 0.0, 0.0
        self.core.sync_carried(r)
        held = self.core.scene_of(r).instances[self.target.instance_id]
        self.core.emit(InstanceInfo(store_id=r.store_id, robot_id=r.robot_id, slot_id=held.slot_id, instances=[held]))
        return self.core.result(self.cmd, ok=True, rover=r, instance_id=held.instance_id, instances=[held])


class _Place(_Action):
    status = RobotStatus.PLACING

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        super().__init__(core, rover, cmd)
        if rover.holding is None or (cmd.instance_id is not None and cmd.instance_id != rover.holding):
            raise _Fail("no_instance", f"not holding {cmd.instance_id or 'anything'}")
        if len(rover.basket) >= rover.robot.basket_capacity:
            raise _Fail("grasp_failed", f"basket full ({rover.robot.basket_capacity} units)")
        self.iid = rover.holding
        scene = core.scene_of(rover)
        self.dims = scene.item_dims(self.iid)
        dims = [scene.item_dims(i) for i in rover.basket] + [self.dims]
        layout = basket_layout(dims)
        self.target_local = layout[-1]
        tops = [p[2] + d[2] / 2 for p, d in zip(layout, dims, strict=True)]
        # carry the unit over the rim / the heap, swing round the mast, release over its stack
        self.lift_drop = clamp_lift(max(BASKET_DROP_LIFT_M, max(tops) + self.dims[2] / 2 + 0.06))
        tx, ty = self.target_local[0], self.target_local[1]
        ry = math.copysign(max(abs(ty), BASKET_RELEASE_MIN_Y_M), ty if abs(ty) > 1e-6 else 1.0)
        pan, ext_center = pan_reach_for((tx, ry))
        self.pan_rel = pan
        self.ext_rel = max(ARM_STOW_EXT_M, ext_center - self.dims[1] / 2 - HELD_CLEARANCE_M)
        self.lift0, self.ext0, self.pan0 = rover.lift, rover.arm_ext, rover.pan
        self.phases = _Phases(
            [
                ("lift", lift_move_s(rover.lift, self.lift_drop)),
                ("swing", PLACE_SWING_S),
                ("release", PLACE_RELEASE_S),
                ("drop", PLACE_DROP_S),
                ("back", PLACE_BACK_S),
            ]
        )
        self.released = False
        self.fall_from: tuple[Point3, float] | None = None

    def _release(self) -> None:
        if self.released:
            return
        r, scene = self.rover, self.core.scene_of(self.rover)
        inst = scene.instances[self.iid]
        self.fall_from = (inst.position, r.pose.theta - math.pi / 2 + r.pan)
        scene.instances[self.iid] = inst.model_copy(update={"in_basket": True})
        r.basket.append(self.iid)
        r.holding = None
        r.free[self.iid] = self.fall_from
        self.released = True

    def _fall(self, u: float) -> None:
        """The released unit drops onto its stack (gravity-like ease-in), turning to the basket yaw."""
        if self.fall_from is None:
            return
        (p0, yaw0), r = self.fall_from, self.rover
        p1 = to_store(r.pose, self.target_local)
        yaw1 = r.pose.theta + math.pi / 2
        s, g = smooth(u), u * u
        dyaw = math.atan2(math.sin(yaw1 - yaw0), math.cos(yaw1 - yaw0))
        r.free[self.iid] = (
            Point3(x=lerp(p0.x, p1.x, s), y=lerp(p0.y, p1.y, s), z=lerp(p0.z, p1.z, g)),
            yaw0 + dyaw * s,
        )

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        r = self.rover
        at = self.phases.at(self.elapsed)
        if at is not None:
            name, u = at
            if name == "lift":
                s = smooth(u)
                r.lift = lerp(self.lift0, self.lift_drop, s)
                r.arm_ext = lerp(self.ext0, ARM_STOW_EXT_M, s)
                r.pan, r.grip = lerp(self.pan0, 0.0, s), 0.0
            elif name == "swing":  # pan round the mast first, then stretch out over the stack
                r.lift = self.lift_drop
                r.pan = lerp(0.0, self.pan_rel, smoother(u))
                r.arm_ext = lerp(ARM_STOW_EXT_M, self.ext_rel, smooth((u - 0.25) / 0.75))
            elif name == "release":
                r.pan, r.arm_ext = self.pan_rel, self.ext_rel
                r.grip = smooth(u)
                if u >= 0.4:
                    self._release()
                    self._fall(0.0)
            elif name == "drop":
                self._release()
                r.grip = 1.0
                self._fall(u)
            else:  # back: swing straight ahead, fold, close the jaws
                self._release()
                r.free.pop(self.iid, None)
                s = smoother(u)
                r.pan = lerp(self.pan_rel, 0.0, s)
                r.arm_ext = lerp(self.ext_rel, ARM_STOW_EXT_M, smooth(u / 0.7))
                r.grip = 1.0 - s
            self.core.sync_carried(r)
            return None
        self._release()
        r.free.pop(self.iid, None)
        r.pan, r.arm_ext, r.grip = 0.0, ARM_STOW_EXT_M, 0.0
        scene = self.core.scene_of(r)
        self.core.sync_carried(r)
        placed = scene.instances[self.iid]
        self.core.emit(
            InstanceInfo(store_id=r.store_id, robot_id=r.robot_id, slot_id=placed.slot_id, instances=[placed])
        )
        return self.core.result(self.cmd, ok=True, rover=r, instance_id=self.iid, instances=[placed])


@dataclass
class _Counter:
    """The checkout counter in front of an unloading robot: how far to drive up, where units go down, conveyor."""

    drive_m: float
    top_z: float
    place_local: tuple[float, float]
    """Robot-frame (x, y) at the approached pose where the arm sets units down."""
    belt_dir: tuple[float, float]
    """Store-frame unit vector along the counter (the conveyor carries units this way)."""
    belt_room_m: float
    across: tuple[float, float]
    """Store-frame unit vector across the counter, away from the robot (next conveyor row)."""


def find_counter(scene: StoreScene, pose: Pose2D) -> _Counter | None:
    """The checkout fixture straight ahead of a robot at ``pose`` (within COUNTER_APPROACH_MAX_M of its bumper)."""
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    front = BASE_SIZE_M[0] / 2 + 0.02
    best: _Counter | None = None
    b = scene.spec.bounds
    for fx in scene.planogram.fixtures:
        if fx.kind != "checkout":
            continue
        r = fx.rect
        # ray from the robot centre along its heading against the (axis-aligned) counter rect
        tmin, tmax = -math.inf, math.inf
        hit = True
        for p0, d, lo, hi in ((pose.x, c, r.x, r.x_max), (pose.y, s, r.y, r.y_max)):
            if abs(d) < 1e-9:
                if not lo <= p0 <= hi:
                    hit = False
                continue
            t1, t2 = (lo - p0) / d, (hi - p0) / d
            tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
        if not hit or tmax < max(tmin, 0.0) or tmin < 0:
            continue
        drive = tmin - front - COUNTER_GAP_M
        if not -0.05 <= drive <= COUNTER_APPROACH_MAX_M:
            continue
        drive = max(0.0, drive)
        long_x = r.w >= r.h
        axis = (1.0, 0.0) if long_x else (0.0, 1.0)
        # conveyor runs toward the counter end nearer the store centre (in view of the store)
        mid = (b.x + b.w / 2, b.y + b.h / 2)
        here = pose.x + c * tmin, pose.y + s * tmin
        to_mid = (mid[0] - here[0]) * axis[0] + (mid[1] - here[1]) * axis[1]
        sign = 1.0 if to_mid >= 0 else -1.0
        belt = (axis[0] * sign, axis[1] * sign)
        end = (r.x_max if sign > 0 else r.x) if long_x else (r.y_max if sign > 0 else r.y)
        start = here[0] if long_x else here[1]
        room = abs(end - start) - 0.12
        cand = _Counter(
            drive_m=drive,
            top_z=fx.height_m,
            place_local=(front + COUNTER_GAP_M + 0.11, 0.0),
            belt_dir=belt,
            belt_room_m=max(0.3, room),
            across=(c, s),
        )
        if best is None or cand.drive_m < best.drive_m:
            best = cand
    return best


class _Unload(_Action):
    """At checkout: drive up to the counter, the arm moves every basket unit onto it (the conveyor carries them off),
    back off; the units leave the ground truth and the result lists them (they stay on the counter as visuals for
    LEFTOVER_S). Without a counter in front: the units just leave the scene after UNLOAD_S."""

    status = RobotStatus.PLACING

    def __init__(self, core: "WorkerCore", rover: Rover, cmd: Any) -> None:
        super().__init__(core, rover, cmd)
        scene = core.scene_of(rover)
        self.counter = find_counter(scene, rover.pose) if rover.basket else None
        self.start_pose = rover.pose
        self.order = list(reversed(rover.basket))  # top of the heap first
        self.unit_k = 0
        self.belt_used = 0.0
        self.belt_row = 0
        self.queue: list[tuple[str, float, Callable[[float], None]]] = []
        self.q_t = 0.0
        if self.counter is not None:
            approach_s = max(0.6, self.counter.drive_m / COUNTER_SPEED_MPS)
            self.queue.append(("approach", approach_s, self._approach))
            self._plan_unit()

    # -- sub-motions ---------------------------------------------------------------------------------------------------
    def _approach(self, u: float, back: bool = False) -> None:
        assert self.counter is not None
        r, d = self.rover, self.counter.drive_m * (1 - smooth(u) if back else smooth(u))
        p = self.start_pose
        self.core.set_pose(r, Pose2D(x=p.x + math.cos(p.theta) * d, y=p.y + math.sin(p.theta) * d, theta=p.theta))
        r.lift = lerp(r.lift, LIFT_REST_M, smooth(u)) if back else r.lift
        if back:
            r.pan = lerp(r.pan, 0.0, smooth(u))
            r.arm_ext = lerp(r.arm_ext, ARM_STOW_EXT_M, smooth(u))
            r.grip = lerp(r.grip, 0.0, smooth(u))

    def _plan_unit(self) -> None:
        """Queue the motions for the next basket unit (or the back-off when the basket is empty)."""
        if self.unit_k >= len(self.order):
            self.queue.append(("back_off", max(0.6, self.counter.drive_m / COUNTER_SPEED_MPS), self._back))  # type: ignore[union-attr]
            return
        fast = 1 if self.unit_k >= UNLOAD_SLOW_UNITS else 0
        iid = self.order[self.unit_k]
        r, scene = self.rover, self.core.scene_of(self.rover)
        dims = scene.item_dims(iid)
        # where the unit sits in the basket now (robot frame)
        layout = basket_layout([scene.item_dims(i) for i in r.basket])
        idx = r.basket.index(iid) if iid in r.basket else len(r.basket) - 1
        bx, by, bz = layout[idx] if layout else BASKET_OFFSET_M
        ry = math.copysign(max(abs(by), BASKET_RELEASE_MIN_Y_M), by if abs(by) > 1e-6 else 1.0)
        pan_b, ext_b = pan_reach_for((bx, ry))
        ext_b = max(ARM_STOW_EXT_M, ext_b - dims[1] / 2 - HELD_CLEARANCE_M)
        lift_b = clamp_lift(bz)
        assert self.counter is not None
        px, py = self.counter.place_local
        pan_c, ext_c = pan_reach_for((px, py))
        ext_c = max(ARM_STOW_EXT_M, min(ARM_FULL_EXT_M, ext_c - dims[1] / 2 - HELD_CLEARANCE_M))
        lift_c = clamp_lift(self.counter.top_z + dims[2] / 2 + 0.02)
        rim = BASKET_OFFSET_M[2] + BASKET_SIZE_M[2] + dims[2] / 2 + 0.04
        state: dict[str, float] = {}

        def to_basket(u: float) -> None:
            if not state:
                state.update(lift=r.lift, pan=r.pan, ext=r.arm_ext)
            s = smoother(u)
            r.pan = lerp(state["pan"], pan_b, s)
            r.arm_ext = lerp(state["ext"], ext_b, smooth((u - 0.3) / 0.7))
            # over the rim first, then down to the unit
            hi = max(rim, lift_b)
            r.lift = lerp(state["lift"], hi, smooth(u / 0.5)) if u < 0.5 else lerp(hi, lift_b, smooth((u - 0.5) / 0.5))
            r.grip = smooth(u)

        def grab(u: float) -> None:
            r.grip = 1.0 - smooth(u)
            if u >= 0.6 and r.carry != iid:
                r.carry = iid
                if iid in r.basket:
                    r.basket.remove(iid)

        def to_counter(u: float) -> None:
            r.carry = iid
            s = smoother(u)
            hi = max(rim, lift_c)
            r.pan = lerp(pan_b, pan_c, s)
            r.arm_ext = lerp(ext_b, ext_c, smooth((u - 0.2) / 0.8))
            r.lift = lerp(lift_b, hi, smooth(u / 0.4)) if u < 0.4 else lerp(hi, lift_c, smooth((u - 0.4) / 0.6))

        def release(u: float) -> None:
            r.grip = smooth(u)
            if u >= 0.5 and r.carry == iid:
                r.carry = None
                self._to_belt(iid, dims)
            if u >= 1.0 - 1e-9:
                self.unit_k += 1
                self._plan_unit()

        self.queue += [
            ("to_basket", UNLOAD_MOVE_S[fast], to_basket),
            ("grab", UNLOAD_GRAB_S[fast], grab),
            ("to_counter", UNLOAD_MOVE_S[fast], to_counter),
            ("release", UNLOAD_RELEASE_S[fast], release),
        ]

    def _back(self, u: float) -> None:
        self._approach(u, back=True)

    def _to_belt(self, iid: str, dims: Vec3) -> None:
        """Set the unit down on the counter and hand it to the conveyor (core.leftovers slides it to its spot)."""
        assert self.counter is not None
        r, cnt = self.rover, self.counter
        scene = self.core.scene_of(r)
        here = to_store(r.pose, (*cnt.place_local, cnt.top_z + dims[2] / 2))
        w = dims[0]
        if self.belt_used + w > cnt.belt_room_m and self.belt_used > 0:
            self.belt_row, self.belt_used = self.belt_row + 1, 0.0
        along = 0.18 + self.belt_used + w / 2
        self.belt_used += w + 0.03
        depth = 0.2 * self.belt_row
        goal = Point3(
            x=here.x + cnt.belt_dir[0] * along + cnt.across[0] * depth,
            y=here.y + cnt.belt_dir[1] * along + cnt.across[1] * depth,
            z=here.z,
        )
        yaw = math.atan2(cnt.belt_dir[1], cnt.belt_dir[0])
        r.free.pop(iid, None)
        self.core.move_instance(scene, iid, here, yaw)
        self.core.add_leftover(scene.store_id, iid, here, goal, yaw)

    # -- state machine -------------------------------------------------------------------------------------------------
    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        r = self.rover
        if self.counter is None:
            if self.elapsed < (UNLOAD_S if r.basket else 0.0):
                return None
            return self._finish(remove=True)
        self.q_t += dt
        while self.queue:
            name, dur, fn = self.queue[0]
            if self.q_t < dur:
                fn(self.q_t / dur if dur > 0 else 1.0)
                self.core.sync_carried(r)
                return None
            self.q_t -= dur
            self.queue.pop(0)
            fn(1.0)  # may queue the next unit's motions
        self.core.sync_carried(r)
        return self._finish(remove=False)

    def _finish(self, remove: bool) -> CommandResult:
        r = self.rover
        scene = self.core.scene_of(r)
        out: list[ItemInstance] = []
        for iid in list(reversed(self.order)) if not remove else list(r.basket):
            inst = scene.instances.pop(iid, None)
            if inst is not None:
                out.append(inst.model_copy(update={"in_basket": True}))
                if remove:
                    self.core.backend.remove_instance(scene.store_id, iid)
        r.basket, r.carry = [], None
        r.pan, r.arm_ext, r.grip = 0.0, ARM_STOW_EXT_M, 0.0
        if not remove:
            self.core.set_pose(r, self.start_pose)
        return self.core.result(self.cmd, ok=True, rover=r, instances=out)


_ACTIONS: dict[str, type[_Action]] = {
    "navigate": _Navigate,
    "scan": _Scan,
    "pick": _Pick,
    "place_in_basket": _Place,
    "unload_basket": _Unload,
}

SceneLoader = Callable[[str], tuple[Planogram, list[Product]]]
RobotsLoader = Callable[[], tuple[list[RobotSpec], list[StoreCameraSpec]]]


def default_scene_loader(store_id: str) -> tuple[Planogram, list[Product]]:
    from utils.data import load_planogram, load_products

    return load_planogram(store_id), list(load_products().values())


def default_robots_loader() -> tuple[list[RobotSpec], list[StoreCameraSpec]]:
    """data/fixtures/robots.yaml (robots + ceiling cameras); empty when the file is missing / invalid."""
    try:
        from utils.data import load_robots_file

        rf = load_robots_file()
        return list(rf.robots), list(rf.store_cameras)
    except Exception:  # noqa: BLE001 - fall back to default_robots / default_store_cameras per store
        return [], []


# --------------------------------------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------------------------------------
class WorkerCore:
    """Single-threaded: call only from the main (render) loop. Outgoing messages go through ``emit``."""

    def __init__(
        self,
        backend: SimBackend,
        emit: Callable[[Any], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        loader: SceneLoader = default_scene_loader,
        robots_loader: RobotsLoader = default_robots_loader,
        default_store: str | None = None,
        assets: SkuAssets | None = None,
        rover_usd: str | None = None,
    ) -> None:
        self.backend, self.emit, self.clock = backend, emit, clock
        self.loader, self.robots_loader = loader, robots_loader
        self.default_store = default_store
        self.assets = assets
        """Per-item grocery models (None / inactive = primitives; see arms.sim.assets)."""
        self.rover_usd = rover_usd
        """Rover USD for the visual puppet (None = primitive rovers)."""
        self.stores: dict[str, StoreScene] = {}
        self.store_order: list[str] = []
        self.offsets: dict[str, Vec3] = {}
        self.rovers: dict[str, Rover] = {}
        self.streams: dict[str, float] = {}
        """camera_id -> next due time (monotonic clock)."""
        self.stream_fps = DEFAULT_STREAM_FPS
        self._frame_n = 0
        self.leftovers: dict[str, dict[str, list[Any]]] = {}
        """store_id -> instance_id -> [position, goal, yaw, seconds left]: unloaded units on the checkout counter
        (visual only: no longer ground truth), slid along the conveyor, removed after LEFTOVER_S."""
        self.sim_time = 0.0
        """Sum of tick dt (sim seconds; the recorder steps it at a fixed rate)."""
        self.on_command: Callable[[str, Any, Any], None] | None = None
        """Optional observer (recorder timing log): ('start' | 'done', cmd, result-or-None)."""

    # ---- hosting ---------------------------------------------------------------------------------------------------
    def load_store(
        self,
        store_id: str | None = None,
        *,
        planogram: Planogram | None = None,
        products: list[Product] | None = None,
        robots: list[RobotSpec] | None = None,
        cameras: list[StoreCameraSpec] | None = None,
        seed: int | None = None,
    ) -> StoreScene:
        """(Re)build one store and respawn its robots at their docks (cancels their motions)."""
        sid = (planogram.store_id if planogram else None) or store_id
        if not sid:
            raise _Fail("not_ready", "reset needs a store_id")
        old = self.stores.get(sid)
        if planogram is None or products is None:
            loaded_plan: Planogram | None = None
            loaded_products: list[Product] | None = None
            load_error = ""
            try:
                loaded_plan, loaded_products = self.loader(sid)
            except Exception as exc:  # noqa: BLE001 - fall back to what the old scene had
                load_error = f": {exc}"
            planogram = planogram or loaded_plan or (old.planogram if old else None)
            if products is None:
                products = loaded_products or (list(old.products.values()) if old else None)
            if planogram is None:
                raise _Fail("not_ready", f"no planogram for store {sid!r}{load_error}")
        products = products or []
        file_robots, file_cams = self.robots_loader()
        if robots is None:
            robots = [r.robot for r in self.rovers.values() if r.store_id == sid] or [
                r for r in file_robots if r.store_id == sid
            ]
        robots = [r for r in robots if r.store_id == sid] or default_robots(planogram)
        if cameras is None:
            cameras = (old.cameras if old else None) or [c for c in file_cams if c.store_id == sid]
        cameras = [c for c in cameras if c.store_id == sid] or default_store_cameras(planogram)

        spec = build_scene_spec(planogram, products, seed=seed or 0, assets=self.assets)
        for w in spec.warnings:
            self.emit(SimLog(level="warning", message=f"{sid} scene: {w}"))
        if self.assets is not None and self.assets.active:
            info = self.assets.summary(sorted(spec.products))
            why = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(info["why"].items()))
            self.emit(
                SimLog(
                    level="info",
                    message=f"{sid} scene: {info['models']} SKUs on per-item models, {info['primitive']} primitive"
                    + (f" ({why})" if why else ""),
                )
            )
        # cancel + remove this store's robots (and any robot id that moves here from another store)
        new_ids = {r.robot_id for r in robots}
        for rover in [r for r in self.rovers.values() if r.store_id == sid or r.robot_id in new_ids]:
            if rover.active is not None:
                self._fail(rover.active.cmd, "cancelled", f"store {sid} reset", rover=rover)
            self.backend.remove_robot(rover.robot_id)
            del self.rovers[rover.robot_id]
        if sid not in self.store_order:
            self.store_order.append(sid)
        scene = StoreScene(
            spec=spec,
            planogram=planogram,
            products={p.sku: p for p in products},
            cameras=cameras,
            instances={i.instance_id: i for i in spec.instances},
        )
        self.stores[sid] = scene
        self.leftovers.pop(sid, None)  # the rebuild drops their prims
        self._update_offsets()
        self.backend.build_store(spec, self.offsets[sid])
        for r in robots:
            self._spawn(r)
        self.emit(InstanceInfo(store_id=sid, instances=list(scene.instances.values())))
        for rover in self.rovers.values():
            if rover.store_id == sid:
                self.publish_state(rover, force=True)
        return scene

    def load_stores(self, store_ids: list[str]) -> list[str]:
        """Startup: build each store from data/ (errors are logged, not raised). Returns the loaded ids."""
        ok: list[str] = []
        for sid in store_ids:
            try:
                self.load_store(sid)
                ok.append(sid)
            except Exception as exc:  # noqa: BLE001 - the gateway's ResetCmd can push the data later
                self.emit(SimLog(level="warning", message=f"no initial scene for {sid}: {exc}"))
        return ok

    def _update_offsets(self) -> None:
        sizes = {sid: (s.spec.bounds.w, s.spec.bounds.h) for sid, s in self.stores.items()}
        new = store_offsets([s for s in self.store_order if s in self.stores], sizes)
        for sid, off in new.items():
            if sid in self.offsets and self.offsets[sid] != off:
                self.backend.set_store_offset(sid, off)
        self.offsets = new

    def _spawn(self, robot: RobotSpec) -> Rover:
        rover = Rover(robot, model_usd=self.rover_usd)
        self.rovers[robot.robot_id] = rover
        self.backend.spawn_robot(rover.spec)
        self.set_pose(rover, robot.dock)
        return rover

    def _respawn(self, rover: Rover, robot: RobotSpec | None = None) -> Rover:
        """Robot-level reset: dock, empty basket + gripper (those units leave the scene), battery from the spec."""
        scene = self.stores.get(rover.store_id)
        for iid in [*rover.basket, *([rover.holding] if rover.holding else [])]:
            if scene is not None and scene.instances.pop(iid, None) is not None:
                self.backend.remove_instance(scene.store_id, iid)
        if robot is not None and robot != rover.robot:
            self.backend.remove_robot(rover.robot_id)
            del self.rovers[rover.robot_id]
            return self._spawn(robot)
        rover.basket, rover.holding, rover.error = [], None, None
        rover.carry, rover.free = None, {}
        rover.battery = rover.robot.battery_pct
        rover.lift, rover.arm_ext, rover.pan, rover.grip = LIFT_REST_M, ARM_STOW_EXT_M, 0.0, 0.0
        self.set_pose(rover, rover.robot.dock)
        return rover

    def hosting_key(self) -> tuple[Any, ...]:
        return (
            tuple((sid, s.spec.data_hash) for sid, s in self.stores.items()),
            tuple(sorted((r.robot_id, r.store_id) for r in self.rovers.values())),
            tuple(c.camera_id for c in self.camera_infos()),
        )

    def store_infos(self) -> list[SimStoreInfo]:
        return [
            SimStoreInfo(
                store_id=sid,
                data_hash=s.spec.data_hash,
                camera_ids=[c.camera_id for c in s.cameras],
                loaded=True,
            )
            for sid, s in self.stores.items()
        ]

    def robot_infos(self) -> list[SimRobotInfo]:
        return [
            SimRobotInfo(
                robot_id=r.robot_id, store_id=r.store_id, kind=r.robot.kind, camera_ids=list(r.spec.camera_ids)
            )
            for r in self.rovers.values()
        ]

    def camera_infos(self) -> list[CameraInfo]:
        source = getattr(self.backend, "camera_source", "isaac")
        robots = [r.robot for r in self.rovers.values()]
        cams = [c for s in self.stores.values() for c in s.cameras]
        return camera_infos(robots, cams, source=source)

    def hello(self, token: str, worker_id: str = "isaac-0", sim_version: str | None = None) -> SimHello:
        return SimHello(
            token=token,
            worker_id=worker_id,
            stores=self.store_infos(),
            robots=self.robot_infos(),
            cameras=self.camera_infos(),
            capabilities=list(CAPABILITIES),
            sim_version=sim_version,
            contracts_version=CONTRACTS_VERSION,
        )

    def hosted(self, worker_id: str = "isaac-0") -> SimHosted:
        return SimHosted(
            worker_id=worker_id, stores=self.store_infos(), robots=self.robot_infos(), cameras=self.camera_infos()
        )

    # ---- lookups ---------------------------------------------------------------------------------------------------
    def rover(self, robot_id: str | None) -> Rover:
        if not robot_id:
            raise _Fail("unknown_robot", "this command needs a robot_id")
        rover = self.rovers.get(robot_id)
        if rover is None:
            raise _Fail("unknown_robot", f"robot {robot_id!r} is not hosted here")
        return rover

    def scene_of(self, rover: Rover) -> StoreScene:
        scene = self.stores.get(rover.store_id)
        if scene is None:
            raise _Fail("not_ready", f"store {rover.store_id} has no scene; send reset")
        return scene

    def require_slot(self, rover: Rover, slot_id: str) -> Slot:
        slot = self.scene_of(rover).slot(slot_id)
        if slot is None:
            raise _Fail("unknown_slot", f"unknown slot {slot_id!r} in {rover.store_id}")
        return slot

    def held_ids(self, store_id: str) -> set[str]:
        return {r.holding for r in self.rovers.values() if r.store_id == store_id and r.holding}

    def blocking_rover(self, rover: Rover, new: Pose2D) -> Rover | None:
        """Another rover in the same store the move to ``new`` would bump into (getting closer within 2R)."""
        for other in self.rovers.values():
            if other is rover or other.store_id != rover.store_id:
                continue
            d_new = new.distance_to(other.pose)
            if d_new < 2 * ROBOT_RADIUS_M and d_new < rover.pose.distance_to(other.pose) - 1e-9:
                return other
        return None

    # ---- robot + instances -----------------------------------------------------------------------------------------
    def set_pose(self, rover: Rover, pose: Pose2D) -> None:
        rover.pose = pose
        self.backend.set_robot_pose(rover.robot_id, pose, rover.lift, rover.arm_ext)
        self.sync_carried(rover)

    def move_instance(self, scene: StoreScene, iid: str, position: Point3, yaw: float | None = None) -> None:
        inst = scene.instances.get(iid)
        if inst is None:
            return
        if inst.position == position and (yaw is None or self._last_yaw.get((scene.store_id, iid)) == yaw):
            return  # unchanged: keep the renderer's scene clean (no extra synchronous render step)
        scene.instances[iid] = inst.model_copy(update={"position": position})
        if yaw is not None:
            self._last_yaw[(scene.store_id, iid)] = yaw
        self.backend.set_instance_pose(scene.store_id, iid, position, yaw)

    @functools.cached_property
    def _last_yaw(self) -> dict[tuple[str, str], float]:
        return {}

    def sync_carried(self, rover: Rover) -> None:
        """Held + carried + basket units ride with the robot (and the lift / arm); ``rover.free`` units are posed by
        their action (falling into the basket, set down on the counter)."""
        scene = self.stores.get(rover.store_id)
        if scene is None:
            return
        arm = getattr(self.backend, "set_robot_arm", None)
        if arm is not None:
            arm(rover.robot_id, rover.pan, rover.grip)
        else:
            gripper = getattr(self.backend, "set_robot_gripper", None)
            if gripper is not None:  # jaws open while reaching for a unit, closed when holding or stowed
                gripper(rover.robot_id, rover.grip < 0.5)
        self.backend.set_robot_pose(rover.robot_id, rover.pose, rover.lift, rover.arm_ext)
        for iid in (rover.holding, rover.carry):
            if iid is not None and iid in scene.instances and iid not in rover.free:
                dims = scene.item_dims(iid)
                # held: label (item -y) toward the robot, as it faced the robot on the shelf, so the wrist camera reads
                # it (yaw + pi flipped it at the grasp and the wrist camera only saw the unit's blank back)
                pos = held_position(rover.pose, rover.lift, rover.arm_ext, dims, pan=rover.pan)
                self.move_instance(scene, iid, pos, rover.pose.theta - math.pi / 2 + rover.pan)
        in_basket = [i for i in rover.basket if i in scene.instances]
        if in_basket:
            yaw = rover.pose.theta + math.pi / 2
            layout = basket_layout([scene.item_dims(i) for i in in_basket])
            for iid, local in zip(in_basket, layout, strict=True):
                if iid not in rover.free:
                    self.move_instance(scene, iid, to_store(rover.pose, local), yaw)
        for iid, (pos, fyaw) in list(rover.free.items()):
            self.move_instance(scene, iid, pos, fyaw)

    def add_leftover(self, store_id: str, iid: str, position: Point3, goal: Point3, yaw: float) -> None:
        """An unloaded unit on the checkout counter: the conveyor slides it to ``goal`` (visual only)."""
        self.leftovers.setdefault(store_id, {})[iid] = [position, goal, yaw, LEFTOVER_S]

    def _tick_leftovers(self, dt: float) -> None:
        for sid, units in list(self.leftovers.items()):
            scene = self.stores.get(sid)
            for iid, item in list(units.items()):
                pos, goal, yaw, left = item
                left -= dt
                if left <= 0 or scene is None:
                    units.pop(iid, None)
                    if scene is not None and iid not in scene.instances:
                        self.backend.remove_instance(sid, iid)
                    continue
                item[3] = left
                dx, dy = goal.x - pos.x, goal.y - pos.y
                dist = math.hypot(dx, dy)
                if dist < 1e-4:
                    continue
                stepm = min(dist, CONVEYOR_SPEED_MPS * dt * min(1.0, 0.3 + dist / 0.15))
                new = Point3(x=pos.x + dx / dist * stepm, y=pos.y + dy / dist * stepm, z=pos.z)
                item[0] = new
                if scene is not None and iid in scene.instances:
                    self.move_instance(scene, iid, new, yaw)
                else:
                    self.backend.set_instance_pose(sid, iid, new, yaw)
            if not units:
                self.leftovers.pop(sid, None)

    def state(self, rover: Rover) -> RobotState:
        scene = self.stores.get(rover.store_id)
        skus = [scene.instances[i].sku for i in rover.basket if i in scene.instances] if scene else []
        return RobotState(
            robot_id=rover.robot_id,
            store_id=rover.store_id,
            pose=rover.pose,
            status=rover.status,
            holding=rover.holding,
            basket=list(rover.basket),
            basket_skus=skus,
            lift_height_m=round(rover.lift, 3),
            battery_pct=round(min(100.0, max(0.0, rover.battery)), 2),
            charging=rover.status == RobotStatus.CHARGING,
            current_cmd_id=rover.active.cmd.cmd_id if rover.active else None,
            error=rover.error,
        )

    def publish_state(self, rover: Rover, force: bool = False) -> None:
        now = self.clock()
        st = self.state(rover)
        key = (
            round(st.pose.x, 2),
            round(st.pose.y, 2),
            round(st.pose.theta, 2),
            st.status,
            st.holding,
            len(st.basket),
            round(rover.lift, 2),
            int(rover.battery),
        )
        if force or (key != rover.last_key and now - rover.last_t >= STATE_MIN_PERIOD_S):
            rover.last_key, rover.last_t = key, now
            self.emit(st)

    # ---- cameras ---------------------------------------------------------------------------------------------------
    def resolve_camera(self, camera_id: str) -> tuple[str, str, Rover | None, StoreCameraSpec | None]:
        """camera_id -> (kind, store_id, rover for robot cams, spec for ceiling cams); unknown -> unknown_robot."""
        try:
            owner, kind = parse_camera_id(camera_id)
        except ValueError as exc:
            raise _Fail("unknown_robot", str(exc)) from exc
        if kind == "ceiling":
            scene = self.stores.get(owner)
            spec = next((c for c in scene.cameras if c.camera_id == camera_id), None) if scene else None
            if spec is None:
                raise _Fail("unknown_robot", f"camera {camera_id!r} is not hosted here")
            return kind, owner, None, spec
        rover = self.rovers.get(owner)
        if rover is None or kind not in rover.robot.cameras:
            raise _Fail("unknown_robot", f"camera {camera_id!r} is not hosted here")
        return kind, rover.store_id, rover, None

    def camera_pose(self, camera_id: str, slot: Slot | None = None) -> CameraPose:
        kind, store_id, rover, spec = self.resolve_camera(camera_id)
        if spec is not None:
            return mount_camera(spec.mount)
        assert rover is not None
        aim = None
        if slot is not None and kind == "front":
            aim = self._slot_aim(self.stores[store_id], slot)
        return robot_camera_pose(
            kind, rover.pose, lift_height_m=rover.lift, arm_ext_m=rover.arm_ext, aim_at=aim, pan=rover.pan
        )

    @staticmethod
    def _slot_aim(scene: StoreScene, slot: Slot) -> Point3:
        layout = scene.spec.slot_layouts.get(slot.slot_id)
        h = layout.dims[2] if layout else 0.1
        p = slot.item_position
        return Point3(x=p.x, y=p.y, z=p.z + h / 2)

    def capture(
        self, camera_id: str, *, slot: Slot | None = None, scan: bool = False, stream: bool = False
    ) -> CameraFrame | None:
        """Render one RAW frame. Raises _Fail('unknown_robot') for unknown cameras; None when no frame is ready."""
        kind, store_id, rover, _ = self.resolve_camera(camera_id)
        scene = self.stores.get(store_id)
        if scene is None:
            raise _Fail("not_ready", f"store {store_id} has no scene")
        cam = self.camera_pose(camera_id, slot)
        rendered = self.backend.render(camera_id, store_id, cam)
        if rendered is None:
            return None
        jpeg, w, h = rendered
        cam = cam.sized(w, h)
        instances: list[ItemInstance] = []
        if not stream:
            held = self.held_ids(store_id)
            for inst in scene.instances.values():
                if scan:
                    if slot is None or inst.slot_id != slot.slot_id or inst.in_basket or inst.instance_id in held:
                        continue
                bbox = project_box(cam, inst.position, scene.item_dims(inst.instance_id), self._yaw(scene, inst))
                if scan or bbox is not None:
                    instances.append(inst.model_copy(update={"bbox": bbox}))
            instances.sort(key=lambda i: (instance_index(i.instance_id), i.instance_id) if scan else (i.instance_id,))
        self._frame_n += 1
        return CameraFrame(
            frame_id=f"{camera_id}-f{self._frame_n:06d}",
            camera=kind,  # type: ignore[arg-type]
            camera_id=camera_id,
            robot_id=rover.robot_id if rover else None,
            store_id=store_id,
            jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
            w=w,
            h=h,
            blurred=False,
            slot_id=slot.slot_id if slot else None,
            pose=rover.pose if rover else None,
            camera_pose=cam.to_mount(),
            instances=instances,
        )

    def _yaw(self, scene: StoreScene, inst: ItemInstance) -> float:
        yaw = scene.spec.item_yaw.get(inst.instance_id, 0.0)
        if inst.in_basket:
            owner = next((r for r in self.rovers.values() if inst.instance_id in r.basket), None)
            if owner is not None:
                yaw = owner.pose.theta + math.pi / 2
        return yaw

    def set_streams(self, camera_ids: list[str], fps: float) -> list[str]:
        """Replace the streaming set; returns the camera ids that are not hosted (ignored)."""
        now = self.clock()
        known = {c.camera_id for c in self.camera_infos()}
        self.stream_fps = max(0.1, min(MAX_STREAM_FPS, fps))
        self.streams = {cid: now for cid in camera_ids if cid in known}
        return [cid for cid in camera_ids if cid not in known]

    def default_streams(self) -> None:
        """After welcome: every robot's front camera at 2 fps."""
        self.set_streams([robot_camera_id(r.robot_id, "front") for r in self.rovers.values()], DEFAULT_STREAM_FPS)

    def stream_due(self, now: float | None = None) -> list[str]:
        """Streaming cameras whose next frame is due (and schedules the following one)."""
        now = self.clock() if now is None else now
        period = 1.0 / self.stream_fps
        due = []
        for cid, t in self.streams.items():
            if now >= t:
                due.append(cid)
                self.streams[cid] = max(t + period, now)
        return due

    # ---- commands --------------------------------------------------------------------------------------------------
    def result(self, cmd: Any, ok: bool, rover: Rover | None = None, **kw: Any) -> CommandResult:
        started = rover.active_started if rover and rover.active and rover.active.cmd is cmd else self.clock()
        return CommandResult(
            cmd_id=cmd.cmd_id,
            robot_id=getattr(cmd, "robot_id", None),
            ok=ok,
            duration_ms=round((self.clock() - started) * 1000, 1),
            **kw,
        )

    def _observe(self, phase: str, cmd: Any, res: Any = None) -> None:
        if self.on_command is not None:
            try:
                self.on_command(phase, cmd, res)
            except Exception:  # noqa: BLE001 - an observer must never break the protocol
                pass

    def _finish(self, rover: Rover, res: CommandResult) -> None:
        cmd = rover.active.cmd if rover.active is not None else None
        rover.active = None
        self.emit(res.model_copy(update={"state": self.state(rover)}))
        self.publish_state(rover, force=True)
        if cmd is not None:
            self._observe("done", cmd, res)

    def _fail(self, cmd: Any, code: SimErrorCode, message: str, rover: Rover | None = None) -> None:
        res = self.result(cmd, ok=False, rover=rover, error_code=code, error=message)
        if rover is not None and rover.active is not None and rover.active.cmd is cmd:
            self._finish(rover, res)
        else:
            self.emit(res.model_copy(update={"state": self.state(rover) if rover else None}))

    def _reply(self, cmd: Any, rover: Rover | None = None, **kw: Any) -> None:
        state = self.state(rover) if rover else None
        self.emit(self.result(cmd, ok=True, rover=rover, state=state, **kw))

    def submit(self, cmd: Any) -> None:
        """Handle one GatewayToSim message (never raises; every command ends in exactly one CommandResult)."""
        kind = getattr(cmd, "type", None)
        if kind == "welcome":
            return
        rover: Rover | None = None
        try:
            if getattr(cmd, "robot_id", None) in self.rovers:
                rover = self.rovers[cmd.robot_id]
            if kind == "stop":
                self._stop(cmd)
            elif kind == "set_streams":
                missing = self.set_streams(list(cmd.camera_ids), cmd.fps)
                if missing:
                    self.emit(SimLog(level="warning", message=f"set_streams: not hosted here: {', '.join(missing)}"))
                self._reply(cmd)
            elif kind == "list_instances":
                self._list_instances(cmd, rover)
            elif kind == "set_slot_stock":
                self._set_slot_stock(cmd)
            elif kind == "capture":
                self._capture(cmd, rover)
            elif kind == "reset":
                self._reset(cmd)
            elif kind in _ACTIONS:
                rover = self.rover(cmd.robot_id)
                if rover.active is not None:
                    raise _Fail(
                        "busy", f"{rover.active.cmd.type} {rover.active.cmd.cmd_id} is running on {rover.robot_id}"
                    )
                action = _ACTIONS[kind](self, rover, cmd)
                rover.active = action
                rover.active_started = self.clock()
                rover.error = None
                self.publish_state(rover, force=True)
                self._observe("start", cmd)
            else:
                raise _Fail("internal", f"unsupported command {kind!r}")
        except _Fail as exc:
            self._fail(cmd, exc.code, exc.message, rover=rover)
        except Exception as exc:  # noqa: BLE001 - one result per cmd, whatever happens
            if rover is not None and rover.active is not None and rover.active.cmd is cmd:
                rover.active = None
            self._fail(cmd, "internal", f"{type(exc).__name__}: {exc}", rover=rover)

    def _stop(self, cmd: Any) -> None:
        if cmd.robot_id is None:
            targets = list(self.rovers.values())
        else:
            targets = [self.rover(cmd.robot_id)]
        for r in targets:
            if r.active is not None:
                self._fail(r.active.cmd, "cancelled", f"stopped by {cmd.cmd_id}", rover=r)
        self._reply(cmd, targets[0] if cmd.robot_id is not None else None)

    def _list_instances(self, cmd: Any, rover: Rover | None) -> None:
        if cmd.store_id:
            if cmd.store_id not in self.stores:
                raise _Fail("not_ready", f"store {cmd.store_id!r} is not hosted here")
            scenes = [self.stores[cmd.store_id]]
        elif cmd.robot_id:
            scenes = [self.scene_of(self.rover(cmd.robot_id))]
        else:
            scenes = list(self.stores.values())
        found = [i for s in scenes for i in s.instances.values() if cmd.slot_id is None or i.slot_id == cmd.slot_id]
        self._reply(cmd, rover, instances=found)

    def _capture(self, cmd: Any, rover: Rover | None) -> None:
        if cmd.camera_id:
            camera_id = cmd.camera_id
        elif cmd.robot_id:
            r = self.rover(cmd.robot_id)
            name = normalize_camera(cmd.camera)
            camera_id = store_camera_id(r.store_id, 1) if name == "ceiling" else robot_camera_id(r.robot_id, name)
        else:
            raise _Fail("unknown_robot", "capture needs camera_id or robot_id")
        frame = self.capture(camera_id)
        if frame is None:
            raise _Fail("not_ready", "renderer has no frame yet")
        self._reply(cmd, rover, frame=frame)

    def _reset(self, cmd: Any) -> None:
        before = self.hosting_key()
        given = {r.robot_id: r for r in (cmd.robots or [])}
        rover: Rover | None = None
        if cmd.robot_id is not None:
            rover = self.rovers.get(cmd.robot_id)
            spec = given.get(cmd.robot_id) or (rover.robot if rover else None)
            if spec is None:
                raise _Fail("unknown_robot", f"robot {cmd.robot_id!r} is not hosted here")
            if cmd.store_id and cmd.store_id != spec.store_id:
                raise _Fail("unknown_robot", f"{cmd.robot_id} works in {spec.store_id}, not {cmd.store_id}")
            if rover is not None and rover.active is not None:
                raise _Fail("busy", f"{rover.active.cmd.type} {rover.active.cmd.cmd_id} is running on {rover.robot_id}")
            loaded = self.stores.get(spec.store_id)
            stale = cmd.planogram is not None and (
                loaded is None or loaded.spec.data_hash != planogram_hash(cmd.planogram)
            )
            if loaded is None or stale:
                robots = list(cmd.robots) if cmd.robots else None
                if robots is not None and cmd.robot_id not in {r.robot_id for r in robots}:
                    robots.append(spec)
                self.load_store(
                    spec.store_id,
                    planogram=cmd.planogram,
                    products=cmd.products,
                    robots=robots,
                    cameras=cmd.cameras,
                    seed=cmd.seed,
                )
                rover = self.rovers.get(cmd.robot_id)
                if rover is None:  # the store's robot list did not include it: add it
                    rover = self._spawn(spec)
            elif rover is None:
                rover = self._spawn(spec)
            else:
                rover = self._respawn(rover, given.get(cmd.robot_id))
            self.publish_state(rover, force=True)
        else:
            sid = cmd.store_id or (cmd.planogram.store_id if cmd.planogram else None)
            if sid is None and len(self.stores) == 1:
                sid = next(iter(self.stores))
            sid = sid or self.default_store
            if sid is None:
                raise _Fail("internal", "store-level reset needs store_id")
            self.load_store(
                sid,
                planogram=cmd.planogram,
                products=cmd.products,
                robots=cmd.robots,
                cameras=cmd.cameras,
                seed=cmd.seed,
            )
            for cid in [c for c in self.streams if c not in {ci.camera_id for ci in self.camera_infos()}]:
                self.streams.pop(cid, None)
        if cmd.robot_id is None or self.hosting_key() != before:
            self.emit(self.hosted())
        self._reply(cmd, rover)

    def _set_slot_stock(self, cmd: Any) -> None:
        scene = self.stores.get(cmd.store_id)
        if scene is None:
            raise _Fail("not_ready", f"store {cmd.store_id!r} is not hosted here")
        slot = scene.slot(cmd.slot_id)
        if slot is None:
            raise _Fail("unknown_slot", f"unknown slot {cmd.slot_id!r} in {cmd.store_id}")
        layout = scene.spec.slot_layouts.get(cmd.slot_id)
        product = scene.products.get(cmd.sku) or scene.products.get(slot.sku)
        if layout is None and product is not None:
            fx = scene.planogram.fixture(slot.fixture_id)
            if fx is None:
                raise _Fail("unknown_slot", f"slot {cmd.slot_id} has no fixture")
            # same dims / model / width as the build (restocks match)
            layout = layout_for(slot, fx, product, self.assets, slot_lateral_room(scene.planogram, slot, fx))
            scene.spec.slot_layouts[cmd.slot_id] = layout
            if product.sku not in scene.spec.products:
                scene.spec.products[product.sku] = product
        if layout is None:
            raise _Fail("internal", f"unknown sku {cmd.sku!r} for slot {cmd.slot_id}")
        held = self.held_ids(scene.store_id)
        on_shelf = sorted(
            (
                i
                for i in scene.instances.values()
                if i.slot_id == cmd.slot_id and not i.in_basket and i.instance_id not in held
            ),
            key=lambda i: (instance_index(i.instance_id), i.instance_id),
        )
        # remove the highest n first (back rows)
        while len(on_shelf) > cmd.qty:
            gone = on_shelf.pop()
            scene.instances.pop(gone.instance_id, None)
            self.backend.remove_instance(scene.store_id, gone.instance_id)
        # spawn the lowest free n
        n = 0
        while len(on_shelf) < cmd.qty:
            iid = instance_id_for(cmd.slot_id, n)
            n += 1
            if iid in scene.instances:
                continue
            on_shelf.append(unit_instance(scene.store_id, layout, iid, 0, None, None))
        on_shelf.sort(key=lambda i: (instance_index(i.instance_id), i.instance_id))
        lots = unit_lots(cmd.lots, len(on_shelf)) if cmd.lots else None
        result: list[ItemInstance] = []
        for rank, inst in enumerate(on_shelf):
            iid = inst.instance_id
            if lots is not None:
                lot_id, lot_fresh = lots[rank]
                fresh = unit_freshness(lot_fresh, iid)
            else:
                lot_id, fresh = inst.lot_id, inst.freshness
            new = unit_instance(scene.store_id, layout, iid, rank, lot_id, fresh)
            existed = iid in scene.instances
            scene.instances[iid] = new
            scene.spec.item_dims[iid] = layout.dims
            scene.spec.item_yaw[iid] = layout.yaw
            if existed:
                self.backend.set_instance_pose(scene.store_id, iid, new.position, layout.yaw)
            else:
                self.backend.add_instance(unit_prim(scene.store_id, layout, iid, rank, lot_id, fresh))
            result.append(new)
        self.emit(InstanceInfo(store_id=scene.store_id, slot_id=cmd.slot_id, instances=result))
        self._reply(cmd, None, instances=result)

    # ---- loop ------------------------------------------------------------------------------------------------------
    def tick(self, dt: float) -> None:
        """Advance every robot by ``dt`` seconds (call once per main-loop iteration)."""
        self.sim_time += dt
        if self.leftovers:
            self._tick_leftovers(dt)
        for rover in list(self.rovers.values()):
            if rover.active is None:
                if rover.on_dock and rover.error is None:
                    rover.battery = min(100.0, rover.battery + BATTERY_CHARGE_PCT_PER_MIN * dt / 60.0)
                else:
                    rover.battery = max(0.0, rover.battery - BATTERY_IDLE_DRAIN_PCT_PER_MIN * dt / 60.0)
            else:
                cmd = rover.active.cmd
                try:
                    res = rover.active.step(dt)
                    if res is not None:
                        self._finish(rover, res)
                except _Fail as exc:
                    self._fail(cmd, exc.code, exc.message, rover=rover)
                except Exception as exc:  # noqa: BLE001
                    self._fail(cmd, "internal", f"{type(exc).__name__}: {exc}", rover=rover)
            if rover.robot_id in self.rovers:
                self.publish_state(rover)

    def on_connected(self) -> None:
        """Gateway accepted us: default streams, one RobotState per robot, ground truth per store."""
        self.default_streams()
        for rover in self.rovers.values():
            self.publish_state(rover, force=True)
        for sid, scene in self.stores.items():
            self.emit(InstanceInfo(store_id=sid, instances=list(scene.instances.values())))

    def on_disconnected(self, reason: str = "disconnected") -> None:
        """Gateway link dropped: abort in-flight actions silently (their results would be dropped); keep the scenes."""
        for rover in self.rovers.values():
            rover.active = None
        self.streams = {}
