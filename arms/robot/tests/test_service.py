import asyncio
import math

import pytest

from arms.robot import FakeRobot, LoopbackSimLink, SimRobot, create
from arms.robot.tests.conftest import R1, R2, STORE, UP, fleet_opts
from contracts.common import Pose2D
from contracts.services import RobotService, Services
from contracts.sim import (
    CameraFrame,
    CommandResult,
    NavigateCmd,
    ResetCmd,
    RobotError,
    RobotStatus,
    SetSlotStockCmd,
    SetStreamsCmd,
    SimLink,
    SimStatus,
)

AT_AVO = Pose2D(x=5.0, y=3.62, theta=UP)


def real(services: Services, **kw) -> SimRobot:
    return create(services, **fleet_opts(**kw))


async def isaac(services: Services, *, stale: bool = False, **kw) -> tuple[SimRobot, LoopbackSimLink]:
    """SimRobot + a connected loopback "Isaac worker" (its own FakeRobot world) hosting the test store."""
    worker = FakeRobot(services, **fleet_opts())
    link = LoopbackSimLink(worker, stale_stores=[STORE] if stale else [])
    services.sim_link = link
    robot = real(services, **kw)
    await robot.list_robots()  # lazy subscribe
    await link.connect()
    await robot.refresh_backends()
    return robot, link


def test_conforms_to_protocol(services):
    robot = create(services)
    assert isinstance(robot, RobotService)
    assert isinstance(LoopbackSimLink(), SimLink)


async def test_without_a_worker_everything_runs_on_the_fake(services):
    robot = real(services)
    states = await robot.list_robots()
    assert [s.robot_id for s in states] == [R1, R2] and {s.backend for s in states} == {"fake"}
    st = await robot.navigate(R1, [Pose2D(x=3.0, y=1.0, theta=0.0)])
    assert st.pose.x == 3.0 and st.backend == "fake"
    status = robot.sim_status()
    assert status.fake_store_ids == [STORE] and status.isaac_store_ids == [] and status.fake_robot_ids == [R1, R2]
    assert not [e for e in services.events.events if e.type == "error"]  # no worker is not an error
    with pytest.raises(RobotError) as exc:
        await robot.state("nope")
    assert exc.value.code == "unknown_robot"


async def test_store_moves_to_isaac_and_commands_carry_robot_id(services):
    robot, link = await isaac(services)
    assert robot.backend(STORE) == "isaac"
    assert not any(isinstance(c, ResetCmd) for c in link.sent)  # hash matched: no rebuild
    st = await robot.navigate(R1, [Pose2D(x=5.0, y=1.0), AT_AVO])
    assert (st.pose.x, st.pose.y) == (5.0, 3.62) and st.backend == "isaac" and st.robot_id == R1
    nav = next(c for c in link.sent if isinstance(c, NavigateCmd))
    assert nav.robot_id == R1 and nav.speed_mps == 0.8
    assert len({c.cmd_id for c in link.sent}) == len(link.sent)
    assert st.battery_pct < 100.0  # owned by the robot arm, integrated from pose deltas

    frame = await robot.scan(R1, "S-avocado")
    assert frame.camera_id == f"{R1}.front" and len(frame.instances) == 5
    best = max(frame.instances, key=lambda i: i.freshness or 0)
    inst = await robot.pick(R1, "S-avocado", "avocado", best.instance_id)
    assert inst.instance_id == best.instance_id and inst.store_id == STORE
    st = await robot.place_in_basket(R1)
    assert st.basket == [best.instance_id]
    assert len(await robot.list_instances(R1, "S-avocado")) == 4
    assert (await robot.capture(R1, "basket")).instances[0].instance_id == best.instance_id
    ceiling = await robot.capture_camera(f"{STORE}.ceiling-1")
    assert ceiling.camera_id == f"{STORE}.ceiling-1" and ceiling.store_id == STORE
    unloaded = await robot.unload_basket(R1)
    assert [i.instance_id for i in unloaded] == [best.instance_id]
    status = robot.sim_status()
    assert status.connected and status.isaac_store_ids == [STORE] and status.isaac_robot_ids == [R1, R2]


async def test_stale_store_gets_a_store_level_reset_first(services):
    robot, link = await isaac(services, stale=True)
    resets = [c for c in link.sent if isinstance(c, ResetCmd)]
    assert len(resets) == 1
    r = resets[0]
    assert r.robot_id is None and r.store_id == STORE and r.planogram is not None and r.products
    assert {s.robot_id for s in r.robots or []} == {R1, R2} and r.cameras
    assert robot.backend(STORE) == "isaac"


