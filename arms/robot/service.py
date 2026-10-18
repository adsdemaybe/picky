"""SimRobot: the real MULTI-ROBOT ``RobotService``: Isaac Sim over the gateway's multiplexed ``SimLink``, with the 2D
fake built in as the always-available fallback (contracts/sim.py "Which backend drives a store").

Backend PER STORE, never per robot:
- A store runs on Isaac iff a connected worker hosts it (``SimLink.status()`` / ``SimHello`` / ``SimHosted``) loaded
  with ``data_hash == planogram_hash(live planogram)``. A stale or unloaded store first gets the store-level
  ``ResetCmd(robot_id=None, store_id, planogram, products, robots, cameras)``. A store moves from the fake to Isaac
  only while all its robots are idle with an empty gripper + basket; otherwise it stays on the fake.
- Everything else runs on the built-in ``FakeRobot`` (so ``PICKY_REAL_ARMS=robot`` works without a GPU box and the
  fleet always sees every robot; ``RobotState.backend`` says which).
- Isaac loss (``ConnectionError`` mid-command / worker gone): the in-flight command raises
  ``RobotError('disconnected')`` ONCE (+ an ``error`` event); the store's robots continue on the fake from their last
  ``RobotState`` (pose, battery, holding, basket; no reset) and the fake shelves are re-synced with the catalog.

Wire: every call is one command with a fresh ``cmd_id`` and ``robot_id`` (``SimLink.send`` correlates the
``CommandResult``); ``ok=False`` -> ``RobotError(error_code)``; ``TimeoutError`` -> ``RobotError('timeout')`` + error
event. One motion at a time per robot (``busy``). Unsolicited ``RobotState`` / ``CameraFrame`` / ``InstanceInfo`` are
cached (per-robot state, per-camera latest frame, instances) and fanned out. Opening ``frames(camera)`` streams that
camera: ref-counted ``SetStreamsCmd`` with the union of open Isaac cameras (fps <= 15). The robot arm owns the battery
(integrated from pose deltas; carried across backends). Subscription to the link is lazy (never in ``create()``).
"""

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from arms.robot._fanout import Fanout
from arms.robot.fake import FakeRobot
from arms.robot.rover import Battery, heading_to_dock, on_dock
from arms.robot.world import live_planogram, load_scene
from contracts.cameras import parse_camera_id
from contracts.common import CameraId, InstanceId, Pose2D, RobotId, Sku, SlotId, StoreId, TripId
from contracts.events import make_event
from contracts.planogram import Planogram, RobotSpec
from contracts.services import Services
from contracts.sim import (
    MOTION_COMMANDS,
    CameraFrame,
    CaptureCmd,
    CommandResult,
    InstanceInfo,
    ItemInstance,
    ListInstancesCmd,
    NavigateCmd,
    PickCmd,
    PlaceInBasketCmd,
    ResetCmd,
    RobotError,
    RobotState,
    RobotStatus,
    ScanCmd,
    SetSlotStockCmd,
    SetStreamsCmd,
    SimHello,
    SimHosted,
    SimLink,
    SimLog,
    SimMessage,
    SimRobotInfo,
    SimStatus,
    SimStoreInfo,
    StopCmd,
    UnloadBasketCmd,
    planogram_hash,
)
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

MAX_STREAM_FPS = 15.0


