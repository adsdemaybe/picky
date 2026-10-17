import math

import pytest

from arms.sim.tests.sim_fixtures import planogram, products
from arms.sim.worker import NullBackend, WorkerCore, head_camera, project_box
from contracts.common import Point3, Pose2D
from contracts.sim import (
    CaptureCmd,
    CommandResult,
    InstanceInfo,
    ListInstancesCmd,
    NavigateCmd,
    PickCmd,
    PlaceInBasketCmd,
    ResetCmd,
    RobotState,
    RobotStatus,
    ScanCmd,
    StopCmd,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig():
    out: list = []
    clock = Clock()
    core = WorkerCore(
        NullBackend(), out.append, "store_test", clock=clock, loader=lambda sid, seed: (planogram(), products())
    )
    return core, out, clock


def results(out) -> dict[str, CommandResult]:
    return {m.cmd_id: m for m in out if isinstance(m, CommandResult)}


def run_until_done(core, out, clock, cmd_id, max_s=120.0, dt=0.05):
    t = 0.0
    while cmd_id not in results(out) and t < max_s:
        clock.t += dt
        t += dt
        core.tick(dt)
    return results(out)[cmd_id]


def test_not_ready_before_reset(rig):
    core, out, _ = rig
    core.submit(NavigateCmd(cmd_id="c1", waypoints=[Pose2D(x=3, y=3)]))
    assert results(out)["c1"].error_code == "not_ready"


def test_reset_emits_instances_and_home_state(rig):
    core, out, _ = rig
    core.submit(ResetCmd(cmd_id="r"))
    res = results(out)["r"]
    assert res.ok and res.state.pose == planogram().entrance
    info = next(m for m in out if isinstance(m, InstanceInfo))
    assert len(info.instances) == 15
    assert core.hello("tok").data_hash == core.spec.data_hash


def test_navigate_reaches_final_pose_and_theta(rig):
    core, out, clock = rig
    core.submit(ResetCmd(cmd_id="r"))
    goal = Pose2D(x=3.3, y=5.0, theta=math.pi)
    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=3.3, y=2.0), goal]))
    assert core.status == RobotStatus.NAVIGATING
    res = run_until_done(core, out, clock, "n")
    assert res.ok, res.error
    assert res.state.pose.distance_to(goal) <= 0.05
    assert abs(math.remainder(res.state.pose.theta - goal.theta, 2 * math.pi)) <= 0.05
    assert res.state.status == RobotStatus.IDLE
    assert sum(isinstance(m, RobotState) for m in out) > 3  # state stream while moving


def test_busy_and_stop_cancel(rig):
    core, out, clock = rig
    core.submit(ResetCmd(cmd_id="r"))
    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=12, y=20)]))
    core.submit(ScanCmd(cmd_id="s", slot_id="s_salsa"))
    assert results(out)["s"].error_code == "busy"
    core.submit(CaptureCmd(cmd_id="cap"))  # always allowed
    assert results(out)["cap"].ok and results(out)["cap"].frame.jpeg_b64
    core.submit(StopCmd(cmd_id="stop"))
    assert results(out)["n"].error_code == "cancelled"
    assert results(out)["stop"].ok
    assert core.active is None


def test_navigate_outside_store_is_path_blocked(rig):
    core, out, _ = rig
    core.submit(ResetCmd(cmd_id="r"))
    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=50, y=5)]))
    assert results(out)["n"].error_code == "path_blocked"


