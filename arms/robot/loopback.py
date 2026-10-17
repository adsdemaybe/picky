"""LoopbackSimLink: a ``contracts.sim.SimLink`` that executes commands on an in-process ``FakeRobot``.

Lets the REAL ``SimRobot`` (and the gateway's /ws/sim plumbing tests) run without a GPU box:
``SimRobot(services, link=LoopbackSimLink(FakeRobot(...)))``. It also documents the worker-side semantics the Isaac
worker must match: exactly one CommandResult per cmd_id, ``error_code`` on failure, ``busy`` while a motion runs.
"""

import asyncio
from collections.abc import Callable

from arms.robot.fake import FakeRobot
from contracts.sim import (
    CaptureCmd,
    CommandResult,
    ListInstancesCmd,
    NavigateCmd,
    PickCmd,
    PlaceInBasketCmd,
    ResetCmd,
    RobotError,
    ScanCmd,
    SimMessageHandler,
    StopCmd,
    default_timeout_s,
)


class LoopbackSimLink:
    def __init__(self, robot: FakeRobot | None = None, connected: bool = True) -> None:
        self.robot = robot or FakeRobot(realtime_factor=float("inf"))
        self.connected = connected
        self.sent: list[object] = []
        self._handlers: list[SimMessageHandler] = []

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler) if handler in self._handlers else None

    async def send(self, cmd, timeout_s: float | None = None) -> CommandResult:  # noqa: ANN001 (SimCommand union)
        if not self.connected:
            raise ConnectionError("loopback link disconnected")
        self.sent.append(cmd)
        timeout = default_timeout_s(cmd) if timeout_s is None else timeout_s
        return await asyncio.wait_for(self._execute(cmd), timeout)

    async def _execute(self, cmd) -> CommandResult:  # noqa: ANN001
        r = self.robot
        try:
            frame, instances, instance_id = None, [], None
            if isinstance(cmd, ResetCmd):
                await r.reset(cmd.store_id or "store_ave")
                instances = await r.list_instances()
            elif isinstance(cmd, NavigateCmd):
                await r.navigate(cmd.waypoints)
            elif isinstance(cmd, ScanCmd):
                frame = await r.scan(cmd.slot_id)
            elif isinstance(cmd, CaptureCmd):
                frame = await r.capture(cmd.camera)
            elif isinstance(cmd, ListInstancesCmd):
                instances = await r.list_instances(cmd.slot_id)
            elif isinstance(cmd, PickCmd):
                inst = await r.pick(cmd.slot_id, cmd.sku, cmd.instance_id)
                instances, instance_id = [inst], inst.instance_id
            elif isinstance(cmd, PlaceInBasketCmd):
                state = await r.state()
                instance_id = state.holding
                await r.place_in_basket()
            elif isinstance(cmd, StopCmd):
                await r.stop()
            else:
                return CommandResult(cmd_id=cmd.cmd_id, ok=False, error_code="internal", error=f"unknown {cmd!r}")
            state = await r.state()
            for h in list(self._handlers):
                await h(state)
            return CommandResult(
                cmd_id=cmd.cmd_id, ok=True, state=state, frame=frame, instances=instances, instance_id=instance_id
            )
        except RobotError as exc:
            return CommandResult(
                cmd_id=cmd.cmd_id, ok=False, error_code=exc.code, error=exc.message, state=await r.state()
            )
