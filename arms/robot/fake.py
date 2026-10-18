"""FakeRobot: deterministic MULTI-ROBOT 2D kinematic sim implementing ``contracts.services.RobotService``.

The always-available fallback when the Isaac worker is down (and the default robot in ``make demo``).
- Fleet: every robot in data/fixtures/robots.yaml (``utils.data``; built-in Pico / Rhea / Hazel while the file is
  missing), each in its own store, spawned on its dock. Robots in different stores AND in the same store move
  concurrently; one motion at a time PER robot (``busy``); ``stop`` cancels that robot's motion (``cancelled``).
- Motion: SO-101 rover (contracts.common): pure pursuit at ``RobotSpec.max_speed_mps`` in SIMULATED time
  (``realtime_factor`` sim-s per wall-s; ``math.inf`` = no sleeping, for tests); rovers in one store yield to a
  moving rover with a smaller id (``path_blocked`` after 10 sim-s); lift to the item height to pick
  (``slot_reachable`` reach check -> ``grasp_failed``), place into the basket (``basket_capacity``), unload at
  checkout. Battery: per meter + idle drain, charging while idle on the dock (status CHARGING); below
  ``BATTERY_MOTION_MIN_PCT`` motion is refused (``battery_low``) except driving to the dock.
- Scene: one ``StoreWorld`` per store (live planogram via ``services.catalog`` at call time, else ``utils.data``),
  deterministic instances (``instance_id(slot_id, n)``, store_id set); ``sync_stock`` respawns / removes units to
  match the catalog after restocks and write-offs.
- Cameras: Pillow frames (``arms.robot.render``) for ``<robot>.front|wrist|basket`` and ``<store>.ceiling-N``: RAW
  (``blurred=False``; CameraService blurs), synthetic watermark + JPEG comment, ground-truth ``instances`` with pixel
  boxes on scans / captures. ``frames(camera_id)`` makes that camera stream (``stream_fps``, rendered on demand in a
  worker thread; stream frames carry ``instances=[]``).
- States stream through ``states()``; the gateway republishes them as ``robot.state``. ``publish_state_events=True``
  also publishes ``robot.state`` itself (standalone runs only: in the pipeline the gateway is the producer).
"""

import asyncio
import base64
import contextlib
import math
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from arms.robot._fanout import Fanout
from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig
from arms.robot.instances import _n_of, basket_position, layout_bboxes
from arms.robot.placeholder import PLACEHOLDER_H, PLACEHOLDER_JPEG_B64, PLACEHOLDER_W
from arms.robot.render import (
    Rendered,
    RoverView,
    SceneView,
    nearest_facing_slot,
    render_basket,
    render_ceiling,
    render_director,
    render_front,
    render_wrist,
)
from arms.robot.rover import (
    LIFT_STOW_M,
    Battery,
    fallback_ceiling_camera,
    heading_to_dock,
    lift_for,
    load_specs,
    motion_duration,
    on_dock,
)
from arms.robot.world import StoreWorld, live_planogram, load_scene
from contracts.cameras import (
    DEFAULT_ROBOT_CAMERA_MOUNTS,
    CameraMount,
    director_camera_id,
    parse_camera_id,
    robot_camera_id,
)
from contracts.common import (
    ARM_MOUNT_X_M,
    ROBOT_RADIUS_M,
    CameraId,
    InstanceId,
    Point3,
    Pose2D,
    RobotId,
    Sku,
    SlotId,
    StoreId,
    TripId,
    normalize_camera,
    slot_reachable,
    utcnow,
)
from contracts.events import make_event
from contracts.planogram import RobotSpec, Slot, StoreCameraSpec
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

DEFAULT_REALTIME_FACTOR = 5.0
"""Sim seconds per wall second for the demo (a 10 m leg at 0.8 m/s takes ~2.5 s on screen)."""
DEFAULT_SPEED_MPS: float | None = None
"""None = each robot's ``RobotSpec.max_speed_mps`` (0.8 m/s)."""
DEFAULT_STREAM_FPS = 4.0
BLOCKED_LIMIT_S = 10.0
"""A rover waiting on another moving rover for longer than this fails with ``path_blocked``."""
SCAN_RANGE_M = 1.0
FRAME_ID_PREFIX = "fakeframe"
"""Frame ids 'fakeframe_xxxxxxxx': the 'fake' prefix marks a rendered frame (no real people can be in it), which is how
perception (``is_synthetic_frame``) certifies stream frames that carry no ground-truth instances."""

DURATIONS_S = {"reset": 0.5, "scan": 0.8, "pick": 2.2, "place_in_basket": 1.5, "unload_basket": 1.0}
BASKET_REACH_LIFT_M = 0.32
"""0.2.4 checkout unload: lift height at which the SO-101 reaches back into the basket (matches the Isaac worker)."""
COUNTER_REACH_M = 0.6
"""place_on_counter: the spot must be within this horizontal distance of the robot base (else grasp_failed)."""
COUNTER_CLEAR_M = 0.05
"""A unit already lying within this of a new counter spot (left from an earlier trip) is cleared first."""
"""Base simulated durations (lift travel is added to pick / place; see arms.robot.rover)."""


@dataclass
class _Rover:
    spec: RobotSpec
    pose: Pose2D
    battery: Battery
    status: RobotStatus = RobotStatus.IDLE
    lift: float = LIFT_STOW_M
    holding: ItemInstance | None = None
    basket: list[ItemInstance] = field(default_factory=list)
    counter: list[ItemInstance] = field(default_factory=list)
    """0.2.4: units this robot unloaded onto the checkout counter (cleared by unload_basket / reset)."""
    motion: str | None = None
    cmd_id: str | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    route: list[Pose2D] = field(default_factory=list)
    task: str | None = None
    auto_task: str | None = None
    focus_slot: SlotId | None = None
    sim_time_s: float = 0.0
    last_state_push: float = -math.inf
    last_event_push: float = -math.inf

    @property
    def robot_id(self) -> RobotId:
        return self.spec.robot_id

    @property
    def store_id(self) -> StoreId:
        return self.spec.store_id


