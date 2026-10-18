"""LoopbackSimLink: a ``contracts.sim.SimLink`` whose "Isaac worker" is an in-process ``FakeRobot``.

Lets the REAL ``SimRobot`` (and the gateway's /ws/sim plumbing tests) run without a GPU box:
``link = LoopbackSimLink(FakeRobot(...)); await link.connect(); SimRobot(services, link=link)``. It documents the
worker-side semantics the Isaac worker (arms/sim) must match: ``SimHello`` listing stores (``loaded`` + ``data_hash``),
robots and cameras, then one ``RobotState`` per robot; exactly one ``CommandResult`` per ``cmd_id`` echoing
``robot_id``; ``error_code`` on failure; ``busy`` per robot; store-level ``reset`` (robot_id None) followed by
``SimHosted``; ``set_streams`` / ``set_slot_stock``; ``disconnect()`` fails in-flight sends with ``ConnectionError``.
"""

import asyncio
import math
from collections.abc import Callable

from arms.robot.fake import FakeRobot
from contracts import CONTRACTS_VERSION
from contracts.cameras import CameraInfo, parse_camera_id
from contracts.common import CameraId, StoreId
from contracts.sim import (
    BASKET_UNLOAD_CAPABILITY,
    CameraFrame,
    CaptureCmd,
    CommandResult,
    ItemInstance,
    ListInstancesCmd,
    NavigateCmd,
    PickCmd,
    PickFromBasketCmd,
    PlaceInBasketCmd,
    PlaceOnCounterCmd,
    ResetCmd,
    RobotError,
    ScanCmd,
    SetSlotStockCmd,
    SetStreamsCmd,
    SimHello,
    SimHosted,
    SimMessage,
    SimMessageHandler,
    SimRobotInfo,
    SimStatus,
    SimStoreInfo,
    SimWorkerStatus,
    StopCmd,
    UnloadBasketCmd,
    default_timeout_s,
    planogram_hash,
)

STALE_HASH = "stale"


