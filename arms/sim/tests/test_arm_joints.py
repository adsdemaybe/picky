"""Joint-level arm control (contracts 0.2.2 ArmJointsCmd) on the worker core: joint mode, policy cameras, the sticky
grasp, and parity of the policy cameras with the dataset recorder. NullBackend + the synthetic SO-101 (no Isaac)."""

import base64
import math

import pytest

from arms.sim import arm_joints as aj
from arms.sim import rover_puppet as rp
from arms.sim.tests.sim_fixtures import loader, robots_loader
from arms.sim.tests.test_worker import R1, SALSA_AP, Clock, drive, results, run_until
from arms.sim.worker import NullBackend, WorkerCore
from contracts.common import Point3, Pose2D
from contracts.sim import (
    ARM_JOINTS_CAPABILITY,
    ArmJointsCmd,
    NavigateCmd,
    PickCmd,
    PlaceInBasketCmd,
    StopCmd,
    command_error_result,
    parse_gateway_to_sim,
    parse_sim_message,
)

OPEN, CLOSED = 40.0, -10.0


@pytest.fixture
def rig():
    out: list = []
    clock = Clock()
    backend = NullBackend()
    core = WorkerCore(backend, out.append, clock=clock, loader=loader, robots_loader=robots_loader)
    core.load_stores(["store_test", "store_two"])
    return core, out, clock, backend


def arm(core, out, clock, cmd_id, targets=(), **kw):
    core.submit(ArmJointsCmd(cmd_id=cmd_id, robot_id=R1, targets=[list(t) for t in targets], **kw))
    return run_until(core, out, clock, cmd_id, max_s=60.0, dt=1 / 60)


def reach_q(core, rover, point: Point3, lift: float, grip: float, seed=None) -> list[float]:
    """Joint degrees that put the synthetic arm's TCP at a store point (level gripper)."""
    kin = core.kinematics(rover)
    c, s = math.cos(rover.pose.theta), math.sin(rover.pose.theta)
    dx, dy = point.x - rover.pose.x, point.y - rover.pose.y
    target = (c * dx + s * dy, -s * dx + c * dy, point.z)
    res = rp.solve_reach(kin.chain, target, q0=seed or {"shoulder_lift": 0.3, "elbow_flex": 0.3}, base=kin.base(lift))
    assert res.error_m < 0.01, res.error_m
    q = rp.q_deg(res.q)
    q[5] = grip
    return q


def lerp(a: list[float], b: list[float], n: int) -> list[list[float]]:
    return [[x + (y - x) * (i + 1) / n for x, y in zip(a, b, strict=True)] for i in range(n)]


def at_salsa(core, out, clock):
    drive(core, out, clock, R1, SALSA_AP)
    rover = core.rovers[R1]
    unit = core.stores["store_test"].instances["s_salsa#00"]
    return rover, unit


def test_capability_and_wire_round_trip(rig):
    core, _, _, _ = rig
    hello = core.hello("tok")
    assert ARM_JOINTS_CAPABILITY in hello.capabilities and "camera:policy_side" in hello.capabilities
    cmd = parse_gateway_to_sim({"type": "arm_joints", "cmd_id": "a", "robot_id": R1, "targets": [[0] * 6] * 3})
    assert isinstance(cmd, ArmJointsCmd) and cmd.hz == 30.0 and cmd.capture == ["side", "wrist"]
    bad = {"type": "arm_joints", "cmd_id": "b", "robot_id": R1, "targets": [[0, 0, 0]]}
    with pytest.raises(ValueError):
        parse_gateway_to_sim(bad)
    err = command_error_result(bad, ValueError("x"))
    assert err is not None and not err.ok and err.cmd_id == "b"


def test_enter_joint_mode_returns_state_and_both_policy_frames(rig):
    core, out, clock, backend = rig
    rover, unit = at_salsa(core, out, clock)
    res = arm(core, out, clock, "a0", lift_height_m=unit.position.z)
    assert res.ok, res.error
    assert res.arm is not None and res.arm.joint_mode and len(res.arm.q_deg) == 6
    assert abs(res.arm.lift_height_m - unit.position.z) < 1e-6 and abs(rover.lift - unit.position.z) < 1e-6
    assert [f.camera for f in res.policy_frames] == ["side", "wrist"]
    assert all((f.w, f.h) == (640, 480) and f.step == 0 and not f.blurred for f in res.policy_frames)
    assert base64.b64decode(res.policy_frames[0].jpeg_b64)[:2] == b"\xff\xd8"
    assert (f"{R1}.policy_side", "store_test") in backend.renders
    assert res.state is not None and res.state.holding is None
    # parsed back on the gateway side (frames + arm survive the lenient parser)
    back = parse_sim_message(res.model_dump_json())
    assert back.arm.q_deg == res.arm.q_deg and len(back.policy_frames) == 2