def test_full_pick_flow(rig):
    core, out, clock = rig
    core.submit(ResetCmd(cmd_id="r"))
    core.submit(ScanCmd(cmd_id="bad", slot_id="nope"))
    assert results(out)["bad"].error_code == "unknown_slot"
    core.submit(PickCmd(cmd_id="far", slot_id="s_salsa", sku="salsa_hot"))
    assert results(out)["far"].error_code == "grasp_failed"  # not at the approach pose yet

    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=3.3, y=5.0, theta=math.pi)]))
    assert run_until_done(core, out, clock, "n").ok

    core.submit(ScanCmd(cmd_id="scan", slot_id="s_salsa"))
    scan = run_until_done(core, out, clock, "scan")
    assert scan.ok and scan.frame.slot_id == "s_salsa"
    seen = scan.frame.instances
    assert seen and all(i.slot_id == "s_salsa" and i.bbox is not None for i in seen)
    for i in seen:
        assert 0 <= i.bbox.x and i.bbox.x + i.bbox.w <= scan.frame.w + 1e-6
        assert 0 <= i.bbox.y and i.bbox.y + i.bbox.h <= scan.frame.h + 1e-6

    core.submit(PickCmd(cmd_id="p", slot_id="s_salsa", sku="salsa_hot"))
    pick = run_until_done(core, out, clock, "p")
    assert pick.ok and pick.instance_id and pick.state.holding == pick.instance_id

    core.submit(PlaceInBasketCmd(cmd_id="pl"))
    place = run_until_done(core, out, clock, "pl")
    assert place.ok
    assert place.state.basket == [pick.instance_id] and place.state.basket_skus == ["salsa_hot"]
    assert place.state.holding is None
    assert core.instances[pick.instance_id].in_basket

    core.submit(ListInstancesCmd(cmd_id="li", slot_id="s_salsa"))
    assert len(results(out)["li"].instances) == 6

    # basket rides with the robot
    before = core.instances[pick.instance_id].position
    core.submit(NavigateCmd(cmd_id="n2", waypoints=[Pose2D(x=3.3, y=8.0, theta=math.pi)]))
    run_until_done(core, out, clock, "n2")
    assert core.instances[pick.instance_id].position.y > before.y + 2


def test_pick_specific_instance_and_empty_slot(rig):
    core, out, clock = rig
    core.submit(ResetCmd(cmd_id="r"))
    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=11.0, y=14.2, theta=math.pi / 2)]))
    assert run_until_done(core, out, clock, "n").ok
    core.submit(PickCmd(cmd_id="p", slot_id="s_avo", sku="avocado", instance_id="s_avo#04"))
    assert run_until_done(core, out, clock, "p").instance_id == "s_avo#04"
    core.submit(PlaceInBasketCmd(cmd_id="x", instance_id="other"))
    assert results(out)["x"].error_code == "no_instance"
    core.submit(PickCmd(cmd_id="p2", slot_id="s_avo", sku="beans"))
    assert results(out)["p2"].error_code == "grasp_failed"  # still holding


def test_no_instance_in_oos_slot(rig):
    core, out, clock = rig
    core.submit(ResetCmd(cmd_id="r"))
    core.submit(NavigateCmd(cmd_id="n", waypoints=[Pose2D(x=7.8, y=6.0, theta=math.pi)]))
    assert run_until_done(core, out, clock, "n").ok
    core.submit(PickCmd(cmd_id="p", slot_id="s_beans_e", sku="beans"))
    assert results(out)["p"].error_code == "no_instance"


def test_reset_with_pushed_planogram(rig):
    core, out, _ = rig
    core.submit(ResetCmd(cmd_id="r", planogram=planogram(), products=products(), seed=3))
    assert results(out)["r"].ok and core.spec.seed == 3


def test_projection_centered_box_is_centered():
    cam = head_camera(Pose2D(x=0, y=0, theta=0), Point3(x=2.0, y=0.0, z=1.35))
    bbox = project_box(cam, Point3(x=2.0, y=0.0, z=1.35), (0.1, 0.1, 0.1), 0.0)
    assert bbox is not None
    assert bbox.x + bbox.w / 2 == pytest.approx(cam.w / 2, abs=1)
    assert bbox.y + bbox.h / 2 == pytest.approx(cam.h / 2, abs=1)
    assert project_box(cam, Point3(x=-2.0, y=0.0, z=1.35), (0.1, 0.1, 0.1), 0.0) is None  # behind
    left = project_box(cam, Point3(x=2.0, y=0.3, z=1.35), (0.1, 0.1, 0.1), 0.0)
    assert left.x < bbox.x  # +y is to the left when facing +x
