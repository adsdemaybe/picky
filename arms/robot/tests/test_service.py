import math

import pytest

from arms.robot import FakeRobot, LoopbackSimLink, SimRobot, create
from arms.robot.tests.conftest import STORE
from contracts.common import Pose2D
from contracts.services import RobotService, Services
from contracts.sim import CommandResult, ResetCmd, RobotError, RobotState, RobotStatus, SimLink


def loopback(services: Services) -> tuple[SimRobot, LoopbackSimLink]:
    link = LoopbackSimLink(FakeRobot(services, realtime_factor=math.inf))
    services.sim_link = link
    return create(services), link


def test_conforms_to_protocol(services):
    robot = create(services)
    assert isinstance(robot, RobotService)
    assert isinstance(LoopbackSimLink(), SimLink)


async def test_disconnected_without_link_raises_and_reports(services):
    robot = create(services)
    assert (await robot.state()).status == RobotStatus.DISCONNECTED
    with pytest.raises(RobotError) as exc:
        await robot.navigate([Pose2D(x=1, y=1)])
    assert exc.value.code == "disconnected"
    errs = [e for e in services.events.events if e.type == "error"]
    assert errs and errs[0].arm == "robot" and errs[0].code == "disconnected"


async def test_full_round_trip_over_loopback(services):
    robot, link = loopback(services)
    st = await robot.reset(STORE)
    assert st.store_id == STORE
    reset_cmd = link.sent[0]
    assert isinstance(reset_cmd, ResetCmd) and reset_cmd.planogram is not None and reset_cmd.products
    assert len({c.cmd_id for c in link.sent}) == len(link.sent)

    st = await robot.navigate([Pose2D(x=5.0, y=3.2, theta=math.pi / 2)])
    assert (st.pose.x, st.pose.y) == (5.0, 3.2)
    frame = await robot.scan("S-avocado")
    assert len(frame.instances) == 5
    best = max(frame.instances, key=lambda i: i.freshness or 0)
    inst = await robot.pick("S-avocado", "avocado", best.instance_id)
    assert inst.instance_id == best.instance_id
    st = await robot.place_in_basket()
    assert st.basket == [best.instance_id]
    assert (await robot.state()).basket == [best.instance_id]  # cached from results / pushed states
    assert len(await robot.list_instances("S-avocado")) == 4
    assert (await robot.capture("basket")).instances[0].instance_id == best.instance_id


async def test_failed_result_maps_to_robot_error_code(services):
    robot, _ = loopback(services)
    await robot.reset(STORE)
    with pytest.raises(RobotError) as exc:
        await robot.pick("S-limes", "limes")
    assert exc.value.code == "no_instance"


class _TimeoutLink:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def is_connected(self) -> bool:
        return True

    def subscribe(self, handler):  # noqa: ANN001, ANN201
        return lambda: None

    async def send(self, cmd, timeout_s=None):  # noqa: ANN001, ANN201
        raise self.exc


@pytest.mark.parametrize(("exc", "code"), [(TimeoutError(), "timeout"), (ConnectionError("gone"), "disconnected")])
async def test_link_failures_become_error_events(services, exc, code):
    robot = SimRobot(services)
    robot.attach_link(_TimeoutLink(exc))
    robot.set_trip("trip_x")
    with pytest.raises(RobotError) as err:
        await robot.navigate([Pose2D(x=1, y=1)])
    assert err.value.code == code
    ev = services.events.events[-1]
    assert ev.type == "error" and ev.code == code and ev.trip_id == "trip_x"
    # busy flag released after the failure
    with pytest.raises(RobotError) as err:
        await robot.navigate([Pose2D(x=1, y=1)])
    assert err.value.code == code


async def test_unsolicited_states_are_cached_and_streamed(services):
    robot, link = loopback(services)
    stream = robot.states()
    handler = link._handlers[0]
    await handler(RobotState(store_id=STORE, pose=Pose2D(x=2, y=2), status=RobotStatus.NAVIGATING))
    got = await stream.__anext__()
    assert got.pose.x == 2 and got.status == RobotStatus.NAVIGATING
    assert (await robot.state()).pose.x == 2
    await stream.aclose()


async def test_ok_false_busy_passthrough(services):
    class BusyLink(_TimeoutLink):
        async def send(self, cmd, timeout_s=None):  # noqa: ANN001, ANN201
            return CommandResult(cmd_id=cmd.cmd_id, ok=False, error_code="busy", error="navigate running")

    robot = SimRobot(services, link=BusyLink(Exception()))
    with pytest.raises(RobotError) as err:
        await robot.scan("S-salsa")
    assert err.value.code == "busy"