async def test_busy_store_stays_on_the_fake_until_idle(services):
    worker = FakeRobot(services, **fleet_opts())
    link = LoopbackSimLink(worker)
    services.sim_link = link
    robot = real(services, strict_reach=False)
    await robot.pick(R1, "S-salsa", "salsa-mild")  # holding something on the fake
    await link.connect()
    assert await robot.refresh_backends() == {STORE: "fake"}
    await robot.place_in_basket(R1)
    await robot.unload_basket(R1)
    assert await robot.refresh_backends() == {STORE: "isaac"}


async def test_disconnect_mid_command_raises_once_then_continues_on_the_fake(services):
    robot, link = await isaac(services, realtime_factor=math.inf)
    link.robot.realtime_factor = 20.0  # slow "Isaac" so the navigate is in flight
    await robot.navigate(R1, [Pose2D(x=2.0, y=1.0, theta=0.0)])
    nav = asyncio.create_task(robot.navigate(R1, [Pose2D(x=8.0, y=1.0, theta=0.0)]))
    await asyncio.sleep(0.05)
    await link.disconnect()
    with pytest.raises(RobotError) as exc:
        await nav
    assert exc.value.code == "disconnected"
    assert robot.backend(STORE) == "fake"
    errs = [e for e in services.events.events if e.type == "error"]
    assert errs and errs[-1].code == "disconnected" and errs[-1].store_id == STORE
    st = await robot.state(R1)
    assert st.backend == "fake" and st.pose.x >= 2.0  # continues from its last Isaac pose, no reset
    st = await robot.navigate(R1, [Pose2D(x=8.0, y=1.0, theta=0.0)])
    assert st.pose.x == 8.0 and st.backend == "fake"


async def test_basket_survives_the_fallback(services):
    robot, link = await isaac(services)
    await robot.navigate(R1, [AT_AVO])
    picked = await robot.pick(R1, "S-avocado", "avocado")
    await robot.place_in_basket(R1)
    await link.disconnect()
    st = await robot.state(R1)  # next call notices the worker is gone
    await robot.list_robots()
    st = await robot.navigate(R1, [Pose2D(x=5.0, y=3.0, theta=0.0)])
    assert st.backend == "fake" and st.basket == [picked.instance_id]
    shelf = [i.instance_id for i in await robot.list_instances(R1, "S-avocado")]
    assert picked.instance_id not in shelf


class _Link:
    def __init__(self, exc: Exception | None = None, result: str | None = None) -> None:
        self.exc, self.code = exc, result
        self.handlers = []

    def is_connected(self, robot_id=None) -> bool:  # noqa: ANN001
        return True

    def subscribe(self, handler):  # noqa: ANN001, ANN201
        self.handlers.append(handler)
        return lambda: None

    def status(self) -> SimStatus:
        from contracts.sim import SimRobotInfo, SimStoreInfo, SimWorkerStatus

        return SimStatus(
            connected=True,
            workers=[
                SimWorkerStatus(
                    worker_id="w",
                    stores=[SimStoreInfo(store_id=STORE, data_hash=self.hash)],
                    robots=[SimRobotInfo(robot_id=R1, store_id=STORE), SimRobotInfo(robot_id=R2, store_id=STORE)],
                )
            ],
        )

    async def send(self, cmd, timeout_s=None):  # noqa: ANN001, ANN201
        if self.exc is not None and cmd.type == "navigate":
            raise self.exc
        if self.code is not None and cmd.type == "scan":
            return CommandResult(cmd_id=cmd.cmd_id, robot_id=cmd.robot_id, ok=False, error_code=self.code, error="x")
        return CommandResult(cmd_id=cmd.cmd_id, robot_id=getattr(cmd, "robot_id", None), ok=True)


async def _with(services: Services, link: _Link) -> SimRobot:
    from contracts.sim import planogram_hash

    link.hash = planogram_hash(services.catalog.plan)
    robot = SimRobot(services, link=link, **fleet_opts())
    assert await robot.refresh_backends() == {STORE: "isaac"}
    return robot


async def test_timeout_becomes_error_event_and_releases_busy(services):
    robot = await _with(services, _Link(TimeoutError()))
    robot.set_trip("trip_x")
    for _ in range(2):
        with pytest.raises(RobotError) as err:
            await robot.navigate(R1, [Pose2D(x=1, y=1)])
        assert err.value.code == "timeout"
    ev = services.events.events[-1]
    assert ev.type == "error" and ev.code == "timeout" and ev.trip_id == "trip_x" and ev.robot_id == R1


async def test_failed_result_maps_to_robot_error_code(services):
    robot = await _with(services, _Link(result="busy"))
    with pytest.raises(RobotError) as err:
        await robot.scan(R1, "S-salsa")
    assert err.value.code == "busy"
    robot2, link = await isaac(Services(catalog=services.catalog, events=services.events))  # type: ignore[arg-type]
    with pytest.raises(RobotError) as err:
        await robot2.pick(R1, "S-limes", "limes")
    assert err.value.code in ("no_instance", "grasp_failed")