class FakeRobot:
    """Multi-robot 2D kinematic RobotService. Construct via ``create_fake(services)``; nothing loads until a call."""

    def __init__(
        self,
        services: Services | None = None,
        *,
        realtime_factor: float = DEFAULT_REALTIME_FACTOR,
        speed_mps: float | None = DEFAULT_SPEED_MPS,
        tick_s: float = 0.05,
        publish_state_events: bool = False,
        state_hz: float = 10.0,
        stream_fps: float = DEFAULT_STREAM_FPS,
        robots: list[RobotSpec] | None = None,
        store_cameras: list[StoreCameraSpec] | None = None,
        strict_reach: bool = True,
        render_in_thread: bool = True,
        live_frame_period_s: float | None = None,  # v0.1 option, ignored (streams are per camera now)
    ) -> None:
        self.services = services or Services()
        self.realtime_factor = realtime_factor
        self.speed_mps = speed_mps
        self.tick_s = tick_s
        self.publish_state_events = publish_state_events
        self.state_period_s = 1.0 / state_hz if state_hz > 0 else 0.0
        self.stream_fps = max(0.2, min(15.0, stream_fps))
        self.strict_reach = strict_reach
        self.render_in_thread = render_in_thread
        self.trip_id: TripId | None = None

        self._robot_specs = list(robots) if robots is not None else None
        self._camera_specs = list(store_cameras) if store_cameras is not None else None
        self._rovers: dict[RobotId, _Rover] | None = None
        self._store_cams: dict[CameraId, StoreCameraSpec] = {}
        self._worlds: dict[StoreId, StoreWorld] = {}
        self._world_locks: dict[StoreId, asyncio.Lock] = {}
        self._ids = IdFactory("robot-fake")
        self._t0 = time.monotonic()
        self._virtual_s = 0.0
        self._state_fanout: Fanout[RobotState] = Fanout(maxsize=256)
        self._frame_fanout: Fanout[CameraFrame] = Fanout(maxsize=8)
        self._demand: Counter[CameraId] = Counter()
        self._streamer: asyncio.Task[None] | None = None
        self._paused_stores: set[StoreId] = set()
        self._state_listeners: list[Callable[[RobotState], None]] = []
        self._frame_listeners: list[Callable[[CameraFrame], None]] = []

    # =================================================================================================================
    # public helpers (not in the Protocol)
    # =================================================================================================================
    def set_trip(self, trip_id: TripId | None) -> None:
        """Trip id stamped on robot.state events this fake publishes itself (publish_state_events=True only)."""
        self.trip_id = trip_id

    def set_task(self, robot_id: RobotId, text: str | None) -> None:
        """Text shown in the robot's camera HUD (e.g. the goal it works on); None = show the current motion."""
        self._rover(robot_id).task = text

    def snapshot(self, robot_id: RobotId) -> RobotState:
        """Synchronous ``state()``."""
        return self._snapshot(self._rover(robot_id))

    def robot_ids(self, store_id: StoreId | None = None) -> list[RobotId]:
        return [r.robot_id for r in self._fleet().values() if store_id is None or r.store_id == store_id]

    def spec(self, robot_id: RobotId) -> RobotSpec:
        return self._rover(robot_id).spec

    def specs(self) -> list[RobotSpec]:
        return [r.spec for r in self._fleet().values()]

    def store_ids(self) -> list[StoreId]:
        return sorted({r.store_id for r in self._fleet().values()})

    def store_camera_specs(self, store_id: StoreId | None = None) -> list[StoreCameraSpec]:
        self._fleet()
        return [c for c in self._store_cams.values() if store_id is None or c.store_id == store_id]

    def camera_ids(self, store_id: StoreId | None = None, robot_id: RobotId | None = None) -> list[CameraId]:
        out: list[CameraId] = []
        for r in self._fleet().values():
            if (store_id is None or r.store_id == store_id) and (robot_id is None or r.robot_id == robot_id):
                out += [robot_camera_id(r.robot_id, k) for k in r.spec.cameras]
        if robot_id is None:
            out += [c.camera_id for c in self.store_camera_specs(store_id)]
        return out

    def camera_store(self, camera_id: CameraId) -> StoreId | None:
        try:
            owner, kind = parse_camera_id(camera_id)
        except ValueError:
            return None
        if kind == "ceiling":
            return owner
        rv = self._fleet().get(owner)
        return rv.store_id if rv else None

    async def world(self, store_id: StoreId) -> StoreWorld:
        """The store's ground truth (loaded on first use)."""
        return await self._world(store_id)

    def loaded_store_ids(self) -> list[StoreId]:
        return sorted(self._worlds)

    def store_idle(self, store_id: StoreId) -> bool:
        """Every robot of the store idle with an empty gripper and basket (safe to hand the store to Isaac)."""
        return all(
            r.motion is None and r.holding is None and not r.basket
            for r in self._fleet().values()
            if r.store_id == store_id
        )

    def pause_store(self, store_id: StoreId, paused: bool = True) -> None:
        """Stop/resume streaming this store's cameras (the real impl pauses stores Isaac drives)."""
        if paused:
            self._paused_stores.add(store_id)
        else:
            self._paused_stores.discard(store_id)

    def add_listener(
        self,
        on_state: Callable[[RobotState], None] | None = None,
        on_frame: Callable[[CameraFrame], None] | None = None,
    ) -> None:
        """Synchronous callbacks for every pushed state / live frame (the real impl merges them into its streams)."""
        if on_state is not None:
            self._state_listeners.append(on_state)
        if on_frame is not None:
            self._frame_listeners.append(on_frame)

    def add_stream_demand(self, camera_ids: list[CameraId]) -> None:
        for cid in camera_ids:
            self._demand[cid] += 1
        self._ensure_streamer()

    def remove_stream_demand(self, camera_ids: list[CameraId]) -> None:
        for cid in camera_ids:
            self._demand[cid] -= 1
            if self._demand[cid] <= 0:
                del self._demand[cid]

    @property
    def sim_time_s(self) -> float:
        """Total simulated motion time across robots (tests)."""
        return sum(r.sim_time_s for r in self._fleet().values())

    def robot_sim_time_s(self, robot_id: RobotId) -> float:
        return self._rover(robot_id).sim_time_s

    async def reset_store(self, store_id: StoreId) -> list[RobotState]:
        """Rebuild the store scene from the catalog and respawn every robot of the store at its dock."""
        plan, products = await load_scene(self.services, store_id)
        async with self._lock(store_id):
            if store_id in self._worlds:
                w = self._worlds[store_id]
                w.plan, w.products = plan, products
                w.rebuild()
            else:
                self._worlds[store_id] = StoreWorld.build(plan, products)
        out = []
        for rv in [r for r in self._fleet().values() if r.store_id == store_id]:
            if rv.motion is not None:
                await self.stop(rv.robot_id)
            self._respawn(rv)
            await self._emit(rv, force=True)
            out.append(self._snapshot(rv))
        return out

    async def adopt_state(self, state: RobotState, known: dict[InstanceId, ItemInstance] | None = None) -> RobotState:
        """Continue a robot on the fake from an Isaac ``RobotState`` (pose, battery, lift, holding, basket); units in
        its gripper / basket are taken off the fake shelves (or rebuilt from ``known``). No reset."""
        if not state.robot_id:
            raise RobotError("unknown_robot", "state without robot_id")
        rv = self._rover(state.robot_id)
        world = await self._world(rv.store_id)
        known = known or {}

        def claim(iid: InstanceId, sku: Sku | None) -> ItemInstance | None:
            slot_id = iid.rsplit("#", 1)[0]
            unit = world.take(slot_id, iid) or known.get(iid)
            if unit is None and sku:
                unit = ItemInstance(
                    instance_id=iid, sku=sku, store_id=rv.store_id, slot_id=slot_id, position=Point3(x=0, y=0)
                )
            return unit.model_copy(update={"store_id": rv.store_id}) if unit else None

        rv.pose = state.pose.model_copy()
        if state.battery_pct is not None:
            rv.battery.pct = float(state.battery_pct)
        rv.lift = state.lift_height_m if state.lift_height_m is not None else LIFT_STOW_M
        skus = list(state.basket_skus) + [None] * max(0, len(state.basket) - len(state.basket_skus))
        rv.basket = [
            u.model_copy(update={"in_basket": True})
            for u in (claim(i, s) for i, s in zip(state.basket, skus, strict=False))
            if u is not None
        ]
        rv.holding = claim(state.holding, None) if state.holding else None
        rv.route, rv.error = [], None
        rv.status = RobotStatus.IDLE
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    # =================================================================================================================
    # RobotService
    # =================================================================================================================
    async def list_robots(self) -> list[RobotState]:
        return [self._snapshot(rv) for rv in self._fleet().values()]

    async def state(self, robot_id: RobotId) -> RobotState:
        return self._snapshot(self._rover(robot_id))

    async def reset(self, robot_id: RobotId, store_id: StoreId | None = None) -> RobotState:
        rv = self._rover(robot_id)
        if store_id is not None and store_id != rv.store_id:
            raise RobotError("unknown_robot", f"{robot_id} works in {rv.store_id}, not {store_id}")
        await self._world(rv.store_id)
        with self._motion(rv, "reset", RobotStatus.IDLE):
            await self._wait(rv, DURATIONS_S["reset"])
            self._respawn(rv)
            log.info("fake robot %s reset to its dock in %s", robot_id, rv.store_id)
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    async def navigate(self, robot_id: RobotId, waypoints: list[Pose2D]) -> RobotState:
        rv = self._rover(robot_id)
        if not waypoints:
            raise RobotError("internal", "navigate needs at least one waypoint")
        world = await self._world(rv.store_id)
        self._settle(rv)
        to_dock = heading_to_dock(waypoints, rv.spec)
        rv.battery.check_motion(robot_id, to_dock)
        with self._motion(rv, "navigate", RobotStatus.NAVIGATING):
            goal = waypoints[-1]
            rv.route = [w.model_copy() for w in waypoints]
            rv.auto_task = "returning to dock" if to_dock else f"driving to ({goal.x:.1f}, {goal.y:.1f})"
            await self._emit(rv, force=True)
            path = [rv.pose, *waypoints]
            self._check_path(world, path)
            speed = self.speed_mps or rv.spec.max_speed_mps
            pp = PurePursuit(path, PurePursuitConfig(max_speed_mps=speed))
            budget = 3.0 * pp.length / max(speed, 1e-3) + 20.0
            t, blocked = 0.0, 0.0
            while not pp.done:
                if t > budget:
                    raise RobotError("timeout", f"navigation did not converge within {budget:.0f} sim-s")
                x, y, th = pp.step(rv.pose, self.tick_s)
                if self._must_yield(rv, x, y):
                    blocked += self.tick_s
                    if blocked > BLOCKED_LIMIT_S:
                        raise RobotError("path_blocked", f"{robot_id} blocked by another rover for {blocked:.0f} s")
                    await self._wait(rv, self.tick_s, emit=True)
                    t += self.tick_s
                    continue
                blocked = 0.0
                rv.battery.drive(math.hypot(x - rv.pose.x, y - rv.pose.y))
                if rv.battery.pct <= 0.0 and not to_dock:
                    raise RobotError("battery_low", f"{robot_id} battery empty")
                rv.pose = Pose2D(x=x, y=y, theta=th)
                while len(rv.route) > 1 and rv.pose.distance_to(rv.route[0]) < 0.3:
                    rv.route.pop(0)
                await self._wait(rv, self.tick_s, emit=True)
                t += self.tick_s
            rv.pose = Pose2D(x=goal.x, y=goal.y, theta=goal.theta)
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    async def stop(self, robot_id: RobotId) -> RobotState:
        rv = self._rover(robot_id)
        if rv.motion is not None:
            rv.cancel.set()
            for _ in range(200):  # let the motion coroutine observe the cancel and unwind
                if rv.motion is None:
                    break
                await asyncio.sleep(0 if math.isinf(self.realtime_factor) else 0.005)
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    async def scan(self, robot_id: RobotId, slot_id: SlotId) -> CameraFrame:
        rv = self._rover(robot_id)
        world = await self._world(rv.store_id)
        slot = world.slot(slot_id)
        with self._motion(rv, "scan", RobotStatus.SCANNING):
            rv.focus_slot = slot_id
            rv.auto_task = f"scanning {slot_id}"
            await self._emit(rv, force=True)
            await self._wait(rv, DURATIONS_S["scan"])
            frame = await self._render_robot_frame(rv, "front", focus=slot)
        self._push_frame(frame)
        await self._emit(rv, force=True)
        return frame

    async def capture(self, robot_id: RobotId, camera: str = "front") -> CameraFrame:
        rv = self._rover(robot_id)
        kind = normalize_camera(camera)
        if kind == "director":
            frame = await self._render_director(rv)
            self._push_frame(frame)
            return frame
        if kind == "ceiling":
            cams = self.store_camera_specs(rv.store_id)
            return await self.capture_camera(cams[0].camera_id if cams else f"{rv.store_id}.ceiling-1")
        if kind not in rv.spec.cameras:
            raise RobotError("unknown_robot", f"{robot_id} has no {camera!r} camera")
        frame = await self._render_robot_frame(rv, kind)
        self._push_frame(frame)
        return frame

    async def capture_camera(self, camera_id: CameraId) -> CameraFrame:
        frame = await self._capture_by_id(camera_id)
        self._push_frame(frame)
        return frame

    async def list_instances(self, robot_id: RobotId, slot_id: SlotId | None = None) -> list[ItemInstance]:
        rv = self._rover(robot_id)
        world = await self._world(rv.store_id)
        if slot_id is not None:
            world.slot(slot_id)
            return [i.model_copy(update={"bbox": None}) for i in world.units(slot_id)]
        out = [i.model_copy(update={"bbox": None}) for units in world.shelves.values() for i in units]
        if rv.holding is not None:
            out.append(self._held_instance(rv))
        return out + self._basket_instances(rv) + [i.model_copy(update={"bbox": None}) for i in rv.counter]

    async def pick(
        self, robot_id: RobotId, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None
    ) -> ItemInstance:
        return await self._pick(robot_id, slot_id, sku, instance_id, strict_reach=self.strict_reach)

    # ---- 0.2.4 checkout unload by the arm (optional RobotService extension; arms/robot/vla.py) -----------------------
    def can_unload_to_counter(self, robot_id: RobotId) -> bool:
        """The 2D fake always has the basket -> counter primitives."""
        self._rover(robot_id)
        return True

    async def pick_from_basket(
        self, robot_id: RobotId, sku: Sku | None = None, instance_id: InstanceId | None = None
    ) -> ItemInstance:
        """Lower the lift, reach back into the basket and lift one unit out (``instance_id``, else the first unit of
        ``sku``). ``no_instance`` when it is not in the basket; ``grasp_failed`` when the gripper is busy."""
        rv = self._rover(robot_id)
        if rv.holding is not None:
            raise RobotError("grasp_failed", f"gripper already holds {rv.holding.instance_id}")
        unit = next(
            (
                u
                for u in rv.basket
                if (instance_id is not None and u.instance_id == instance_id)
                or (instance_id is None and sku is not None and u.sku == sku)
            ),
            None,
        )
        if unit is None:
            raise RobotError("no_instance", f"no {instance_id or sku} in the basket of {robot_id}")
        self._settle(rv)
        rv.battery.check_motion(robot_id)
        with self._motion(rv, "pick_from_basket", RobotStatus.PICKING):
            rv.auto_task = f"taking {unit.sku} out of the basket"
            dur = motion_duration("pick_from_basket", rv.lift, BASKET_REACH_LIFT_M)
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.5)
            rv.lift = BASKET_REACH_LIFT_M
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.5)
            rv.basket = [u for u in rv.basket if u.instance_id != unit.instance_id]
            rv.holding = unit.model_copy(update={"in_basket": False, "on_counter": False, "store_id": rv.store_id})
        await self._emit(rv, force=True)
        return self._held_instance(rv)

    async def place_on_counter(
        self, robot_id: RobotId, position: Point3, yaw: float = 0.0, spot: int | None = None
    ) -> RobotState:
        """Carry the held unit to ``position`` on the checkout counter and release it there (``on_counter``)."""
        rv = self._rover(robot_id)
        if rv.holding is None:
            raise RobotError("no_instance", "gripper is empty")
        dist = math.hypot(position.x - rv.pose.x, position.y - rv.pose.y)
        if self.strict_reach and dist > COUNTER_REACH_M:
            raise RobotError("grasp_failed", f"counter spot {spot} out of reach ({dist:.2f} m from the robot)")
        with self._motion(rv, "place_on_counter", RobotStatus.PLACING):
            rv.auto_task = f"putting {rv.holding.sku} on the counter"
            lift_to = lift_for(position.z)
            dur = motion_duration("place_on_counter", rv.lift, lift_to)
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.6)
            rv.lift = lift_to
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.4)
            held, rv.holding = rv.holding, None
            rv.counter = [
                u
                for u in rv.counter
                if math.hypot(u.position.x - position.x, u.position.y - position.y) > COUNTER_CLEAR_M
            ]
            dims_z = 0.12
            rv.counter.append(
                held.model_copy(
                    update={
                        "position": Point3(x=position.x, y=position.y, z=round(position.z + dims_z / 2, 4)),
                        "in_basket": False,
                        "on_counter": True,
                        "store_id": rv.store_id,
                        "bbox": None,
                    }
                )
            )
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    def counter_units(self, robot_id: RobotId) -> list[ItemInstance]:
        """Units this robot left on the checkout counter (ground truth for the verifier)."""
        return [u.model_copy() for u in self._rover(robot_id).counter]

    # ---- extras for the VLA layer (arms/robot/vla.py; not in the RobotService Protocol) ------------------------------
    def can_put_back(self, robot_id: RobotId) -> bool:
        """The 2D fake can always return a held unit to its own slot (``put_back``)."""
        self._rover(robot_id)
        return True

    async def grab_unit(
        self, robot_id: RobotId, slot_id: SlotId, instance_id: InstanceId | None = None
    ) -> ItemInstance:
        """Fault injection ('wrong_item'): really grasp a unit of ``slot_id`` (the neighbouring SKU) WITHOUT the reach
        check, so the held unit, the wrist frame and the ground truth all show the wrong product."""
        rv = self._rover(robot_id)
        world = await self._world(rv.store_id)
        return await self._pick(robot_id, slot_id, world.slot(slot_id).sku, instance_id, strict_reach=False)

    async def put_back(self, robot_id: RobotId, slot_id: SlotId | None = None) -> RobotState:
        """Put the held unit back on ITS OWN slot (``ItemInstance.slot_id``; ``slot_id`` is only a hint). Magic-grasp
        assisted like ``pick`` but without the reach check, so a wrong unit grabbed from the neighbouring slot can be
        returned from where the robot stands. ``no_instance`` when the gripper is empty."""
        rv = self._rover(robot_id)
        if rv.holding is None:
            raise RobotError("no_instance", "gripper is empty")
        world = await self._world(rv.store_id)
        home = rv.holding.slot_id or slot_id
        slot = world.slot(home)
        if slot_id is not None and slot_id != home:
            log.info("put_back on %s: returning %s to its own slot %s", slot_id, rv.holding.instance_id, home)
        self._settle(rv)
        with self._motion(rv, "put_back", RobotStatus.PLACING):
            rv.focus_slot = slot.slot_id
            rv.auto_task = f"putting {rv.holding.sku} back"
            lift_to = lift_for(slot.item_position.z)
            await self._emit(rv, force=True)
            await self._wait(rv, motion_duration("place_in_basket", rv.lift, lift_to))
            held, rv.holding = rv.holding, None
            units = world.units(slot.slot_id)
            units.append(held.model_copy(update={"in_basket": False, "store_id": rv.store_id, "bbox": None}))
            units.sort(key=_n_of)
            rv.lift = lift_to
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    async def _pick(
        self, robot_id: RobotId, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None, *, strict_reach: bool
    ) -> ItemInstance:
        rv = self._rover(robot_id)
        world = await self._world(rv.store_id)
        slot = world.slot(slot_id)
        if slot.sku != sku:
            raise RobotError("no_instance", f"slot {slot_id} holds {slot.sku}, not {sku}")
        if rv.holding is not None:
            raise RobotError("grasp_failed", f"gripper already holds {rv.holding.instance_id}")
        target = self._target(world, slot_id, sku, instance_id)
        if not slot_reachable(rv.pose, slot.approach_pose, slot.item_position):
            dist = rv.pose.distance_to(slot.approach_pose)
            if strict_reach:
                raise RobotError(
                    "grasp_failed",
                    f"{slot_id} out of reach ({dist:.2f} m from its approach pose, z={slot.item_position.z})",
                )
            log.warning("fake pick on %s from %.2f m away (strict_reach=False)", slot_id, dist)
        self._settle(rv)
        rv.battery.check_motion(robot_id)
        with self._motion(rv, "pick", RobotStatus.PICKING):
            rv.focus_slot = slot_id
            label = world.product(sku).name if world.product(sku) else sku
            rv.auto_task = f"picking {label}"
            lift_to = lift_for(slot.item_position.z)
            dur = motion_duration("pick", rv.lift, lift_to)
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.4)
            rv.lift = lift_to
            await self._emit(rv, force=True)
            await self._wait(rv, dur * 0.6)
            unit = world.take(slot_id, target.instance_id)
            if unit is None:  # another robot took it meanwhile
                raise RobotError("no_instance", f"{target.instance_id} is gone from {slot_id}")
            rv.holding = unit.model_copy(update={"store_id": rv.store_id})
        await self._emit(rv, force=True)
        return self._held_instance(rv)

    async def place_in_basket(self, robot_id: RobotId) -> RobotState:
        rv = self._rover(robot_id)
        if rv.holding is None:
            raise RobotError("no_instance", "gripper is empty")
        if len(rv.basket) >= rv.spec.basket_capacity:
            raise RobotError("grasp_failed", f"basket full ({len(rv.basket)}/{rv.spec.basket_capacity})")
        with self._motion(rv, "place_in_basket", RobotStatus.PLACING):
            rv.auto_task = "placing in basket"
            dur = motion_duration("place_in_basket", rv.lift, LIFT_STOW_M)
            await self._emit(rv, force=True)
            await self._wait(rv, dur)
            held, rv.holding = rv.holding, None
            rv.basket.append(held.model_copy(update={"in_basket": True, "slot_id": held.slot_id}))
            rv.lift = LIFT_STOW_M
        await self._emit(rv, force=True)
        return self._snapshot(rv)

    async def unload_basket(self, robot_id: RobotId) -> list[ItemInstance]:
        rv = self._rover(robot_id)
        with self._motion(rv, "unload_basket", RobotStatus.PLACING):
            rv.auto_task = f"unloading {len(rv.basket)} items at checkout"
            await self._emit(rv, force=True)
            await self._wait(rv, motion_duration("unload_basket", units=len(rv.basket)))
            out = self._basket_instances(rv) + [u.model_copy() for u in rv.counter]
            rv.basket = []
            rv.counter = []  # 0.2.4: the handoff = staff bag what the arm put on the counter + any basket leftovers
        await self._emit(rv, force=True)
        return out

    async def sync_stock(self, store_id: StoreId, slot_ids: list[SlotId] | None = None) -> None:
        if store_id not in self._worlds:
            return  # nothing simulated yet: the first load reads the live catalog anyway
        plan = await live_planogram(self.services, store_id)
        async with self._lock(store_id):
            changed = self._worlds[store_id].apply_planogram(plan, slot_ids)
        if changed:
            log.info(
                "fake shelves synced in %s: %s", store_id, ", ".join(changed[:8]) + ("…" if len(changed) > 8 else "")
            )

    def frames(self, camera_id: CameraId | None = None, robot_id: RobotId | None = None) -> AsyncIterator[CameraFrame]:
        demand = self._demand_for(camera_id, robot_id)
        self.add_stream_demand(demand)

        def match(f: CameraFrame) -> bool:
            return (camera_id is None or f.camera_id == camera_id) and (robot_id is None or f.robot_id == robot_id)

        return self._frame_fanout.stream(predicate=match, on_close=lambda: self.remove_stream_demand(demand))

    def states(self, robot_id: RobotId | None = None) -> AsyncIterator[RobotState]:
        fleet = self._fleet()
        if robot_id is not None and robot_id not in fleet:
            raise RobotError("unknown_robot", f"unknown robot {robot_id!r}")
        initial = [self._snapshot(rv) for rv in fleet.values() if robot_id is None or rv.robot_id == robot_id]
        return self._state_fanout.stream(
            initial=initial, predicate=None if robot_id is None else (lambda s: s.robot_id == robot_id)
        )

    # =================================================================================================================
    # internals: fleet, worlds, time
    # =================================================================================================================
    def _fleet(self) -> dict[RobotId, _Rover]:
        if self._rovers is None:
            if self._robot_specs is None:
                specs, cams = load_specs()
            else:
                specs, cams = self._robot_specs, []
            if self._camera_specs is not None:
                cams = self._camera_specs
            self._rovers = {
                s.robot_id: _Rover(
                    spec=s, pose=s.dock.model_copy(), battery=Battery(pct=s.battery_pct, last_t=self._now())
                )
                for s in specs
            }
            self._store_cams = {c.camera_id: c for c in cams}
        return self._rovers

    def _rover(self, robot_id: RobotId | None) -> _Rover:
        rv = self._fleet().get(robot_id or "")
        if rv is None:
            raise RobotError("unknown_robot", f"unknown robot {robot_id!r}")
        return rv

    def _lock(self, store_id: StoreId) -> asyncio.Lock:
        return self._world_locks.setdefault(store_id, asyncio.Lock())

    async def _world(self, store_id: StoreId) -> StoreWorld:
        w = self._worlds.get(store_id)
        if w is not None:
            return w
        async with self._lock(store_id):
            w = self._worlds.get(store_id)
            if w is None:
                plan, products = await load_scene(self.services, store_id)
                w = StoreWorld.build(plan, products)
                self._worlds[store_id] = w
                if not any(c.store_id == store_id for c in self._store_cams.values()):
                    cam = fallback_ceiling_camera(plan)
                    self._store_cams[cam.camera_id] = cam
                log.info(
                    "fake scene for %s: %d slots, %d units",
                    store_id,
                    len(plan.slots),
                    sum(map(len, w.shelves.values())),
                )
        return w

    def _now(self) -> float:
        if math.isinf(self.realtime_factor) or self.realtime_factor <= 0:
            return self._virtual_s
        return (time.monotonic() - self._t0) * self.realtime_factor

    def _settle(self, rv: _Rover) -> None:
        rv.battery.settle(self._now(), charging=rv.motion is None and on_dock(rv.pose, rv.spec))

    def _respawn(self, rv: _Rover) -> None:
        rv.pose = rv.spec.dock.model_copy()
        rv.holding, rv.basket, rv.route = None, [], []
        rv.counter = []
        rv.battery = Battery(pct=rv.spec.battery_pct, last_t=self._now())
        rv.lift, rv.error, rv.focus_slot = LIFT_STOW_M, None, None

    @contextlib.contextmanager
    def _motion(self, rv: _Rover, name: str, status: RobotStatus) -> Iterator[None]:
        if rv.motion is not None:
            raise RobotError("busy", f"{rv.robot_id}: {rv.motion} in progress")
        self._settle(rv)
        rv.motion = name
        rv.cmd_id = self._ids("cmd")
        rv.cancel.clear()
        rv.status = status
        rv.error = None
        try:
            yield
        except RobotError as exc:
            rv.error = None if exc.code == "cancelled" else exc.message
            raise
        finally:
            self._settle(rv)
            rv.motion = None
            rv.cmd_id = None
            rv.route = []
            rv.auto_task = None
            rv.status = RobotStatus.IDLE

    async def _wait(self, rv: _Rover, sim_s: float, emit: bool = False) -> None:
        """Advance this robot's simulated time by ``sim_s`` (in ticks), sleeping ``sim_s / realtime_factor``."""
        remaining = sim_s
        instant = math.isinf(self.realtime_factor) or self.realtime_factor <= 0
        while remaining > 1e-9:
            if rv.cancel.is_set():
                raise RobotError("cancelled", f"{rv.robot_id}: {rv.motion} cancelled by stop")
            dt = min(self.tick_s, remaining)
            remaining -= dt
            rv.sim_time_s += dt
            if instant:
                self._virtual_s += dt
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(dt / self.realtime_factor)
            if emit:
                await self._emit(rv)
        if rv.cancel.is_set():
            raise RobotError("cancelled", f"{rv.robot_id}: {rv.motion} cancelled by stop")

    def _must_yield(self, rv: _Rover, x: float, y: float) -> bool:
        """Yield to a MOVING rover of the same store with a smaller id that we are closing in on (no deadlocks:
        priority by id; parked rovers are passed)."""
        for other in self._fleet().values():
            if other is rv or other.store_id != rv.store_id or other.motion != "navigate":
                continue
            if other.robot_id > rv.robot_id:
                continue
            d_next = math.hypot(other.pose.x - x, other.pose.y - y)
            if d_next < 2 * ROBOT_RADIUS_M and d_next < rv.pose.distance_to(other.pose) - 1e-6:
                return True
        return False

    def _check_path(self, world: StoreWorld, path: list[Pose2D]) -> None:
        plan = world.plan
        obstacles = [f.rect for f in plan.fixtures] + list(plan.obstacles)
        for a, b in zip(path, path[1:], strict=False):
            n = max(1, int(a.distance_to(b) / 0.1))
            for k in range(n + 1):
                x = a.x + (b.x - a.x) * k / n
                y = a.y + (b.y - a.y) * k / n
                if not plan.bounds.contains(x, y):
                    raise RobotError("path_blocked", f"path leaves the store at ({x:.2f}, {y:.2f})")
                for r in obstacles:
                    if r.contains(x, y):
                        raise RobotError("path_blocked", f"path crosses an obstacle at ({x:.2f}, {y:.2f})")

    def _target(self, world: StoreWorld, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None) -> ItemInstance:
        units = world.units(slot_id)
        target = (
            units[0]
            if (instance_id is None and units)
            else next((i for i in units if i.instance_id == instance_id), None)
        )
        if target is None:
            raise RobotError("no_instance", f"no {instance_id or sku} on slot {slot_id}")
        return target

    # =================================================================================================================
    # internals: state + frames
    # =================================================================================================================
    def _snapshot(self, rv: _Rover) -> RobotState:
        self._settle(rv)
        basket = self._basket_instances(rv)
        idle_on_dock = rv.motion is None and on_dock(rv.pose, rv.spec)
        status = RobotStatus.CHARGING if (rv.status == RobotStatus.IDLE and idle_on_dock) else rv.status
        return RobotState(
            robot_id=rv.robot_id,
            store_id=rv.store_id,
            pose=rv.pose.model_copy(),
            status=status,
            holding=rv.holding.instance_id if rv.holding else None,
            basket=[i.instance_id for i in basket],
            basket_skus=[i.sku for i in basket],
            lift_height_m=round(rv.lift, 3),
            battery_pct=rv.battery.display,
            charging=idle_on_dock and rv.battery.pct < 100.0,
            backend="fake",
            current_cmd_id=rv.cmd_id,
            error=rv.error,
            ts=utcnow(),
        )

    async def _emit(self, rv: _Rover, force: bool = False) -> None:
        now = rv.sim_time_s
        if force or now - rv.last_state_push >= self.state_period_s:
            rv.last_state_push = now
            snap = self._snapshot(rv)
            self._state_fanout.push(snap)
            for cb in list(self._state_listeners):
                try:
                    cb(snap)
                except Exception as exc:  # a listener must never kill a motion
                    log.warning("robot state listener failed: %s", exc)
        events = self.services.events
        if self.publish_state_events and events is not None and (force or now - rv.last_event_push >= 0.5):
            rv.last_event_push = now
            try:
                await events.publish(
                    make_event(
                        "robot.state",
                        trip_id=self.trip_id,
                        source="robot",
                        store_id=rv.store_id,
                        robot_id=rv.robot_id,
                        state=self._snapshot(rv),
                    )
                )
            except Exception as exc:  # never let the UI bus kill a motion
                log.warning("robot.state publish failed: %s", exc)

    def _push_frame(self, frame: CameraFrame) -> None:
        live = frame.model_copy(update={"instances": []})
        self._frame_fanout.push(live)
        for cb in list(self._frame_listeners):
            try:
                cb(live)
            except Exception as exc:
                log.warning("robot frame listener failed: %s", exc)

    def _held_instance(self, rv: _Rover) -> ItemInstance:
        assert rv.holding is not None
        c, s = math.cos(rv.pose.theta), math.sin(rv.pose.theta)
        reach = ARM_MOUNT_X_M + 0.25
        pos = Point3(x=round(rv.pose.x + reach * c, 4), y=round(rv.pose.y + reach * s, 4), z=round(rv.lift, 4))
        return rv.holding.model_copy(update={"position": pos, "store_id": rv.store_id, "bbox": None})

    def _basket_instances(self, rv: _Rover) -> list[ItemInstance]:
        return [
            i.model_copy(update={"position": basket_position(rv.pose, n), "in_basket": True, "store_id": rv.store_id})
            for n, i in enumerate(rv.basket)
        ]

    def _rover_view(self, rv: _Rover) -> RoverView:
        snap_status = self._snapshot(rv).status
        return RoverView(
            robot_id=rv.robot_id,
            name=rv.spec.name,
            color=rv.spec.color or "#2563eb",
            pose=rv.pose.model_copy(),
            status=snap_status.value,
            battery=rv.battery.display,
            lift_m=rv.lift,
            task=rv.task or rv.auto_task,
            route=[p.model_copy() for p in rv.route],
            dock=rv.spec.dock,
            holding=self._held_instance(rv) if rv.holding else None,
            basket=self._basket_instances(rv),
            capacity=rv.spec.basket_capacity,
        )

    def _scene_view(self, world: StoreWorld) -> SceneView:
        return SceneView(
            plan=world.plan,
            products=world.products,
            units={sid: list(us) for sid, us in world.shelves.items()},
            tags={s.slot_id: world.price_tag_cents(s) for s in world.plan.slots},
        )

    async def _run_render(self, fn: Callable[..., Rendered], *args: Any, **kw: Any) -> Rendered:
        if self.render_in_thread:
            return await asyncio.to_thread(fn, *args, **kw)
        return fn(*args, **kw)

    def _camera_pose(self, rv: _Rover, kind: str, focus: Slot | None, world: StoreWorld) -> CameraMount:
        mount = DEFAULT_ROBOT_CAMERA_MOUNTS.get(kind, CameraMount())
        p = rv.pose
        c, s = math.cos(p.theta), math.sin(p.theta)
        if kind == "wrist":
            reach = ARM_MOUNT_X_M + 0.25
            return mount.model_copy(
                update={"frame": "world", "x": p.x + reach * c, "y": p.y + reach * s, "z": rv.lift, "yaw": p.theta}
            )
        x, y = p.x + c * mount.x - s * mount.y, p.y + s * mount.x + c * mount.y
        yaw, pitch = p.theta + mount.yaw, mount.pitch
        if kind == "front" and focus is not None:  # pan-tilt head aimed at the slot (item center)
            h = world.asset(focus.sku).dims_m[2]
            tx, ty, tz = focus.item_position.x, focus.item_position.y, focus.item_position.z + h / 2
            yaw = math.atan2(ty - y, tx - x)
            pitch = math.atan2(tz - mount.z, math.hypot(tx - x, ty - y))
        return mount.model_copy(
            update={"frame": "world", "x": round(x, 4), "y": round(y, 4), "yaw": yaw, "pitch": pitch}
        )

    async def _render_robot_frame(self, rv: _Rover, kind: str, focus: Slot | None = None) -> CameraFrame:
        world = await self._world(rv.store_id)
        mount = DEFAULT_ROBOT_CAMERA_MOUNTS.get(kind, CameraMount())
        w, h = mount.width, mount.height
        scene, view = self._scene_view(world), self._rover_view(rv)
        try:
            if kind == "front":
                r = await self._run_render(render_front, scene, view, w=w, h=h, focus=focus)
            elif kind == "wrist":
                slot = None
                if rv.holding is None:
                    slot = world.plan.slot(rv.focus_slot) if rv.focus_slot else None
                    if slot is None or rv.pose.distance_to(slot.approach_pose) > SCAN_RANGE_M:
                        slot = nearest_facing_slot(world.plan, rv.pose)
                r = await self._run_render(render_wrist, scene, view, slot, w=w, h=h)
            else:
                r = await self._run_render(render_basket, scene, view, w=w, h=h)
        except Exception as exc:  # rendering must never fail a motion: constant placeholder
            log.warning("fake %s frame render failed (%s); using the placeholder", kind, exc)
            insts = list(world.units(focus.slot_id)) if focus else []
            boxes = layout_bboxes(insts, PLACEHOLDER_W, PLACEHOLDER_H, focus.facings if focus else 1)
            r = Rendered(
                base64.b64decode(PLACEHOLDER_JPEG_B64),
                PLACEHOLDER_W,
                PLACEHOLDER_H,
                [i.model_copy(update={"bbox": boxes.get(i.instance_id)}) for i in insts],
                focus.slot_id if focus else None,
            )
        return CameraFrame(
            frame_id=self._ids(FRAME_ID_PREFIX),
            camera=kind,  # type: ignore[arg-type]
            camera_id=robot_camera_id(rv.robot_id, kind),
            robot_id=rv.robot_id,
            store_id=rv.store_id,
            jpeg_b64=base64.b64encode(r.jpeg).decode("ascii"),
            w=r.w,
            h=r.h,
            blurred=False,  # RAW: CameraService blurs (there are no people in synthetic frames anyway)
            slot_id=r.slot_id,
            pose=rv.pose.model_copy(),
            camera_pose=self._camera_pose(rv, kind, focus, world),
            instances=[i.model_copy(update={"store_id": rv.store_id}) for i in r.instances],
            ts=utcnow(),
        )

    async def _render_director(self, rv: _Rover) -> CameraFrame:
        """``<robot>.director`` on the 2D fake: a close top-down follow view (render.render_director)."""
        world = await self._world(rv.store_id)
        views = [self._rover_view(r) for r in self._fleet().values() if r.store_id == rv.store_id]
        focus = self._rover_view(rv)
        w, h = 640, 480
        try:
            r = await self._run_render(render_director, world.plan, views, focus, w=w, h=h)
        except Exception as exc:
            log.warning("fake director frame render failed (%s); using the placeholder", exc)
            r = Rendered(base64.b64decode(PLACEHOLDER_JPEG_B64), PLACEHOLDER_W, PLACEHOLDER_H)
        return CameraFrame(
            frame_id=self._ids(FRAME_ID_PREFIX),
            camera="director",
            camera_id=director_camera_id(rv.robot_id),
            robot_id=rv.robot_id,
            store_id=rv.store_id,
            jpeg_b64=base64.b64encode(r.jpeg).decode("ascii"),
            w=r.w,
            h=r.h,
            blurred=False,
            pose=rv.pose.model_copy(),
            ts=utcnow(),
        )

    async def _capture_by_id(self, camera_id: CameraId, live: bool = False) -> CameraFrame:
        try:
            owner, kind = parse_camera_id(camera_id)
        except ValueError as exc:
            raise RobotError("unknown_robot", str(exc)) from exc
        if kind == "director":
            return await self._render_director(self._rover(owner))
        if kind == "perceived":
            raise RobotError("unknown_robot", f"{camera_id!r} is produced by the cameras service, not the robot")
        if kind != "ceiling":
            rv = self._rover(owner)
            if kind not in rv.spec.cameras:
                raise RobotError("unknown_robot", f"{owner} has no {kind!r} camera")
            focus = None
            if live and kind == "front" and rv.motion in ("scan", "pick") and rv.focus_slot:
                world = await self._world(rv.store_id)
                focus = world.plan.slot(rv.focus_slot)
            return await self._render_robot_frame(rv, kind, focus=focus)
        store_id = owner
        if store_id not in self.store_ids():
            raise RobotError("unknown_robot", f"unknown store camera {camera_id!r}")
        world = await self._world(store_id)
        cam = self._store_cams.get(camera_id)
        if cam is None:
            raise RobotError("unknown_robot", f"unknown store camera {camera_id!r}")
        views = [self._rover_view(rv) for rv in self._fleet().values() if rv.store_id == store_id]
        w, h = cam.mount.width, cam.mount.height
        try:
            r = await self._run_render(render_ceiling, world.plan, views, cam, w=w, h=h)
        except Exception as exc:
            log.warning("fake ceiling frame render failed (%s); using the placeholder", exc)
            r = Rendered(base64.b64decode(PLACEHOLDER_JPEG_B64), PLACEHOLDER_W, PLACEHOLDER_H)
        return CameraFrame(
            frame_id=self._ids(FRAME_ID_PREFIX),
            camera="ceiling",
            camera_id=camera_id,
            store_id=store_id,
            jpeg_b64=base64.b64encode(r.jpeg).decode("ascii"),
            w=r.w,
            h=r.h,
            blurred=False,
            camera_pose=cam.mount.model_copy(),
            ts=utcnow(),
        )

    # =================================================================================================================
    # internals: on-demand streaming
    # =================================================================================================================
    def _demand_for(self, camera_id: CameraId | None, robot_id: RobotId | None) -> list[CameraId]:
        if camera_id is not None:
            return [camera_id]
        if robot_id is not None:
            return self.camera_ids(robot_id=robot_id)
        return []  # an unfiltered subscriber only sees frames produced anyway (scans, captures, other streams)

    def _ensure_streamer(self) -> None:
        if not self._demand or (self._streamer is not None and not self._streamer.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet: CameraService polls capture_camera when nothing arrives
        self._streamer = loop.create_task(self._stream_loop(), name="robot-fake-streamer")

    async def _stream_loop(self) -> None:
        period = 1.0 / self.stream_fps
        loop = asyncio.get_running_loop()
        try:
            while self._demand:
                t0 = loop.time()
                for cid in sorted(self._demand):
                    store = self.camera_store(cid)
                    if store is None or store in self._paused_stores:
                        continue
                    try:
                        frame = await self._capture_by_id(cid, live=True)
                    except RobotError as exc:
                        log.debug("stream %s skipped: %s", cid, exc)
                        continue
                    except Exception as exc:  # keep streaming the other cameras
                        log.warning("stream %s failed: %s", cid, exc)
                        continue
                    self._push_frame(frame)
                await asyncio.sleep(max(0.02, period - (loop.time() - t0)))
        except asyncio.CancelledError:
            raise
        finally:
            self._streamer = None


__all__ = ["DEFAULT_REALTIME_FACTOR", "DEFAULT_SPEED_MPS", "DEFAULT_STREAM_FPS", "DURATIONS_S", "FakeRobot"]
