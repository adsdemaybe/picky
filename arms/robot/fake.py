"""FakeRobot: deterministic 2D kinematic sim implementing ``contracts.services.RobotService``.

The always-available fallback when the Isaac worker is down (and the default robot in ``make demo``).
- Drives a holonomic base along waypoints with the pure-pursuit follower at ~0.8 m/s in SIMULATED time;
  ``realtime_factor`` speeds the demo up (math.inf = no sleeping at all, for tests).
- Scene = the planogram (``services.catalog.get_planogram`` at call time, else ``utils.data``); instances are
  generated deterministically from slot lots (``arms.robot.instances``), so ids/freshness are stable across runs.
- pick/place update holding/basket; capture/scan return a constant synthetic JPEG with ground-truth ``instances``
  (pixel bboxes laid out synthetically). The image has no people in it, so frames are marked ``blurred=True``.
- One motion at a time (``busy``), ``stop`` cancels it (``cancelled``); every failure raises ``RobotError(code)``.
- States stream through ``states()``; the gateway republishes them as ``robot.state``. Set
  ``publish_state_events=True`` to also publish ``robot.state`` via ``services.events`` (standalone runs only: in the
  full pipeline the gateway is the publisher, contracts.events.EVENT_PRODUCERS).
"""

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Iterator

from arms.robot._fanout import Fanout
from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig
from arms.robot.instances import basket_position, build_instances, layout_bboxes
from arms.robot.placeholder import PLACEHOLDER_H, PLACEHOLDER_JPEG_B64, PLACEHOLDER_W
from contracts.catalog import Product
from contracts.common import CameraName, InstanceId, Pose2D, Sku, SlotId, StoreId, TripId, utcnow
from contracts.events import RobotStateEvent
from contracts.planogram import Planogram, Slot
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus
from utils.config import get_settings
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

DEFAULT_SPEED_MPS = 0.8
DEFAULT_REALTIME_FACTOR = 5.0
"""Sim seconds per wall second for the demo (a 10 m leg at 0.8 m/s takes ~2.5 s on screen)."""

DURATIONS_S: dict[str, float] = {"reset": 0.5, "scan": 0.8, "pick": 3.0, "place_in_basket": 1.5}
"""Simulated duration of each non-navigation motion."""

SCAN_RANGE_M = 1.0
"""capture('head') includes a slot's instances when the robot is within this distance of its approach pose."""