def test_policy_cameras_follow_the_arm(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    arm(core, out, clock, "a0", lift_height_m=unit.position.z, capture=[])
    kin = core.kinematics(rover)
    q0 = list(rover.arm_q)
    w0 = aj.wrist_camera_pose(kin, rover.pose, rover.lift, rp.q_rad(q0))
    s0 = aj.side_camera_pose(kin, rover.pose, rover.lift)
    q1 = reach_q(core, rover, unit.position, rover.lift, OPEN)
    res = arm(core, out, clock, "a1", lerp(q0, q1, 10))
    assert res.ok and res.arm.steps_applied == 10
    w1 = aj.wrist_camera_pose(kin, rover.pose, rover.lift, rp.q_rad(rover.arm_q))
    s1 = aj.side_camera_pose(kin, rover.pose, rover.lift)
    assert math.dist((w0.x, w0.y, w0.z), (w1.x, w1.y, w1.z)) > 0.05  # the wrist camera rides the gripper
    assert (s0.x, s0.y, s0.z, s0.yaw) == (s1.x, s1.y, s1.z, s1.yaw)  # the side camera rides the lift only
    side = res.policy_frames[0].camera_pose
    assert side is not None and abs(side.hfov_deg - 72.0) < 1e-6
    # the side camera looks at the reach workspace in front of the rover: the unit projects into its image
    assert s1.project((unit.position.x, unit.position.y, unit.position.z)) is not None


def test_sticky_grasp_attaches_on_close_and_t0_place_takes_over(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    lift = unit.position.z
    arm(core, out, clock, "a0", lift_height_m=lift, capture=[])
    q0 = list(rover.arm_q)
    q_open = [*q0[:5], OPEN]
    q_at = reach_q(core, rover, unit.position, lift, OPEN)
    q_shut = [*q_at[:5], CLOSED]
    res = arm(core, out, clock, "a1", [*lerp(q0, q_open, 5), *lerp(q_open, q_at, 20)])
    assert res.ok and res.arm.attached is None and rover.holding is None
    res = arm(core, out, clock, "a2", lerp(q_at, q_shut, 8))
    assert res.ok and res.arm.events == [f"sticky_grasp:{unit.instance_id}"]
    assert res.arm.attached == unit.instance_id == res.instance_id == res.state.holding
    assert res.arm.gripper_closed
    assert any("sticky grasp (physics substitute)" in getattr(m, "message", "") for m in out)
    # the unit rides rigidly with the gripper: lift the TCP 5 cm and it follows
    above = Point3(x=unit.position.x, y=unit.position.y, z=unit.position.z + 0.05)
    q_up = reach_q(core, rover, above, lift, CLOSED, seed=rp.q_rad(q_shut))
    arm(core, out, clock, "a3", lerp(q_shut, q_up, 10), capture=[])
    now = core.stores["store_test"].instances[unit.instance_id].position
    assert now.z == pytest.approx(unit.position.z + 0.05, abs=0.01)
    # end: the scripted rig takes the arm back with the unit still held; T0 place puts it into the basket
    res = arm(core, out, clock, "a4", end=True, capture=[])
    assert res.ok and not res.arm.joint_mode and rover.arm_q is None and rover.holding == unit.instance_id
    core.submit(PlaceInBasketCmd(cmd_id="pl", robot_id=R1))
    assert run_until(core, out, clock, "pl").ok and rover.basket == [unit.instance_id]


def test_closing_on_air_attaches_nothing(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    arm(core, out, clock, "a0", lift_height_m=unit.position.z, capture=[])
    q0 = list(rover.arm_q)
    res = arm(core, out, clock, "a1", [*lerp(q0, [*q0[:5], OPEN], 4), *lerp([*q0[:5], OPEN], [*q0[:5], CLOSED], 4)])
    assert res.ok and res.arm.events == ["close_empty"] and res.arm.attached is None and rover.holding is None


def test_opening_puts_back_near_its_spot_and_drops_elsewhere(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    lift = unit.position.z
    arm(core, out, clock, "a0", lift_height_m=lift, capture=[])
    q_at = reach_q(core, rover, unit.position, lift, OPEN)
    arm(core, out, clock, "a1", [*lerp(list(rover.arm_q), q_at, 10), [*q_at[:5], CLOSED]], capture=[])
    assert rover.holding == unit.instance_id
    res = arm(core, out, clock, "a2", [[*q_at[:5], OPEN]], capture=[])
    assert res.arm.events == [f"put_back:{unit.instance_id}"] and rover.holding is None
    assert core.stores["store_test"].instances[unit.instance_id].position == unit.position
    # grab again, swing the arm sideways (away from the shelf spot) and open: it lands on the floor
    arm(core, out, clock, "a3", [[*q_at[:5], CLOSED]], capture=[])
    assert rover.holding == unit.instance_id
    q_side = [q_at[0] + 60.0, *q_at[1:5], CLOSED]
    arm(core, out, clock, "a4", lerp([*q_at[:5], CLOSED], q_side, 10), capture=[])
    res = arm(core, out, clock, "a5", [[*q_side[:5], OPEN]], capture=[])
    assert res.arm.events == [f"drop:{unit.instance_id}"]
    dropped = core.stores["store_test"].instances[unit.instance_id]
    assert dropped.position.z == pytest.approx(core.stores["store_test"].item_dims(unit.instance_id)[2] / 2)


def test_release_over_the_basket(rig, monkeypatch):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    lift = unit.position.z
    arm(core, out, clock, "a0", lift_height_m=lift, capture=[])
    q_at = reach_q(core, rover, unit.position, lift, OPEN)
    arm(core, out, clock, "a1", [*lerp(list(rover.arm_q), q_at, 10), [*q_at[:5], CLOSED]], capture=[])
    monkeypatch.setattr(aj, "over_basket", lambda pose, p: True)
    res = arm(core, out, clock, "a2", [[*q_at[:5], OPEN]], capture=[])
    assert res.arm.events == [f"release_basket:{unit.instance_id}"]
    assert rover.basket == [unit.instance_id] and core.stores["store_test"].instances[unit.instance_id].in_basket


def test_over_basket_geometry():
    pose = Pose2D(x=1.0, y=2.0, theta=math.pi / 2)  # facing +y: the basket (robot x = -0.12) is at y = 1.88
    assert aj.over_basket(pose, Point3(x=1.0, y=1.88, z=0.6))
    assert not aj.over_basket(pose, Point3(x=1.0, y=2.3, z=0.6))
    assert not aj.over_basket(pose, Point3(x=1.0, y=1.88, z=0.2))
    assert aj.point_box_distance((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.1, 0.1, 0.1), 0.3) == 0.0
    assert aj.point_box_distance((0.1, 0.0, 0.0), (0.0, 0.0, 0.0), (0.1, 0.1, 0.1), 0.0) == pytest.approx(0.05)


def test_rows_play_at_hz_and_other_motions_leave_joint_mode(rig):
    core, out, clock, backend = rig
    rover, unit = at_salsa(core, out, clock)
    arm(core, out, clock, "a0", lift_height_m=unit.position.z, capture=[])
    q0 = list(rover.arm_q)
    rows = lerp(q0, [q0[0] + 20.0, *q0[1:]], 15)
    core.submit(ArmJointsCmd(cmd_id="a1", robot_id=R1, targets=rows, hz=30.0, capture=[]))
    t = 0.0
    while "a1" not in results(out):
        clock.t += 1 / 60
        t += 1 / 60
        core.tick(1 / 60)
        assert t < 5
    assert t == pytest.approx(15 / 30, abs=2 / 60)  # 15 rows at 30 Hz = 0.5 s
    assert backend.joints[R1] == pytest.approx(rows[-1])
    core.submit(NavigateCmd(cmd_id="nav", robot_id=R1, waypoints=[SALSA_AP]))
    assert run_until(core, out, clock, "nav").ok
    assert rover.arm_q is None  # the scripted rig owns the arm again


def test_busy_stop_battery(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    arm(core, out, clock, "a0", lift_height_m=unit.position.z, capture=[])
    q0 = list(rover.arm_q)
    core.submit(ArmJointsCmd(cmd_id="long", robot_id=R1, targets=[q0] * 50, hz=10.0, capture=[]))
    core.submit(ArmJointsCmd(cmd_id="second", robot_id=R1, targets=[q0], capture=[]))
    assert results(out)["second"].error_code == "busy"
    core.submit(StopCmd(cmd_id="stop", robot_id=R1))
    assert results(out)["long"].error_code == "cancelled" and rover.arm_q is not None  # holds its pose
    rover.battery = 1.0
    core.submit(ArmJointsCmd(cmd_id="low", robot_id=R1, targets=[q0], capture=[]))
    assert results(out)["low"].error_code == "battery_low"
    core.submit(ArmJointsCmd(cmd_id="look", robot_id=R1, capture=["wrist"]))  # observe only: still allowed
    assert run_until(core, out, clock, "look").ok
    core.submit(ArmJointsCmd(cmd_id="ghost", robot_id="nope", capture=[]))
    assert results(out)["ghost"].error_code == "unknown_robot"


def test_capture_each_step_records_every_row(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    arm(core, out, clock, "a0", lift_height_m=unit.position.z, capture=[])
    q0 = list(rover.arm_q)
    res = arm(core, out, clock, "a1", lerp(q0, [q0[0] + 5, *q0[1:]], 4), capture=["side"], capture_each_step=True)
    assert [f.step for f in res.policy_frames] == [0, 1, 2, 3, 4]


def test_entering_joint_mode_while_holding_keeps_the_unit_in_the_jaws(rig):
    core, out, clock, _ = rig
    rover, unit = at_salsa(core, out, clock)
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    assert run_until(core, out, clock, "p").ok and rover.holding
    held = rover.holding
    before = core.stores["store_test"].instances[held].position
    res = arm(core, out, clock, "a0", capture=[])
    assert res.ok and res.arm.attached == held
    after = core.stores["store_test"].instances[held].position
    assert math.dist((before.x, before.y, before.z), (after.x, after.y, after.z)) < 1e-6


def test_policy_cameras_match_the_dataset_recorder():
    """The worker's policy cameras must be the recorder's (a policy trained on its frames sees the same views)."""
    plan = pytest.importorskip("arms.sim.tools.record_lerobot.plan")
    if not all(hasattr(plan, n) for n in ("PickPlanner", "side_camera_matrix", "wrist_camera_matrix")):
        pytest.skip("recorder API changed")
    syn = rp.synthetic_kinematics()
    kin = rp.ArmKinematics(chain=syn.chain, model=rp.rot_z(math.pi / 2), dx=syn.dx, z_ref=0.42, lift_mode="offset")
    planner = plan.PickPlanner({"chain": kin.chain, "M": kin.model, "dx": kin.dx, "z_ref": kin.z_ref})
    q = {"shoulder_pan": 0.2, "shoulder_lift": 0.4, "elbow_flex": 0.5, "wrist_flex": -0.3, "wrist_roll": 1.2}
    for lift in (0.45, 0.8, 1.3):
        dz = rp.lift_offset(lift, kin.z_ref)
        for mine, theirs in (
            (aj.side_camera_matrix(kin, lift), plan.side_camera_matrix(planner, dz)),
            (aj.wrist_camera_matrix(kin, lift, q), plan.wrist_camera_matrix(planner, q, dz)),
        ):
            assert (
                max(abs(a - b) for ra, rb in zip(mine, theirs, strict=True) for a, b in zip(ra, rb, strict=True)) < 1e-9
            )
    assert aj.SIDE_HFOV == pytest.approx(math.radians(plan.SIDE_HFOV_DEG))
    assert aj.WRIST_HFOV == pytest.approx(math.radians(plan.WRIST_HFOV_DEG))


def test_camera_pose_roll_round_trip():
    from arms.sim.geometry import CameraPose

    cam = CameraPose(1.0, 2.0, 1.5, 0.7, -0.4, roll=0.3)
    r, _, f = cam.basis()
    back = CameraPose.from_axes((1.0, 2.0, 1.5), f, r)
    assert (back.yaw, back.pitch, back.roll) == pytest.approx((0.7, -0.4, 0.3))
    assert back.project((2.0, 2.5, 1.2)) == pytest.approx(cam.project((2.0, 2.5, 1.2)))
    assert CameraPose(0, 0, 1, 0.0, 0.0).basis() == CameraPose(0, 0, 1, 0.0, 0.0, roll=0.0).basis()


def test_isaac_style_kinematics_scripted_pose_matches_the_puppet():
    """Offset-mode kinematics with a reach table reproduce the scripted puppet pose formula exactly."""
    syn = rp.synthetic_kinematics()
    z_ref = 0.42

    def target(ext: float, drop: float) -> rp.Vec3:
        return (0.12 + ext, 0.0, z_ref - drop)

    table = rp.ReachTable.build(
        syn.chain, target, (0.12, 0.36), drops=(0.0, 0.1), samples=4, base=syn.base_at_travel(0)
    )
    kin = rp.ArmKinematics(chain=syn.chain, dx=syn.dx, z_ref=z_ref, table=table, lift_mode="offset")
    q = kin.scripted_q(0.6, 0.2, closed=False)
    expect = table.config(0.2, 0.0)
    assert q["shoulder_lift"] == pytest.approx(expect["shoulder_lift"])
    assert q["wrist_roll"] == pytest.approx(math.radians(rp.WRIST_ROLL_DEG))
    assert q["gripper"] == pytest.approx(math.radians(rp.GRIPPER_OPEN_DEG))
    assert kin.travel(0.6) == pytest.approx(0.6 - z_ref) and kin.ref_height(0.6) == pytest.approx(0.6)
    assert kin.clamp_deg([500, 0, 0, 0, 0, -50]) == pytest.approx([110.0, 0, 0, 0, 0, -10.0])