class SimRobot:
    """RobotService over a SimLink (+ built-in 2D fake). Construct via ``create(services)``; no I/O until a call.

    ``fake_opts`` go to the built-in ``FakeRobot`` (e.g. ``realtime_factor``); ``fallback`` reuses an existing one."""

    def __init__(
        self,
        services: Services | None = None,
        link: SimLink | None = None,
        *,
        fallback: FakeRobot | None = None,
        stream_fps: float = 5.0,
        **fake_opts: Any,
    ) -> None:
        self.services = services or Services()
        self._link = link
        self.fake = fallback or FakeRobot(self.services, **fake_opts)
        self.fake.add_listener(self._on_fake_state, self._on_fake_frame)
        self.stream_fps = min(MAX_STREAM_FPS, max(0.5, stream_fps))
        self.trip_id: TripId | None = None
        self._subscribed_to: SimLink | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._hello_stores: dict[StoreId, SimStoreInfo] = {}
        self._hello_robots: dict[RobotId, SimRobotInfo] = {}
        self._reset_hash: dict[StoreId, str] = {}
        self._isaac_stores: set[StoreId] = set()
        self._isaac_states: dict[RobotId, RobotState] = {}
        self._battery: dict[RobotId, Battery] = {}
        self._busy: dict[RobotId, str] = {}
        self._instances: dict[InstanceId, ItemInstance] = {}
        self._frame_cache: dict[CameraId, CameraFrame] = {}
        self._stream_refs: Counter[CameraId] = Counter()
        self._streams_sent: tuple[CameraId, ...] | None = None
        self._store_locks: dict[StoreId, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._t0 = time.monotonic()
        self._state_fanout: Fanout[RobotState] = Fanout(maxsize=256)
        self._frame_fanout: Fanout[CameraFrame] = Fanout(maxsize=8)

    # =================================================================================================================
    # wiring helpers (not in the Protocol)
    # =================================================================================================================
    def attach_link(self, link: SimLink | None) -> None:
        """Use ``link`` instead of ``services.sim_link`` (the gateway may call this when a worker (re)connects)."""
        if link is self._link and link is self._subscribed_to:
            return
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
        self._unsubscribe, self._subscribed_to = None, None
        self._link = link
        self._ensure_subscribed()

    def set_trip(self, trip_id: TripId | None) -> None:
        """Trip id stamped on ``error`` events this robot publishes (legacy single-trip callers)."""
        self.trip_id = trip_id
        self.fake.set_trip(trip_id)

    def set_task(self, robot_id: RobotId, text: str | None) -> None:
        """HUD text on the robot's synthetic camera frames (fake backend)."""
        self.fake.set_task(robot_id, text)

    def is_connected(self) -> bool:
        link = self._current_link()
        return link is not None and self._link_up(link)

    def backend(self, store_id: StoreId) -> str:
        """'isaac' or 'fake': what drives that store right now."""
        return "isaac" if store_id in self._isaac_stores else "fake"

    def sim_status(self, base: SimStatus | None = None) -> SimStatus:
        """``SimStatus`` with ``isaac_*`` / ``fake_*`` filled from this arm's per-store choice (GET /api/sim/status)."""
        if base is None:
            link = self._current_link()
            fn = getattr(link, "status", None)
            try:
                base = fn() if callable(fn) else None
            except Exception:
                base = None
        base = base or SimStatus(connected=self.is_connected())
        stores = self.fake.store_ids()
        isaac_stores = sorted(s for s in stores if s in self._isaac_stores)
        fake_stores = sorted(s for s in stores if s not in self._isaac_stores)
        robots = self.fake.robot_ids()
        isaac_robots = [r for r in robots if self.fake.spec(r).store_id in self._isaac_stores]
        note = (
            "every store runs on Isaac"
            if not fake_stores
            else "Isaac not connected: every robot runs on the 2D fake"
            if not base.connected
            else f"on the 2D fake: {', '.join(fake_stores)}"
        )
        return base.model_copy(
            update={
                "isaac_store_ids": isaac_stores,
                "fake_store_ids": fake_stores,
                "isaac_robot_ids": isaac_robots,
                "fake_robot_ids": [r for r in robots if r not in isaac_robots],
                "note": note,
            }
        )

    async def refresh_backends(self) -> dict[StoreId, str]:
        """Re-evaluate every store's backend now (resetting stale Isaac scenes, switching idle stores to Isaac)."""
        for store_id in self.fake.store_ids():
            try:
                await self._use_isaac(store_id)
            except RobotError as exc:
                log.warning("backend check for %s failed: %s", store_id, exc)
        return {s: self.backend(s) for s in self.fake.store_ids()}

    # =================================================================================================================
    # RobotService
    # =================================================================================================================
    async def list_robots(self) -> list[RobotState]:
        self._ensure_subscribed()
        return [self._current_state(rid) for rid in self.fake.robot_ids()]

    async def state(self, robot_id: RobotId) -> RobotState:
        self._ensure_subscribed()
        self._spec(robot_id)
        return self._current_state(robot_id)

    async def reset(self, robot_id: RobotId, store_id: StoreId | None = None) -> RobotState:
        spec = self._spec(robot_id)
        if store_id is not None and store_id != spec.store_id:
            raise RobotError("unknown_robot", f"{robot_id} works in {spec.store_id}, not {store_id}")
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.reset(robot_id, spec.store_id)
        plan, products = await load_scene(self.services, spec.store_id)
        cmd = ResetCmd(
            cmd_id=new_id("cmd"),
            robot_id=robot_id,
            store_id=spec.store_id,
            planogram=plan,
            products=list(products.values()) or None,
            robots=[s for s in self.fake.specs() if s.store_id == spec.store_id],
            cameras=self.fake.store_camera_specs(spec.store_id) or None,
        )
        res = await self._isaac(spec, cmd)
        self._battery[robot_id] = Battery(pct=spec.battery_pct, last_t=self._now())
        return self._state_from(spec, res)

    async def navigate(self, robot_id: RobotId, waypoints: list[Pose2D]) -> RobotState:
        spec = self._spec(robot_id)
        if not waypoints:
            raise RobotError("internal", "navigate needs at least one waypoint")
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.navigate(robot_id, waypoints)
        self._bat(robot_id).check_motion(robot_id, heading_to_dock(waypoints, spec))
        cmd = NavigateCmd(cmd_id=new_id("cmd"), robot_id=robot_id, waypoints=waypoints, speed_mps=spec.max_speed_mps)
        return self._state_from(spec, await self._isaac(spec, cmd))

    async def stop(self, robot_id: RobotId) -> RobotState:
        spec = self._spec(robot_id)
        if spec.store_id in self._isaac_stores and self.is_connected():
            try:
                res = await self._isaac(spec, StopCmd(cmd_id=new_id("cmd"), robot_id=robot_id))
                return self._state_from(spec, res)
            except RobotError as exc:
                if exc.code != "disconnected":
                    raise
        return await self.fake.stop(robot_id)

    async def scan(self, robot_id: RobotId, slot_id: SlotId) -> CameraFrame:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.scan(robot_id, slot_id)
        res = await self._isaac(spec, ScanCmd(cmd_id=new_id("cmd"), robot_id=robot_id, slot_id=slot_id))
        if res.frame is None:
            raise RobotError("internal", f"scan of {slot_id} returned no frame")
        return self._frame_from(res.frame, spec, camera="front")

    async def capture(self, robot_id: RobotId, camera: str = "front") -> CameraFrame:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.capture(robot_id, camera)
        res = await self._isaac(spec, CaptureCmd(cmd_id=new_id("cmd"), robot_id=robot_id, camera=camera))  # type: ignore[arg-type]
        if res.frame is None:
            raise RobotError("internal", f"capture({camera}) returned no frame")
        return self._frame_from(res.frame, spec, camera=camera)

    async def capture_camera(self, camera_id: CameraId) -> CameraFrame:
        try:
            owner, kind = parse_camera_id(camera_id)
        except ValueError as exc:
            raise RobotError("unknown_robot", str(exc)) from exc
        if kind != "ceiling":
            return await self.capture(owner, kind)
        store_id = owner
        if store_id not in self.fake.store_ids() or not await self._use_isaac(store_id):
            return await self.fake.capture_camera(camera_id)
        try:
            res = await self._send(CaptureCmd(cmd_id=new_id("cmd"), camera_id=camera_id), store_id=store_id)
        except RobotError as exc:
            if exc.code in ("disconnected", "timeout"):
                return await self.fake.capture_camera(camera_id)
            raise
        if res.frame is None:
            raise RobotError("internal", f"capture({camera_id}) returned no frame")
        frame = res.frame.model_copy(update={"camera_id": camera_id, "store_id": store_id, "camera": "ceiling"})
        self._frame_cache[camera_id] = frame
        return frame

    async def list_instances(self, robot_id: RobotId, slot_id: SlotId | None = None) -> list[ItemInstance]:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.list_instances(robot_id, slot_id)
        cmd = ListInstancesCmd(cmd_id=new_id("cmd"), robot_id=robot_id, store_id=spec.store_id, slot_id=slot_id)
        res = await self._isaac(spec, cmd)
        return [i.model_copy(update={"bbox": None, "store_id": i.store_id or spec.store_id}) for i in res.instances]

    async def pick(
        self, robot_id: RobotId, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None
    ) -> ItemInstance:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.pick(robot_id, slot_id, sku, instance_id)
        self._bat(robot_id).check_motion(robot_id)
        cmd = PickCmd(cmd_id=new_id("cmd"), robot_id=robot_id, slot_id=slot_id, sku=sku, instance_id=instance_id)
        res = await self._isaac(spec, cmd)
        picked_id = res.instance_id or instance_id
        inst = next((i for i in res.instances if i.instance_id == picked_id), None)
        if inst is None and res.instances:
            inst = res.instances[0]
        if inst is None and picked_id and picked_id in self._instances:
            inst = self._instances[picked_id]
        if inst is None:
            raise RobotError("internal", f"pick on {slot_id} succeeded but returned no instance")
        return inst.model_copy(update={"store_id": inst.store_id or spec.store_id})

    async def place_in_basket(self, robot_id: RobotId) -> RobotState:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.place_in_basket(robot_id)
        return self._state_from(
            spec, await self._isaac(spec, PlaceInBasketCmd(cmd_id=new_id("cmd"), robot_id=robot_id))
        )

    async def unload_basket(self, robot_id: RobotId) -> list[ItemInstance]:
        spec = self._spec(robot_id)
        if not await self._use_isaac(spec.store_id, robot_id):
            return await self.fake.unload_basket(robot_id)
        res = await self._isaac(spec, UnloadBasketCmd(cmd_id=new_id("cmd"), robot_id=robot_id))
        return [i.model_copy(update={"store_id": i.store_id or spec.store_id}) for i in res.instances]

    async def sync_stock(self, store_id: StoreId, slot_ids: list[SlotId] | None = None) -> None:
        # the fake shelves always follow the catalog (they are the fallback world)
        try:
            await self.fake.sync_stock(store_id, slot_ids)
        except RobotError as exc:
            log.warning("fake sync_stock(%s) failed: %s", store_id, exc)
        if store_id not in self._isaac_stores or not self.is_connected():
            return
        plan = await live_planogram(self.services, store_id)
        wanted = set(slot_ids) if slot_ids is not None else None
        for slot in plan.slots:
            if wanted is not None and slot.slot_id not in wanted:
                continue
            cmd = SetSlotStockCmd(
                cmd_id=new_id("cmd"),
                store_id=store_id,
                slot_id=slot.slot_id,
                sku=slot.sku,
                qty=slot.qty,
                lots=slot.lots,
            )
            try:
                await self._send(cmd, store_id=store_id)
            except RobotError as exc:
                log.warning("set_slot_stock %s failed: %s", slot.slot_id, exc)
                if exc.code == "disconnected":
                    return

    def frames(self, camera_id: CameraId | None = None, robot_id: RobotId | None = None) -> AsyncIterator[CameraFrame]:
        self._ensure_subscribed()
        demand = self.fake._demand_for(camera_id, robot_id)
        for cid in demand:
            self._stream_refs[cid] += 1
        self.fake.add_stream_demand(demand)
        self._schedule(self._sync_streams())

        def match(f: CameraFrame) -> bool:
            return (camera_id is None or f.camera_id == camera_id) and (robot_id is None or f.robot_id == robot_id)

        def on_close() -> None:
            for cid in demand:
                self._stream_refs[cid] -= 1
                if self._stream_refs[cid] <= 0:
                    del self._stream_refs[cid]
            self.fake.remove_stream_demand(demand)
            self._schedule(self._sync_streams())

        return self._frame_fanout.stream(predicate=match, on_close=on_close)

    def states(self, robot_id: RobotId | None = None) -> AsyncIterator[RobotState]:
        self._ensure_subscribed()
        ids = self.fake.robot_ids()
        if robot_id is not None and robot_id not in ids:
            raise RobotError("unknown_robot", f"unknown robot {robot_id!r}")
        initial = [self._current_state(r) for r in ids if robot_id is None or r == robot_id]
        return self._state_fanout.stream(
            initial=initial, predicate=None if robot_id is None else (lambda s: s.robot_id == robot_id)
        )

    # =================================================================================================================
    # internals: backend choice
    # =================================================================================================================
    def _spec(self, robot_id: RobotId | None) -> RobotSpec:
        try:
            return self.fake.spec(robot_id or "")
        except RobotError:
            raise RobotError("unknown_robot", f"unknown robot {robot_id!r}") from None

    def _current_link(self) -> SimLink | None:
        return self._link if self._link is not None else self.services.sim_link

    @staticmethod
    def _link_up(link: SimLink) -> bool:
        try:
            return bool(link.is_connected())
        except Exception:
            return False

    def _hosted(self) -> tuple[dict[StoreId, SimStoreInfo], dict[RobotId, SimRobotInfo]]:
        link = self._current_link()
        if link is None or not self._link_up(link):
            return {}, {}
        fn = getattr(link, "status", None)
        status: SimStatus | None = None
        if callable(fn):
            try:
                status = fn()
            except Exception:
                status = None
        if status is not None and status.workers:
            stores: dict[StoreId, SimStoreInfo] = {}
            robots: dict[RobotId, SimRobotInfo] = {}
            for wk in status.workers:
                if not wk.connected:
                    continue
                stores.update({st.store_id: st for st in wk.stores})
                robots.update({r.robot_id: r for r in wk.robots})
            if stores:
                return stores, robots
        return dict(self._hello_stores), dict(self._hello_robots)

    def _lock(self, store_id: StoreId) -> asyncio.Lock:
        return self._store_locks.setdefault(store_id, asyncio.Lock())

    async def _use_isaac(self, store_id: StoreId, robot_id: RobotId | None = None) -> bool:
        """True when ``store_id`` (and that robot) runs on Isaac now; switches / falls back as the rules say."""
        self._ensure_subscribed()
        stores, robots = self._hosted()
        info = stores.get(store_id)
        if info is None or (robot_id is not None and robots and robot_id not in robots):
            if store_id in self._isaac_stores:
                await self._fall_back(store_id, "the worker no longer hosts it")
            return False
        if store_id in self._isaac_stores:
            return True
        if not self.fake.store_idle(store_id):
            return False  # switch only between goals (idle, empty gripper + basket)
        async with self._lock(store_id):
            if store_id in self._isaac_stores:
                return True
            try:
                plan = await live_planogram(self.services, store_id)
            except RobotError as exc:
                log.warning("no live planogram for %s (%s): staying on the fake", store_id, exc)
                return False
            want = planogram_hash(plan)
            if not (info.loaded and (info.data_hash == want or self._reset_hash.get(store_id) == want)):
                if not await self._reset_isaac_store(store_id, plan):
                    return False
            for rid in self.fake.robot_ids(store_id):
                self._battery[rid] = Battery(pct=self.fake.snapshot(rid).battery_pct or 100.0, last_t=self._now())
            self._isaac_stores.add(store_id)
            self.fake.pause_store(store_id, True)
            log.info("store %s now runs on Isaac", store_id)
        await self._sync_streams()
        return True

    async def _reset_isaac_store(self, store_id: StoreId, plan: Planogram) -> bool:
        try:
            _, products = await load_scene(self.services, store_id)
        except RobotError:
            products = {}
        cmd = ResetCmd(
            cmd_id=new_id("cmd"),
            robot_id=None,
            store_id=store_id,
            planogram=plan,
            products=list(products.values()) or None,
            robots=[s for s in self.fake.specs() if s.store_id == store_id],
            cameras=self.fake.store_camera_specs(store_id) or None,
        )
        try:
            res = await self._send(cmd, store_id=store_id, report_disconnect=False)
        except RobotError as exc:
            log.warning("store reset of %s on Isaac failed (%s): staying on the fake", store_id, exc)
            return False
        if not res.ok:
            return False
        self._reset_hash[store_id] = planogram_hash(plan)
        prev = self._hello_stores.get(store_id)
        self._hello_stores[store_id] = SimStoreInfo(
            store_id=store_id,
            data_hash=self._reset_hash[store_id],
            loaded=True,
            camera_ids=prev.camera_ids if prev else [],
        )
        log.info("rebuilt %s on Isaac (data_hash %s…)", store_id, self._reset_hash[store_id][:8])
        return True

    async def _fall_back(self, store_id: StoreId, reason: str) -> None:
        """Isaac lost for a store: its robots continue on the 2D fake from their last state (no reset)."""
        if store_id not in self._isaac_stores:
            return
        self._isaac_stores.discard(store_id)
        for rid in self.fake.robot_ids(store_id):
            st = self._isaac_states.get(rid)
            if st is None:
                continue
            st = st.model_copy(update={"battery_pct": self._bat(rid).display})
            try:
                await self.fake.adopt_state(st, self._instances)
            except Exception as exc:
                log.warning("could not move %s onto the fake: %s", rid, exc)
        self.fake.pause_store(store_id, False)
        with contextlib.suppress(Exception):
            await self.fake.sync_stock(store_id)
        await self._report(
            "disconnected", f"Isaac lost for {store_id} ({reason}): robots continue on the 2D fake", store_id
        )

    # =================================================================================================================
    # internals: commands
    # =================================================================================================================
    async def _isaac(self, spec: RobotSpec, cmd: Any) -> CommandResult:
        """Send a robot command to Isaac (per-robot busy for motions); results update the caches."""
        rid = spec.robot_id
        motion = cmd.type in MOTION_COMMANDS
        if motion:
            if rid in self._busy:
                raise RobotError("busy", f"{rid}: {self._busy[rid]} in progress")
            self._busy[rid] = cmd.type
        try:
            return await self._send(cmd, store_id=spec.store_id, robot_id=rid)
        finally:
            if motion:
                self._busy.pop(rid, None)

    async def _send(
        self, cmd: Any, *, store_id: StoreId, robot_id: RobotId | None = None, report_disconnect: bool = True
    ) -> CommandResult:
        link = self._current_link()
        if link is None or not self._link_up(link):
            if report_disconnect:
                await self._fall_back(store_id, "worker not connected")
            raise RobotError("disconnected", "sim worker not connected")
        try:
            res = await link.send(cmd)
        except TimeoutError as exc:
            await self._report("timeout", f"{cmd.type} ({cmd.cmd_id}) timed out", store_id, robot_id)
            raise RobotError("timeout", f"{cmd.type} timed out") from exc
        except ConnectionError as exc:
            if report_disconnect:
                await self._fall_back(store_id, str(exc) or "link dropped")
            raise RobotError("disconnected", str(exc) or "sim link dropped") from exc
        if res.state is not None:
            self._ingest_state(res.state)
        for inst in res.instances:
            self._instances[inst.instance_id] = inst
        if res.frame is not None and res.frame.camera_id:
            self._frame_cache[res.frame.camera_id] = res.frame
        if not res.ok:
            raise RobotError(res.error_code or "internal", res.error or f"{cmd.type} failed")
        return res

    def _state_from(self, spec: RobotSpec, res: CommandResult) -> RobotState:
        return self._current_state(spec.robot_id)

    def _frame_from(self, frame: CameraFrame, spec: RobotSpec, camera: str) -> CameraFrame:
        from contracts.cameras import robot_camera_id
        from contracts.common import normalize_camera

        kind = normalize_camera(camera)
        cid = frame.camera_id or robot_camera_id(spec.robot_id, kind)
        out = frame.model_copy(
            update={
                "camera_id": cid,
                "robot_id": frame.robot_id or spec.robot_id,
                "store_id": frame.store_id or spec.store_id,
                "instances": [i.model_copy(update={"store_id": i.store_id or spec.store_id}) for i in frame.instances],
                "blurred": False,  # only perception (CameraService.blur_faces) may mark a frame blurred (§8)
            }
        )
        self._frame_cache[cid] = out
        return out

    def _now(self) -> float:
        return time.monotonic() - self._t0

    def _bat(self, robot_id: RobotId) -> Battery:
        bat = self._battery.get(robot_id)
        if bat is None:
            pct = self.fake.snapshot(robot_id).battery_pct
            bat = self._battery[robot_id] = Battery(pct=100.0 if pct is None else pct, last_t=self._now())
        return bat

    def _current_state(self, robot_id: RobotId) -> RobotState:
        spec = self._spec(robot_id)
        st = self._isaac_states.get(robot_id)
        if spec.store_id not in self._isaac_stores or st is None:
            return self.fake.snapshot(robot_id)
        bat = self._bat(robot_id)
        idle = st.status in (RobotStatus.IDLE, RobotStatus.CHARGING) and robot_id not in self._busy
        docked = idle and on_dock(st.pose, spec)
        bat.settle(self._now(), charging=docked)
        return st.model_copy(
            update={
                "battery_pct": bat.display,
                "charging": docked and bat.pct < 100.0,
                "status": RobotStatus.CHARGING
                if docked
                else (RobotStatus.IDLE if st.status == RobotStatus.CHARGING else st.status),
                "backend": "isaac",
                "store_id": st.store_id or spec.store_id,
            }
        )

    def _ingest_state(self, state: RobotState) -> None:
        rid = state.robot_id
        if not rid or rid not in self.fake.robot_ids():
            return
        spec = self.fake.spec(rid)
        prev = self._isaac_states.get(rid)
        bat = self._bat(rid)
        if prev is not None:
            bat.drive(prev.pose.distance_to(state.pose))
        self._isaac_states[rid] = state.model_copy(update={"store_id": state.store_id or spec.store_id})
        if spec.store_id in self._isaac_stores:
            self._state_fanout.push(self._current_state(rid))

    # =================================================================================================================
    # internals: link messages, fake listeners, streams
    # =================================================================================================================
    def _ensure_subscribed(self) -> None:
        link = self._current_link()
        if link is None or link is self._subscribed_to:
            return
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
        try:
            self._unsubscribe = link.subscribe(self._on_message)
        except Exception as exc:
            log.warning("could not subscribe to the sim link: %s", exc)
            return
        self._subscribed_to = link

    async def _on_message(self, msg: SimMessage) -> None:
        if isinstance(msg, SimHello | SimHosted):
            if isinstance(msg, SimHello):
                stores = list(msg.stores)
                if msg.store_id and all(s.store_id != msg.store_id for s in stores):  # v0.1 single-store hello
                    stores.append(SimStoreInfo(store_id=msg.store_id, data_hash=msg.data_hash))
            else:
                stores = list(msg.stores)
            self._hello_stores = {s.store_id: s for s in stores}
            self._hello_robots = {r.robot_id: r for r in msg.robots}
            self._streams_sent = None  # a new worker streams its defaults: resend our set
            self._schedule(self.refresh_backends())
        elif isinstance(msg, RobotState):
            self._ingest_state(msg)
        elif isinstance(msg, CameraFrame):
            cid = msg.camera_id
            if cid is None and msg.robot_id:
                from contracts.cameras import robot_camera_id

                cid = robot_camera_id(msg.robot_id, msg.camera)
            if cid is None:
                return
            # never trust a worker's blurred flag: only perception may set it, so every frame gets face-blurred (§8)
            frame = msg.model_copy(update={"camera_id": cid, "instances": [], "blurred": False})
            self._frame_cache[cid] = frame
            store = frame.store_id or self.fake.camera_store(cid)
            if store in self._isaac_stores:
                self._frame_fanout.push(frame)
        elif isinstance(msg, InstanceInfo):
            for inst in msg.instances:
                self._instances[inst.instance_id] = inst
        elif isinstance(msg, SimLog):
            level = logging.getLevelName(msg.level.upper())
            log.log(level if isinstance(level, int) else logging.INFO, "[sim %s] %s", msg.robot_id or "-", msg.message)

    def _on_fake_state(self, state: RobotState) -> None:
        if state.store_id not in self._isaac_stores:
            self._state_fanout.push(state)

    def _on_fake_frame(self, frame: CameraFrame) -> None:
        if (frame.store_id or "") not in self._isaac_stores:
            self._frame_fanout.push(frame)

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sync_streams(self) -> None:
        if not self._isaac_stores or not self.is_connected():
            return
        cams = tuple(sorted(c for c in self._stream_refs if self.fake.camera_store(c) in self._isaac_stores))
        if cams == self._streams_sent:
            return
        self._streams_sent = cams
        store = next(iter(sorted(self._isaac_stores)))
        cmd = SetStreamsCmd(cmd_id=new_id("cmd"), camera_ids=list(cams), fps=self.stream_fps)
        try:
            await self._send(cmd, store_id=store, report_disconnect=False)
        except RobotError as exc:
            self._streams_sent = None
            log.warning("set_streams failed: %s", exc)

    async def _report(
        self, code: str, message: str, store_id: StoreId | None = None, robot_id: RobotId | None = None
    ) -> None:
        log.warning("robot error %s: %s", code, message)
        events = self.services.events
        if events is None:
            return
        try:
            await events.publish(
                make_event(
                    "error",
                    trip_id=self.trip_id,
                    source="robot",
                    store_id=store_id,
                    robot_id=robot_id,
                    arm="robot",
                    message=message,
                    code=code,
                )
            )
        except Exception as exc:
            log.warning("could not publish robot error event: %s", exc)


__all__ = ["MAX_STREAM_FPS", "SimRobot"]