class FakeRobot:
    """2D kinematic RobotService. Construct via ``create_fake(services)``; nothing is loaded until the first call."""

    def __init__(
        self,
        services: Services | None = None,
        *,
        realtime_factor: float = DEFAULT_REALTIME_FACTOR,
        speed_mps: float = DEFAULT_SPEED_MPS,
        tick_s: float = 0.05,
        publish_state_events: bool = False,
        state_hz: float = 10.0,
        live_frame_period_s: float = 1.0,
    ) -> None:
        self.services = services or Services()
        self.realtime_factor = realtime_factor
        self.speed_mps = speed_mps
        self.tick_s = tick_s
        self.publish_state_events = publish_state_events
        self.state_period_s = 1.0 / state_hz if state_hz > 0 else 0.0
        self.live_frame_period_s = live_frame_period_s
        self.trip_id: TripId | None = None

        self._ids = IdFactory("robot-fake")
        self._planogram: Planogram | None = None
        self._products: dict[Sku, Product] = {}
        self._slots: dict[SlotId, list[ItemInstance]] = {}
        self._store_id: StoreId | None = None
        self._pose = Pose2D(x=0.0, y=0.0, theta=0.0)
        self._status = RobotStatus.IDLE
        self._holding: ItemInstance | None = None
        self._basket: list[ItemInstance] = []
        self._cmd_id: str | None = None
        self._error: str | None = None
        self._motion: str | None = None
        self._cancel = asyncio.Event()
        self.sim_time_s = 0.0
        self._last_state_push = -math.inf
        self._last_event_push = -math.inf
        self._last_frame_push = -math.inf
        self._state_fanout: Fanout[RobotState] = Fanout()
        self._frame_fanout: Fanout[CameraFrame] = Fanout(maxsize=4)

    # ----------------------------------------------------------------------------------------------- helpers (public)
    def set_trip(self, trip_id: TripId | None) -> None:
        """Trip id stamped on robot.state / error events this robot publishes itself (optional)."""
        self.trip_id = trip_id

    @property
    def planogram(self) -> Planogram | None:
        return self._planogram

    # ----------------------------------------------------------------------------------------------- RobotService
    async def state(self) -> RobotState:
        return self._snapshot()

    async def reset(self, store_id: StoreId) -> RobotState:
        with self._motion_ctx("reset", RobotStatus.IDLE):
            plan, products = await self._load_scene(store_id)
            await self._wait(DURATIONS_S["reset"])
            self._planogram, self._products, self._store_id = plan, products, store_id
            self._slots = build_instances(plan, products)
            self._pose = plan.start_pose.model_copy()
            self._holding = None
            self._basket = []
            self._error = None
            log.info("fake robot reset to %s at (%.2f, %.2f)", store_id, self._pose.x, self._pose.y)
        await self._emit(force=True)
        return self._snapshot()

    async def navigate(self, waypoints: list[Pose2D]) -> RobotState:
        if not waypoints:
            raise RobotError("internal", "navigate needs at least one waypoint")
        with self._motion_ctx("navigate", RobotStatus.NAVIGATING):
            await self._emit(force=True)
            path = [self._pose, *waypoints]
            self._check_path(path)
            pp = PurePursuit(path, PurePursuitConfig(max_speed_mps=self.speed_mps))
            budget = 3.0 * pp.length / max(self.speed_mps, 1e-3) + 20.0
            t = 0.0
            while not pp.done:
                if t > budget:
                    raise RobotError("timeout", f"navigation did not converge within {budget:.0f} sim-s")
                x, y, th = pp.step(self._pose, self.tick_s)
                self._pose = Pose2D(x=x, y=y, theta=th)
                await self._wait(self.tick_s, emit=True)
                t += self.tick_s
            goal = waypoints[-1]
            self._pose = Pose2D(x=goal.x, y=goal.y, theta=goal.theta)
        await self._emit(force=True)
        return self._snapshot()

    async def stop(self) -> RobotState:
        if self._motion is not None:
            self._cancel.set()
            # let the motion coroutine observe the cancel and unwind
            for _ in range(100):
                if self._motion is None:
                    break
                await asyncio.sleep(0 if math.isinf(self.realtime_factor) else 0.005)
        await self._emit(force=True)
        return self._snapshot()

    async def scan(self, slot_id: SlotId) -> CameraFrame:
        await self._ensure_scene()
        slot = self._slot(slot_id)
        with self._motion_ctx("scan", RobotStatus.SCANNING):
            await self._emit(force=True)
            await self._wait(DURATIONS_S["scan"])
            insts = self._slots.get(slot_id, [])
            frame = self._frame("head", insts, slot_id=slot_id, facings=slot.facings)
        self._frame_fanout.push(frame)
        await self._emit(force=True)
        return frame

    async def capture(self, camera: CameraName = "head") -> CameraFrame:
        await self._ensure_scene()
        if camera == "basket":
            frame = self._frame(camera, self._basket_instances(), facings=3)
        else:
            slot_id = self._nearest_slot()
            insts = self._slots.get(slot_id, []) if slot_id else []
            facings = self._slot(slot_id).facings if slot_id else 1
            frame = self._frame(camera, insts, slot_id=slot_id, facings=facings)
        self._frame_fanout.push(frame)
        return frame

    async def list_instances(self, slot_id: SlotId | None = None) -> list[ItemInstance]:
        await self._ensure_scene()
        if slot_id is not None:
            self._slot(slot_id)
            return [i.model_copy() for i in self._slots.get(slot_id, [])]
        out = [i.model_copy() for insts in self._slots.values() for i in insts]
        if self._holding is not None:
            out.append(self._held_instance())
        return out + self._basket_instances()

    async def pick(self, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None) -> ItemInstance:
        await self._ensure_scene()
        slot = self._slot(slot_id)
        if slot.sku != sku:
            raise RobotError("no_instance", f"slot {slot_id} holds {slot.sku}, not {sku}")
        if self._holding is not None:
            raise RobotError("grasp_failed", f"gripper already holds {self._holding.instance_id}")
        insts = self._slots.get(slot_id, [])
        if instance_id is None:
            target = insts[0] if insts else None
        else:
            target = next((i for i in insts if i.instance_id == instance_id), None)
        if target is None:
            raise RobotError("no_instance", f"no {instance_id or sku} on slot {slot_id}")
        dist = self._pose.distance_to(slot.approach_pose)
        if dist > 0.5:  # lenient on purpose: the fake must never stall the demo
            log.warning("fake pick on %s from %.2f m away (not at the approach pose)", slot_id, dist)
        with self._motion_ctx("pick", RobotStatus.PICKING):
            await self._emit(force=True)
            await self._wait(DURATIONS_S["pick"])
            insts.remove(target)
            self._holding = target
        await self._emit(force=True)
        return self._held_instance()

    async def place_in_basket(self) -> RobotState:
        if self._holding is None:
            raise RobotError("no_instance", "gripper is empty")
        with self._motion_ctx("place_in_basket", RobotStatus.PLACING):
            await self._emit(force=True)
            await self._wait(DURATIONS_S["place_in_basket"])
            held = self._holding
            self._holding = None
            self._basket.append(held.model_copy(update={"in_basket": True}))
        await self._emit(force=True)
        return self._snapshot()

    def frames(self) -> AsyncIterator[CameraFrame]:
        return self._frame_fanout.stream()

    def states(self) -> AsyncIterator[RobotState]:
        return self._state_fanout.stream(initial=self._snapshot())

    # ----------------------------------------------------------------------------------------------- internals
    @contextlib.contextmanager
    def _motion_ctx(self, name: str, status: RobotStatus) -> Iterator[None]:
        if self._motion is not None:
            raise RobotError("busy", f"{self._motion} in progress")
        self._motion = name
        self._cmd_id = self._ids("cmd")
        self._cancel.clear()
        self._status = status
        try:
            yield
        except RobotError as exc:
            self._error = None if exc.code == "cancelled" else exc.message
            raise
        finally:
            self._motion = None
            self._cmd_id = None
            self._status = RobotStatus.IDLE

    async def _wait(self, sim_s: float, emit: bool = False) -> None:
        """Advance simulated time by ``sim_s`` (in ticks), sleeping ``sim_s / realtime_factor`` of wall time."""
        remaining = sim_s
        while remaining > 1e-9:
            if self._cancel.is_set():
                raise RobotError("cancelled", f"{self._motion} cancelled by stop")
            dt = min(self.tick_s, remaining)
            remaining -= dt
            self.sim_time_s += dt
            if math.isinf(self.realtime_factor) or self.realtime_factor <= 0:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(dt / self.realtime_factor)
            if emit:
                await self._emit()
        if self._cancel.is_set():
            raise RobotError("cancelled", f"{self._motion} cancelled by stop")

    def _snapshot(self) -> RobotState:
        basket = self._basket_instances()
        return RobotState(
            store_id=self._store_id,
            pose=self._pose.model_copy(),
            status=self._status,
            holding=self._holding.instance_id if self._holding else None,
            basket=[i.instance_id for i in basket],
            basket_skus=[i.sku for i in basket],
            current_cmd_id=self._cmd_id,
            error=self._error,
            ts=utcnow(),
        )

    async def _emit(self, force: bool = False) -> None:
        now = self.sim_time_s
        if force or now - self._last_state_push >= self.state_period_s:
            self._last_state_push = now
            self._state_fanout.push(self._snapshot())
        if self.live_frame_period_s > 0 and self._status == RobotStatus.NAVIGATING:
            if now - self._last_frame_push >= self.live_frame_period_s and self._frame_fanout.subscribers:
                self._last_frame_push = now
                self._frame_fanout.push(self._frame("head", []))
        events = self.services.events
        if self.publish_state_events and events is not None and (force or now - self._last_event_push >= 0.5):
            self._last_event_push = now
            try:
                await events.publish(RobotStateEvent(trip_id=self.trip_id, state=self._snapshot(), source="robot"))
            except Exception as exc:  # never let the UI bus kill a motion
                log.warning("robot.state publish failed: %s", exc)

    async def _load_scene(self, store_id: StoreId) -> tuple[Planogram, dict[Sku, Product]]:
        catalog = self.services.catalog
        plan: Planogram | None = None
        products: dict[Sku, Product] = {}
        if catalog is not None:
            try:
                plan = await catalog.get_planogram(store_id)
                for sku in sorted({s.sku for s in plan.slots}):
                    prod = await catalog.get_product(sku)
                    if prod is not None:
                        products[sku] = prod
            except KeyError as exc:
                raise RobotError("not_ready", f"unknown store {store_id!r}") from exc
            except Exception as exc:
                log.warning("catalog unavailable for robot reset (%s); falling back to data/", exc)
                plan = None
        if plan is None:
            from utils.data import DataError, load_planogram, load_products

            try:
                plan = load_planogram(store_id)
            except DataError as exc:
                raise RobotError("not_ready", str(exc)) from exc
            try:
                products = load_products()
            except DataError:
                products = {}
        return plan, products

    async def _ensure_scene(self) -> None:
        if self._planogram is None:
            store = self._store_id or get_settings().picky_default_store
            log.info("fake robot: no scene yet, auto-resetting to %s", store)
            await self.reset(store)

    def _slot(self, slot_id: SlotId | None) -> Slot:
        slot = self._planogram.slot(slot_id) if (self._planogram and slot_id) else None
        if slot is None:
            raise RobotError("unknown_slot", f"unknown slot {slot_id!r} in {self._store_id}")
        return slot

    def _nearest_slot(self) -> SlotId | None:
        if self._planogram is None:
            return None
        best, best_d = None, SCAN_RANGE_M
        for s in self._planogram.slots:
            d = self._pose.distance_to(s.approach_pose)
            if d <= best_d:
                best, best_d = s.slot_id, d
        return best

    def _check_path(self, path: list[Pose2D]) -> None:
        plan = self._planogram
        if plan is None:
            return
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

    def _held_instance(self) -> ItemInstance:
        assert self._holding is not None
        c, s = math.cos(self._pose.theta), math.sin(self._pose.theta)
        pos = self._holding.position.model_copy(
            update={"x": round(self._pose.x + 0.5 * c, 4), "y": round(self._pose.y + 0.5 * s, 4), "z": 0.9}
        )
        return self._holding.model_copy(update={"position": pos})

    def _basket_instances(self) -> list[ItemInstance]:
        return [
            i.model_copy(update={"position": basket_position(self._pose, n), "in_basket": True})
            for n, i in enumerate(self._basket)
        ]

    def _frame(
        self, camera: CameraName, instances: list[ItemInstance], slot_id: SlotId | None = None, facings: int = 1
    ) -> CameraFrame:
        boxes = layout_bboxes(instances, PLACEHOLDER_W, PLACEHOLDER_H, facings)
        return CameraFrame(
            frame_id=self._ids("frame"),
            camera=camera,
            jpeg_b64=PLACEHOLDER_JPEG_B64,
            w=PLACEHOLDER_W,
            h=PLACEHOLDER_H,
            blurred=True,  # synthetic image: no people, nothing to blur
            slot_id=slot_id,
            pose=self._pose.model_copy(),
            instances=[i.model_copy(update={"bbox": boxes.get(i.instance_id)}) for i in instances],
            ts=utcnow(),
        )


__all__ = ["DEFAULT_REALTIME_FACTOR", "DEFAULT_SPEED_MPS", "DURATIONS_S", "FakeRobot"]