async def test_per_robot_busy_on_isaac(services):
    robot, link = await isaac(services)
    link.robot.realtime_factor = 50.0
    nav = asyncio.create_task(robot.navigate(R1, [Pose2D(x=8.0, y=1.0, theta=0.0)]))
    await asyncio.sleep(0.02)
    with pytest.raises(RobotError) as err:
        await robot.scan(R1, "S-salsa")
    assert err.value.code == "busy"
    other = await robot.navigate(R2, [Pose2D(x=2.0, y=2.2, theta=0.0)])  # different robot: fine
    assert other.pose.x == 2.0
    await robot.stop(R1)
    with pytest.raises(RobotError) as err:
        await nav
    assert err.value.code == "cancelled"


async def test_unsolicited_states_and_frames_are_cached_and_streamed(services):
    robot, link = await isaac(services)
    states = robot.states(R2)
    first = await states.__anext__()
    assert first.robot_id == R2 and first.backend == "isaac"
    await link.push(link.robot.snapshot(R2).model_copy(update={"pose": Pose2D(x=2, y=2.2), "status": "navigating"}))
    got = await asyncio.wait_for(states.__anext__(), 1.0)
    assert got.pose.x == 2 and got.status == RobotStatus.NAVIGATING and got.backend == "isaac"
    assert (await robot.state(R2)).pose.x == 2
    await states.aclose()

    frames = robot.frames(camera_id=f"{R1}.wrist")
    await asyncio.sleep(0.01)
    streams = [c for c in link.sent if isinstance(c, SetStreamsCmd)]
    assert streams and streams[-1].camera_ids == [f"{R1}.wrist"] and streams[-1].fps <= 15
    raw = await link.robot.capture(R1, "wrist")
    await link.push(raw)
    f = await asyncio.wait_for(frames.__anext__(), 1.0)
    assert isinstance(f, CameraFrame) and f.camera_id == f"{R1}.wrist" and f.instances == []
    await frames.aclose()
    await asyncio.sleep(0.01)
    assert [c for c in link.sent if isinstance(c, SetStreamsCmd)][-1].camera_ids == []


async def test_sync_stock_updates_isaac_and_the_fallback_world(services):
    robot, link = await isaac(services)
    await robot.fake.world(STORE)
    services.catalog.plan.slot("S-salsa").qty = 7
    await robot.sync_stock(STORE, ["S-salsa"])
    cmds = [c for c in link.sent if isinstance(c, SetSlotStockCmd)]
    assert [(c.slot_id, c.qty) for c in cmds] == [("S-salsa", 7)]
    assert len(await link.robot.list_instances(R1, "S-salsa")) == 7  # the "Isaac" scene
    assert len((await robot.fake.world(STORE)).units("S-salsa")) == 7  # the fallback world


async def test_reset_on_isaac_restores_battery_and_dock(services):
    robot, link = await isaac(services)
    await robot.navigate(R1, [Pose2D(x=4.0, y=1.0, theta=0.0)])
    st = await robot.reset(R1)
    assert (st.pose.x, st.pose.y) == (1.0, 1.0) and st.battery_pct == pytest.approx(100.0, abs=0.1)
    reset = [c for c in link.sent if isinstance(c, ResetCmd)][-1]
    assert reset.robot_id == R1 and reset.store_id == STORE
    with pytest.raises(RobotError) as err:
        await robot.reset(R1, "store_other")
    assert err.value.code == "unknown_robot"


async def test_frames_from_the_worker_are_never_marked_blurred(services):
    """A worker (or later the physical robot) claiming blurred=true must not skip perception's face blur (§8)."""
    robot, link = await isaac(services)
    frames = robot.frames(camera_id=f"{R1}.front")
    await asyncio.sleep(0.01)
    raw = await link.robot.capture(R1, "front")
    await link.push(raw.model_copy(update={"blurred": True}))
    f = await asyncio.wait_for(frames.__anext__(), 1.0)
    assert f.camera_id == f"{R1}.front" and f.blurred is False
    await frames.aclose()
    calls = []
    original_camera, original = link.robot.capture_camera, link.robot.capture

    async def blurred(fn, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        calls.append(a)
        return (await fn(*a, **kw)).model_copy(update={"blurred": True})

    link.robot.capture_camera = lambda *a, **kw: blurred(original_camera, *a, **kw)  # type: ignore[method-assign]
    link.robot.capture = lambda *a, **kw: blurred(original, *a, **kw)  # type: ignore[method-assign]
    got = await robot.capture(R1, "front")
    assert calls, "the Isaac worker (loopback) answered the capture command"
    assert got.blurred is False
