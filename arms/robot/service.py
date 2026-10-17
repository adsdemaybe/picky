"""SimRobot: the real ``RobotService``, driving the Isaac Sim worker over the gateway-provided ``SimLink``.

The link comes from ``services.sim_link`` (read at call time) or ``attach_link(link)``. Every method turns into one
``contracts.sim`` command with a fresh ``cmd_id``; ``SimLink.send`` correlates the ``CommandResult``. Failures:
- ``ok=False`` results -> ``RobotError(result.error_code)``;
- ``TimeoutError`` -> ``RobotError("timeout")`` + an ``error`` event;
- ``ConnectionError`` / no link -> ``RobotError("disconnected")`` + an ``error`` event (gateway swaps in the fake).
Unsolicited ``RobotState`` / ``CameraFrame`` / ``InstanceInfo`` messages are cached and fanned out to
``states()`` / ``frames()`` (subscription is set up lazily on first use, never in ``create()``).

TODO (spec items, CLAUDE.md §3 robot; the Isaac side lives in arms/robot/isaac + arms/sim):
 1. [P0] Validate against the live worker: navigate along a RouteLeg incl. final theta (pure pursuit in Isaac).
 2. [P0] Magic-grasp pick + place_in_basket round trip with ground-truth instance ids matching the fake's ids.
 3. [P1] Retry once on ``grasp_failed`` with instance_id=None (attach-on-contact fallback) before surfacing.
 4. [P1] Health check: if no RobotState for > 5 s while connected, report ``disconnected`` so the gateway falls back.
 5. [P2] RMPflow / cuMotion picking instead of magic grasp (Franka on a fixed base).
"""

import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from arms.robot._fanout import Fanout
from contracts.common import CameraName, InstanceId, Pose2D, Sku, SlotId, StoreId, TripId
from contracts.events import ErrorEvent
from contracts.planogram import Planogram
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
    SimLink,
    SimLog,
    SimMessage,
    StopCmd,
)
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)