class LoopbackSimLink:
    def __init__(
        self,
        robot: FakeRobot | None = None,
        connected: bool = False,
        *,
        stores: list[StoreId] | None = None,
        stale_stores: tuple[StoreId, ...] | list[StoreId] = (),
        worker_id: str = "loopback-0",
    ) -> None:
        self.robot = robot or FakeRobot(realtime_factor=math.inf)
        self.connected = connected
        self.worker_id = worker_id
        self.sent: list[object] = []
        self.streaming: list[CameraId] = []
        self.stream_fps: float = 2.0
        self._handlers: list[SimMessageHandler] = []
        self._only_stores = list(stores) if stores is not None else None
        self._stale = set(stale_stores)
        self._hashes: dict[StoreId, str] = {}
        self._inflight: set[asyncio.Task[CommandResult]] = set()
        self._killed: set[asyncio.Task[CommandResult]] = set()
        self.hello: SimHello | None = None

    # ------------------------------------------------------------------------------------------------- worker side
    def hosted_store_ids(self) -> list[StoreId]:
        ids = self.robot.store_ids()
        return [s for s in ids if self._only_stores is None or s in self._only_stores]

    async def _lists(self) -> tuple[list[SimStoreInfo], list[SimRobotInfo], list[CameraInfo]]:
        stores, robots, cams = [], [], []
        for sid in self.hosted_store_ids():
            world = await self.robot.world(sid)
            h = STALE_HASH if sid in self._stale else self._hashes.get(sid, world.data_hash)
            ceil = [c.camera_id for c in self.robot.store_camera_specs(sid)]
            stores.append(SimStoreInfo(store_id=sid, data_hash=h, camera_ids=ceil, loaded=sid not in self._stale))
            for rid in self.robot.robot_ids(sid):
                robots.append(SimRobotInfo(robot_id=rid, store_id=sid, camera_ids=self.robot.camera_ids(robot_id=rid)))
            for cid in self.robot.camera_ids(store_id=sid):
                owner, kind = parse_camera_id(cid)
                cams.append(
                    CameraInfo(
                        camera_id=cid,
                        kind=kind,
                        name=cid,
                        store_id=sid,
                        robot_id=None if kind == "ceiling" else owner,
                        source="isaac",
                    )
                )
        return stores, robots, cams

    async def connect(self) -> SimHello:
        """Simulate the worker connecting: SimHello, then one RobotState per hosted robot."""
        stores, robots, cams = await self._lists()
        self.connected = True
        self.hello = SimHello(
            token="loopback",
            worker_id=self.worker_id,
            stores=stores,
            robots=robots,
            cameras=cams,
            contracts_version=CONTRACTS_VERSION,
            capabilities=[BASKET_UNLOAD_CAPABILITY],
        )
        await self._dispatch(self.hello)
        for r in robots:
            await self._dispatch(self.robot.snapshot(r.robot_id))
        return self.hello

    async def disconnect(self) -> None:
        """Simulate losing the worker: in-flight sends raise ConnectionError, later sends too."""
        self.connected = False
        for task in list(self._inflight):
            self._killed.add(task)
            task.cancel()
        await asyncio.sleep(0)

    async def push(self, msg: SimMessage) -> None:
        """Deliver an unsolicited worker message to subscribers (tests)."""
        await self._dispatch(msg)

    # ------------------------------------------------------------------------------------------------- SimLink
    def is_connected(self, robot_id: str | None = None) -> bool:
        if not self.connected:
            return False
        if robot_id is None:
            return True
        return self.hello is not None and robot_id in self.hello.robot_ids()

    def status(self) -> SimStatus:
        if not self.connected or self.hello is None:
            return SimStatus(connected=False)
        h = self.hello
        return SimStatus(
            connected=True,
            workers=[
                SimWorkerStatus(
                    worker_id=self.worker_id,
                    stores=h.stores,
                    robots=h.robots,
                    cameras=h.cameras,
                    streaming_camera_ids=list(self.streaming),
                )
            ],
        )

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler) if handler in self._handlers else None

    async def send(self, cmd, timeout_s: float | None = None) -> CommandResult:  # noqa: ANN001 (SimCommand union)
        if not self.connected:
            raise ConnectionError("loopback worker not connected")
        self.sent.append(cmd)
        timeout = default_timeout_s(cmd) if timeout_s is None else timeout_s
        task = asyncio.ensure_future(self._execute(cmd))
        self._inflight.add(task)
        try:
            return await asyncio.wait_for(task, timeout)
        except asyncio.CancelledError:
            if task in self._killed:
                raise ConnectionError("loopback worker disconnected mid-command") from None
            raise
        finally:
            self._inflight.discard(task)
            self._killed.discard(task)

    # ------------------------------------------------------------------------------------------------- internals
    async def _dispatch(self, msg: SimMessage) -> None:
        for h in list(self._handlers):
            await h(msg)

    async def _execute(self, cmd) -> CommandResult:  # noqa: ANN001
        r = self.robot
        rid = getattr(cmd, "robot_id", None)
        try:
            frame: CameraFrame | None = None
            instances: list[ItemInstance] = []
            iid = None
            if isinstance(cmd, NavigateCmd):
                await r.navigate(rid, cmd.waypoints)  # type: ignore[arg-type]
            elif isinstance(cmd, ScanCmd):
                frame = await r.scan(rid, cmd.slot_id)  # type: ignore[arg-type]
            elif isinstance(cmd, PickCmd):
                inst = await r.pick(rid, cmd.slot_id, cmd.sku, cmd.instance_id)  # type: ignore[arg-type]
                instances, iid = [inst], inst.instance_id
            elif isinstance(cmd, PlaceInBasketCmd):
                iid = r.snapshot(rid).holding  # type: ignore[arg-type]
                await r.place_in_basket(rid)  # type: ignore[arg-type]
            elif isinstance(cmd, PickFromBasketCmd):
                inst = await r.pick_from_basket(rid, cmd.sku, cmd.instance_id)  # type: ignore[arg-type]
                instances, iid = [inst], inst.instance_id
            elif isinstance(cmd, PlaceOnCounterCmd):
                iid = r.snapshot(rid).holding  # type: ignore[arg-type]
                await r.place_on_counter(rid, cmd.position, cmd.yaw, cmd.spot)  # type: ignore[arg-type]
            elif isinstance(cmd, UnloadBasketCmd):
                instances = await r.unload_basket(rid)  # type: ignore[arg-type]
            elif isinstance(cmd, CaptureCmd):
                frame = await (r.capture_camera(cmd.camera_id) if cmd.camera_id else r.capture(rid, cmd.camera))  # type: ignore[arg-type]
            elif isinstance(cmd, ListInstancesCmd):
                if rid is not None:
                    instances = await r.list_instances(rid, cmd.slot_id)
                else:
                    world = await r.world(cmd.store_id or "")
                    units = (
                        world.units(cmd.slot_id) if cmd.slot_id else [u for us in world.shelves.values() for u in us]
                    )
                    instances = [u.model_copy() for u in units]
            elif isinstance(cmd, ResetCmd):
                if rid is not None:
                    await r.reset(rid, cmd.store_id)
                else:
                    if not cmd.store_id:
                        raise RobotError("not_ready", "store-level reset needs store_id")
                    await r.reset_store(cmd.store_id)
                    world = await r.world(cmd.store_id)
                    self._stale.discard(cmd.store_id)
                    self._hashes[cmd.store_id] = planogram_hash(cmd.planogram) if cmd.planogram else world.data_hash
                    stores, robots, cams = await self._lists()
                    if self.hello is not None:
                        self.hello = self.hello.model_copy(update={"stores": stores, "robots": robots, "cameras": cams})
                    await self._dispatch(
                        SimHosted(worker_id=self.worker_id, stores=stores, robots=robots, cameras=cams)
                    )
            elif isinstance(cmd, StopCmd):
                for target in [rid] if rid else r.robot_ids():
                    await r.stop(target)
            elif isinstance(cmd, SetStreamsCmd):
                self.streaming, self.stream_fps = list(cmd.camera_ids), cmd.fps
            elif isinstance(cmd, SetSlotStockCmd):
                world = await r.world(cmd.store_id)
                instances = world.set_slot_stock(cmd.slot_id, cmd.qty, cmd.lots)
            else:
                return CommandResult(
                    cmd_id=cmd.cmd_id, robot_id=rid, ok=False, error_code="internal", error=f"unknown {cmd!r}"
                )
            state = r.snapshot(rid) if rid else None
            if state is not None:
                await self._dispatch(state)
            return CommandResult(
                cmd_id=cmd.cmd_id, robot_id=rid, ok=True, state=state, frame=frame, instances=instances, instance_id=iid
            )
        except RobotError as exc:
            state = None
            try:
                state = r.snapshot(rid) if rid else None
            except RobotError:
                pass
            return CommandResult(
                cmd_id=cmd.cmd_id, robot_id=rid, ok=False, error_code=exc.code, error=exc.message, state=state
            )


__all__ = ["STALE_HASH", "LoopbackSimLink"]