class SimRobot:
    """RobotService over a SimLink. Construct via ``create(services)``; no I/O until the first call."""

    def __init__(self, services: Services | None = None, link: SimLink | None = None) -> None:
        self.services = services or Services()
        self._link = link
        self.trip_id: TripId | None = None
        self._subscribed_to: SimLink | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._state: RobotState | None = None
        self._instances: dict[InstanceId, ItemInstance] = {}
        self._motion: str | None = None
        self._state_fanout: Fanout[RobotState] = Fanout()
        self._frame_fanout: Fanout[CameraFrame] = Fanout(maxsize=4)

    # ----------------------------------------------------------------------------------------------- wiring helpers
    def attach_link(self, link: SimLink | None) -> None:
        """Use ``link`` instead of ``services.sim_link`` (gateway calls this when the worker (re)connects)."""
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
        self._unsubscribe, self._subscribed_to = None, None
        self._link = link

    def set_trip(self, trip_id: TripId | None) -> None:
        """Trip id stamped on ``error`` events this robot publishes."""
        self.trip_id = trip_id

    def is_connected(self) -> bool:
        link = self._current_link()
        return bool(link is not None and link.is_connected())

    # ----------------------------------------------------------------------------------------------- RobotService
    async def state(self) -> RobotState:
        self._ensure_subscribed()
        if self._state is not None:
            if not self.is_connected():
                return self._state.model_copy(update={"status": RobotStatus.DISCONNECTED})
            return self._state.model_copy()
        status = RobotStatus.IDLE if self.is_connected() else RobotStatus.DISCONNECTED
        return RobotState(pose=Pose2D(x=0.0, y=0.0), status=status)

    async def reset(self, store_id: StoreId) -> RobotState:
        plan, products = await self._scene_payload(store_id)
        res = await self._send(
            ResetCmd(cmd_id=new_id("cmd"), store_id=store_id, planogram=plan, products=products or None)
        )
        self._instances = {i.instance_id: i for i in res.instances}
        return self._state_from(res)

    async def navigate(self, waypoints: list[Pose2D]) -> RobotState:
        if not waypoints:
            raise RobotError("internal", "navigate needs at least one waypoint")
        res = await self._send(NavigateCmd(cmd_id=new_id("cmd"), waypoints=waypoints))
        return self._state_from(res)

    async def stop(self) -> RobotState:
        res = await self._send(StopCmd(cmd_id=new_id("cmd")))
        return self._state_from(res)

    async def scan(self, slot_id: SlotId) -> CameraFrame:
        res = await self._send(ScanCmd(cmd_id=new_id("cmd"), slot_id=slot_id))
        if res.frame is None:
            raise RobotError("internal", f"scan of {slot_id} returned no frame")
        return res.frame

    async def capture(self, camera: CameraName = "head") -> CameraFrame:
        res = await self._send(CaptureCmd(cmd_id=new_id("cmd"), camera=camera))
        if res.frame is None:
            raise RobotError("internal", f"capture({camera}) returned no frame")
        return res.frame

    async def list_instances(self, slot_id: SlotId | None = None) -> list[ItemInstance]:
        res = await self._send(ListInstancesCmd(cmd_id=new_id("cmd"), slot_id=slot_id))
        if slot_id is None:
            self._instances = {i.instance_id: i for i in res.instances}
        return [i.model_copy(update={"bbox": None}) for i in res.instances]

    async def pick(self, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None) -> ItemInstance:
        res = await self._send(PickCmd(cmd_id=new_id("cmd"), slot_id=slot_id, sku=sku, instance_id=instance_id))
        picked_id = res.instance_id or instance_id
        inst = next((i for i in res.instances if i.instance_id == picked_id), None)
        if inst is None and res.instances:
            inst = res.instances[0]
        if inst is None and picked_id and picked_id in self._instances:
            inst = self._instances[picked_id]
        if inst is None:
            raise RobotError("internal", f"pick on {slot_id} succeeded but returned no instance")
        return inst

    async def place_in_basket(self) -> RobotState:
        res = await self._send(PlaceInBasketCmd(cmd_id=new_id("cmd")))
        return self._state_from(res)

    def frames(self) -> AsyncIterator[CameraFrame]:
        self._ensure_subscribed()
        return self._frame_fanout.stream()

    def states(self) -> AsyncIterator[RobotState]:
        self._ensure_subscribed()
        return self._state_fanout.stream(initial=self._state)

    # ----------------------------------------------------------------------------------------------- internals
    def _current_link(self) -> SimLink | None:
        return self._link if self._link is not None else self.services.sim_link

    def _ensure_subscribed(self) -> None:
        link = self._current_link()
        if link is None or link is self._subscribed_to:
            return
        if self._unsubscribe is not None:
            with contextlib.suppress(Exception):
                self._unsubscribe()
        self._unsubscribe = link.subscribe(self._on_message)
        self._subscribed_to = link

    async def _on_message(self, msg: SimMessage) -> None:
        if isinstance(msg, RobotState):
            self._state = msg
            self._state_fanout.push(msg)
        elif isinstance(msg, CameraFrame):
            self._frame_fanout.push(msg)
        elif isinstance(msg, InstanceInfo):
            for inst in msg.instances:
                self._instances[inst.instance_id] = inst
        elif isinstance(msg, SimLog):
            log.log(logging.getLevelName(msg.level.upper()), "[sim] %s", msg.message)

    async def _send(self, cmd: Any) -> CommandResult:
        link = self._current_link()
        if link is None or not link.is_connected():
            await self._report("disconnected", f"sim worker not connected ({cmd.type})")
            raise RobotError("disconnected", "sim worker not connected")
        self._ensure_subscribed()
        is_motion = cmd.type in MOTION_COMMANDS
        if is_motion and self._motion is not None:
            raise RobotError("busy", f"{self._motion} in progress")
        if is_motion:
            self._motion = cmd.type
        try:
            res = await link.send(cmd)
        except TimeoutError as exc:
            await self._report("timeout", f"{cmd.type} ({cmd.cmd_id}) timed out")
            raise RobotError("timeout", f"{cmd.type} timed out") from exc
        except ConnectionError as exc:
            await self._report("disconnected", f"sim link dropped during {cmd.type}: {exc}")
            raise RobotError("disconnected", str(exc) or "sim link dropped") from exc
        finally:
            if is_motion:
                self._motion = None
        if res.state is not None:
            self._state = res.state
            self._state_fanout.push(res.state)
        if not res.ok:
            code = res.error_code or "internal"
            raise RobotError(code, res.error or f"{cmd.type} failed")
        return res

    def _state_from(self, res: CommandResult) -> RobotState:
        if res.state is not None:
            return res.state
        if self._state is not None:
            return self._state.model_copy()
        return RobotState(pose=Pose2D(x=0.0, y=0.0))

    async def _scene_payload(self, store_id: StoreId) -> tuple[Planogram | None, list]:
        """Planogram + products to push in ResetCmd (catalog first, data/ second, else let the worker use its own)."""
        catalog = self.services.catalog
        try:
            if catalog is not None:
                plan = await catalog.get_planogram(store_id)
                products = []
                for sku in sorted({s.sku for s in plan.slots}):
                    prod = await catalog.get_product(sku)
                    if prod is not None:
                        products.append(prod)
                return plan, products
            from utils.data import load_planogram, load_products

            plan = load_planogram(store_id)
            skus = {s.sku for s in plan.slots}
            return plan, [p for sku, p in load_products().items() if sku in skus]
        except KeyError as exc:
            raise RobotError("not_ready", f"unknown store {store_id!r}") from exc
        except Exception as exc:
            log.warning("no planogram to push for %s (%s); worker will build from its own data/", store_id, exc)
            return None, []

    async def _report(self, code: str, message: str) -> None:
        log.warning("robot error %s: %s", code, message)
        events = self.services.events
        if events is None:
            return
        try:
            await events.publish(
                ErrorEvent(trip_id=self.trip_id, arm="robot", message=message, code=code, source="robot")
            )
        except Exception as exc:
            log.warning("could not publish robot error event: %s", exc)


__all__ = ["SimRobot"]
