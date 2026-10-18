import base64
import math

import pytest

from arms.sim.tests.sim_fixtures import loader, planogram, planogram2, products, robots, robots_loader
from arms.sim.worker import NullBackend, WorkerCore
from contracts import CONTRACTS_VERSION
from contracts.catalog import Lot
from contracts.common import Pose2D, slot_reachable
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
    SetSlotStockCmd,
    SetStreamsCmd,
    SimHosted,
    StopCmd,
    UnloadBasketCmd,
    parse_gateway_to_sim,
    parse_sim_message,
)

R1, R2, H1 = "rover-test-1", "rover-test-2", "rover-two-1"
SALSA_AP = Pose2D(x=2.98, y=5.0, theta=math.pi)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig():
    out: list = []
    clock = Clock()
    backend = NullBackend()
    core = WorkerCore(backend, out.append, clock=clock, loader=loader, robots_loader=robots_loader)
    core.load_stores(["store_test", "store_two"])
    return core, out, clock, backend


def results(out) -> dict[str, CommandResult]:
    return {m.cmd_id: m for m in out if isinstance(m, CommandResult)}


def run_until(core, out, clock, *cmd_ids, max_s=180.0, dt=0.05):
    t = 0.0
    while not all(c in results(out) for c in cmd_ids) and t < max_s:
        clock.t += dt
        t += dt
        core.tick(dt)
    res = results(out)
    return [res[c] for c in cmd_ids] if len(cmd_ids) > 1 else res[cmd_ids[0]]


def drive(core, out, clock, robot_id, pose, cmd_id=None):
    cmd_id = cmd_id or f"nav-{robot_id}-{pose.x}-{pose.y}"
    core.submit(NavigateCmd(cmd_id=cmd_id, robot_id=robot_id, waypoints=[pose]))
    res = run_until(core, out, clock, cmd_id)
    assert res.ok, res.error
    return res


def test_hello_lists_stores_robots_cameras(rig):
    core, _, _, _ = rig
    hello = core.hello("tok", "isaac-test", "sketch")
    assert hello.contracts_version == CONTRACTS_VERSION
    assert {s.store_id for s in hello.stores} == {"store_test", "store_two"}
    assert all(s.loaded and s.data_hash for s in hello.stores)
    test = next(s for s in hello.stores if s.store_id == "store_test")
    assert test.camera_ids == ["store_test.ceiling-1"]
    assert {r.robot_id: r.store_id for r in hello.robots} == {R1: "store_test", R2: "store_test", H1: "store_two"}
    cams = {c.camera_id for c in hello.cameras}
    assert {f"{R1}.front", f"{R1}.wrist", f"{R1}.basket", f"{H1}.basket", "store_two.ceiling-1"} <= cams
    assert parse_sim_message(hello.model_dump_json()).robot_ids() == hello.robot_ids()


def test_robots_spawn_at_docks_and_charge(rig):
    core, out, clock, _ = rig
    st = core.state(core.rovers[R1])
    assert st.pose == robots()[0].dock and st.status == RobotStatus.CHARGING and st.charging
    assert st.robot_id == R1 and st.store_id == "store_test"
    core.rovers[R1].battery = 50.0
    clock.t += 60
    core.tick(60.0)
    assert core.rovers[R1].battery == pytest.approx(55.0)


def test_robot_commands_need_a_hosted_robot(rig):
    core, out, _, _ = rig
    core.submit(NavigateCmd(cmd_id="n0", waypoints=[Pose2D(x=3, y=3)]))
    core.submit(PickCmd(cmd_id="p0", robot_id="ghost", slot_id="s_salsa", sku="salsa_hot"))
    core.submit(CaptureCmd(cmd_id="c0"))
    res = results(out)
    assert res["n0"].error_code == "unknown_robot"
    assert res["p0"].error_code == "unknown_robot" and res["p0"].robot_id == "ghost"
    assert res["c0"].error_code == "unknown_robot"


def test_navigate_pure_pursuit_reaches_pose_and_theta(rig):
    core, out, clock, backend = rig
    goal = Pose2D(x=3.4, y=9.0, theta=math.pi)
    core.submit(NavigateCmd(cmd_id="n", robot_id=R1, waypoints=[Pose2D(x=3.4, y=2.0), Pose2D(x=3.4, y=6.0), goal]))
    assert core.rovers[R1].status == RobotStatus.NAVIGATING
    res = run_until(core, out, clock, "n")
    assert res.ok, res.error
    assert res.robot_id == R1 and res.state.robot_id == R1
    assert res.state.pose.distance_to(goal) <= 0.05
    assert abs(math.remainder(res.state.pose.theta - goal.theta, 2 * math.pi)) <= 0.05
    assert res.state.status == RobotStatus.IDLE
    states = [m for m in out if isinstance(m, RobotState) and m.robot_id == R1]
    assert len(states) > 5
    assert res.state.battery_pct < robots()[0].battery_pct  # drained by distance
    assert backend.robot_poses[R1][0] == res.state.pose


def test_two_robots_move_concurrently_and_busy_is_per_robot(rig):
    core, out, clock, _ = rig
    core.submit(NavigateCmd(cmd_id="a", robot_id=R1, waypoints=[Pose2D(x=2.5, y=2.0), Pose2D(x=4.0, y=12.0)]))
    core.submit(NavigateCmd(cmd_id="h", robot_id=H1, waypoints=[Pose2D(x=4.0, y=8.0)]))
    core.submit(ScanCmd(cmd_id="busy", robot_id=R1, slot_id="s_salsa"))
    assert results(out)["busy"].error_code == "busy"
    clock.t += 0.5
    core.tick(0.5)
    assert core.rovers[R1].status == RobotStatus.NAVIGATING and core.rovers[H1].status == RobotStatus.NAVIGATING
    a, h = run_until(core, out, clock, "a", "h")
    assert a.ok and h.ok
    assert a.state.store_id == "store_test" and h.state.store_id == "store_two"


def test_stop_all_and_stop_one(rig):
    core, out, clock, _ = rig
    core.submit(NavigateCmd(cmd_id="a", robot_id=R1, waypoints=[Pose2D(x=12, y=20)]))
    core.submit(NavigateCmd(cmd_id="b", robot_id=H1, waypoints=[Pose2D(x=6, y=8)]))
    core.submit(CaptureCmd(cmd_id="cap", robot_id=R1))  # always allowed
    assert results(out)["cap"].ok and results(out)["cap"].frame.camera_id == f"{R1}.front"
    core.submit(StopCmd(cmd_id="s1", robot_id=H1))
    assert results(out)["b"].error_code == "cancelled" and "a" not in results(out)
    core.submit(StopCmd(cmd_id="all"))
    assert results(out)["a"].error_code == "cancelled"
    assert results(out)["all"].ok
    assert all(r.active is None for r in core.rovers.values())


def test_navigate_outside_store_is_path_blocked(rig):
    core, out, _, _ = rig
    core.submit(NavigateCmd(cmd_id="n", robot_id=H1, waypoints=[Pose2D(x=12, y=5)]))  # store_two is 8 m wide
    assert results(out)["n"].error_code == "path_blocked"


def test_rovers_avoid_each_other(rig):
    core, out, clock, _ = rig
    # R2 parks right on R1's straight-line path; R1 waits, then gives up with path_blocked
    core.rovers[R2].pose = Pose2D(x=1.0, y=5.0)
    core.submit(NavigateCmd(cmd_id="n", robot_id=R1, waypoints=[Pose2D(x=1.0, y=8.0, theta=math.pi / 2)]))
    res = run_until(core, out, clock, "n", max_s=40)
    assert res.error_code == "path_blocked"
    assert core.rovers[R1].pose.distance_to(core.rovers[R2].pose) >= 0.6 - 1e-6


def test_battery_low_refuses_motion_except_to_the_dock(rig):
    core, out, clock, _ = rig
    rover = core.rovers[R1]
    rover.battery = 2.0
    rover.pose = Pose2D(x=3.4, y=4.0)
    core.submit(NavigateCmd(cmd_id="n", robot_id=R1, waypoints=[Pose2D(x=3.4, y=8.0)]))
    core.submit(ScanCmd(cmd_id="s", robot_id=R1, slot_id="s_salsa"))
    assert results(out)["n"].error_code == "battery_low"
    assert results(out)["s"].error_code == "battery_low"
    res = drive(core, out, clock, R1, robots()[0].dock, "home")
    assert res.ok and res.state.status == RobotStatus.CHARGING


def test_scan_pick_place_unload(rig):
    core, out, clock, backend = rig
    core.submit(ScanCmd(cmd_id="bad", robot_id=R1, slot_id="TWO-salsa"))  # a slot of another store
    assert results(out)["bad"].error_code == "unknown_slot"
    core.submit(PickCmd(cmd_id="far", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    assert results(out)["far"].error_code == "grasp_failed"

    drive(core, out, clock, R1, SALSA_AP)
    assert slot_reachable(
        core.rovers[R1].pose, planogram().slot("s_salsa").approach_pose, planogram().slot("s_salsa").item_position
    )
    core.submit(ScanCmd(cmd_id="scan", robot_id=R1, slot_id="s_salsa"))
    scan = run_until(core, out, clock, "scan")
    assert scan.ok and scan.frame.slot_id == "s_salsa" and scan.frame.camera_id == f"{R1}.front"
    assert scan.frame.blurred is False and scan.frame.robot_id == R1 and scan.frame.store_id == "store_test"
    assert scan.frame.camera_pose is not None and scan.frame.camera_pose.frame == "world"
    assert scan.frame.camera_pose.pitch < 0  # pan-tilt head aimed down at the 0.55 m shelf
    seen = scan.frame.instances
    assert [i.instance_id for i in seen] == [f"s_salsa#{n:02d}" for n in range(6)]  # every unit = exact count
    visible = [i for i in seen if i.bbox is not None]
    assert visible, "front units should be in view"
    for i in visible:
        assert 0 <= i.bbox.x and i.bbox.x + i.bbox.w <= scan.frame.w + 1e-6
        assert 0 <= i.bbox.y and i.bbox.y + i.bbox.h <= scan.frame.h + 1e-6
    assert base64.b64decode(scan.frame.jpeg_b64)[:2] == b"\xff\xd8"

    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot", instance_id="s_salsa#05"))
    assert core.rovers[R1].status == RobotStatus.PICKING
    pick = run_until(core, out, clock, "p")
    assert pick.ok and pick.instance_id == "s_salsa#05" and pick.state.holding == "s_salsa#05"
    assert pick.state.lift_height_m == pytest.approx(0.55 + 0.06, abs=0.01)  # lift went to the item height
    held = core.stores["store_test"].instances["s_salsa#05"]
    assert held.position.z == pytest.approx(pick.state.lift_height_m)  # rides in the gripper
    assert not held.in_basket
    core.submit(PickCmd(cmd_id="p2", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    assert results(out)["p2"].error_code == "grasp_failed"  # gripper full

    core.submit(PlaceInBasketCmd(cmd_id="pl", robot_id=R1))
    place = run_until(core, out, clock, "pl")
    assert place.ok and place.state.basket == ["s_salsa#05"] and place.state.basket_skus == ["salsa_hot"]
    assert place.state.holding is None and core.stores["store_test"].instances["s_salsa#05"].in_basket
    assert any(isinstance(m, InstanceInfo) and m.robot_id == R1 for m in out)

    # the next scan no longer counts the unit in the basket
    core.submit(ScanCmd(cmd_id="scan2", robot_id=R1, slot_id="s_salsa"))
    assert len(run_until(core, out, clock, "scan2").frame.instances) == 5

    # basket rides with the robot
    before = core.stores["store_test"].instances["s_salsa#05"].position
    drive(core, out, clock, R1, Pose2D(x=3.4, y=8.0, theta=math.pi))
    after = core.stores["store_test"].instances["s_salsa#05"].position
    assert after.y - before.y > 2.5
    assert backend.positions[("store_test", "s_salsa#05")] == after

    # basket camera sees the unit
    core.submit(CaptureCmd(cmd_id="bc", camera_id=f"{R1}.basket"))
    assert "s_salsa#05" in [i.instance_id for i in results(out)["bc"].frame.instances]

    core.submit(UnloadBasketCmd(cmd_id="u", robot_id=R1))
    unload = run_until(core, out, clock, "u")
    assert unload.ok and [i.instance_id for i in unload.instances] == ["s_salsa#05"]
    assert unload.state.basket == [] and "s_salsa#05" not in core.stores["store_test"].instances
    assert ("store_test", "s_salsa#05") not in backend.instance_prims


def test_pick_nothing_left_is_no_instance(rig):
    core, out, clock, _ = rig
    core.rovers[R1].pose = Pose2D(x=7.38, y=6.0, theta=math.pi)
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_beans_e", sku="beans"))
    assert results(out)["p"].error_code == "no_instance"


def test_place_without_holding_and_basket_capacity(rig):
    core, out, clock, _ = rig
    core.submit(PlaceInBasketCmd(cmd_id="pl", robot_id=R1))
    assert results(out)["pl"].error_code == "no_instance"
    rover = core.rovers[R1]
    rover.robot = rover.robot.model_copy(update={"basket_capacity": 1})
    rover.pose = SALSA_AP
    for k in range(2):
        core.submit(PickCmd(cmd_id=f"p{k}", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
        assert run_until(core, out, clock, f"p{k}").ok
        core.submit(PlaceInBasketCmd(cmd_id=f"pl{k}", robot_id=R1))
        if k == 0:
            assert run_until(core, out, clock, "pl0").ok
    assert results(out)["pl1"].error_code == "grasp_failed"


def test_captures_ceiling_wrist_and_aliases(rig):
    core, out, _, _ = rig
    core.submit(CaptureCmd(cmd_id="ceil", camera_id="store_test.ceiling-1"))
    core.submit(CaptureCmd(cmd_id="head", robot_id=H1, camera="head"))
    core.submit(CaptureCmd(cmd_id="wrist", camera_id=f"{R2}.wrist"))
    core.submit(CaptureCmd(cmd_id="nope", camera_id="store_test.ceiling-9"))
    res = results(out)
    ceil = res["ceil"].frame
    assert ceil.camera == "ceiling" and ceil.robot_id is None and ceil.store_id == "store_test" and ceil.pose is None
    assert ceil.camera_pose.z == pytest.approx(3.2)
    assert res["head"].frame.camera_id == f"{H1}.front" and res["head"].frame.store_id == "store_two"
    assert res["wrist"].frame.camera == "wrist" and res["wrist"].robot_id is None
    assert res["nope"].error_code == "unknown_robot"


def test_streams_default_and_set(rig):
    core, out, clock, _ = rig
    core.on_connected()
    assert set(core.streams) == {f"{R1}.front", f"{R2}.front", f"{H1}.front"}
    assert sum(isinstance(m, RobotState) for m in out[-6:]) >= 1
    assert len(core.stream_due()) == 3
    assert core.stream_due() == []
    clock.t += 0.5
    assert len(core.stream_due()) == 3  # 2 fps
    core.submit(SetStreamsCmd(cmd_id="ss", camera_ids=["store_two.ceiling-1", "bogus.front"], fps=10))
    assert results(out)["ss"].ok and set(core.streams) == {"store_two.ceiling-1"}
    frame = core.capture("store_two.ceiling-1", stream=True)
    assert frame.instances == [] and frame.camera_id == "store_two.ceiling-1"
    core.on_disconnected()
    assert core.streams == {}


def test_list_instances_scopes(rig):
    core, out, _, _ = rig
    core.submit(ListInstancesCmd(cmd_id="store", store_id="store_two"))
    core.submit(ListInstancesCmd(cmd_id="robot", robot_id=R1, slot_id="s_avo"))
    core.submit(ListInstancesCmd(cmd_id="all"))
    core.submit(ListInstancesCmd(cmd_id="nope", store_id="store_x"))
    res = results(out)
    assert len(res["store"].instances) == 4 and all(i.store_id == "store_two" for i in res["store"].instances)
    assert len(res["robot"].instances) == 6
    assert len(res["all"].instances) == 15 + 4
    assert res["nope"].error_code == "not_ready"


def test_set_slot_stock_removes_back_rows_and_respawns_lowest_ids(rig):
    core, out, clock, backend = rig
    core.submit(SetSlotStockCmd(cmd_id="down", store_id="store_test", slot_id="s_salsa", sku="salsa_hot", qty=2))
    down = results(out)["down"]
    assert down.ok and [i.instance_id for i in down.instances] == ["s_salsa#00", "s_salsa#01"]
    assert ("store_test", "s_salsa#05") not in backend.instance_prims
    # pick #00 into the basket, then restock to 4: #00 stays in the basket, new units are #02, #03
    core.rovers[R1].pose = SALSA_AP
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot", instance_id="s_salsa#00"))
    assert run_until(core, out, clock, "p").ok
    core.submit(PlaceInBasketCmd(cmd_id="pl", robot_id=R1))
    assert run_until(core, out, clock, "pl").ok
    lots = [Lot(lot_id="NEW", qty=4, freshness=0.8)]
    core.submit(
        SetSlotStockCmd(cmd_id="up", store_id="store_test", slot_id="s_salsa", sku="salsa_hot", qty=4, lots=lots)
    )
    up = results(out)["up"]
    assert [i.instance_id for i in up.instances] == ["s_salsa#01", "s_salsa#02", "s_salsa#03", "s_salsa#04"]
    assert all(i.lot_id == "NEW" and i.freshness is not None for i in up.instances)
    assert core.stores["store_test"].instances["s_salsa#00"].in_basket
    assert ("store_test", "s_salsa#04") in backend.instance_prims
    # restock an empty slot from zero
    core.submit(SetSlotStockCmd(cmd_id="beans", store_id="store_test", slot_id="s_beans_e", sku="beans", qty=3))
    assert [i.instance_id for i in results(out)["beans"].instances] == [f"s_beans_e#{n:02d}" for n in range(3)]
    core.submit(SetSlotStockCmd(cmd_id="bad", store_id="store_test", slot_id="nope", sku="beans", qty=3))
    assert results(out)["bad"].error_code == "unknown_slot"


def test_store_reset_rebuilds_respawns_and_sends_hosted(rig):
    core, out, clock, _ = rig
    core.submit(NavigateCmd(cmd_id="n", robot_id=R1, waypoints=[Pose2D(x=12, y=20)]))
    plan = planogram()
    plan.slot("s_salsa").qty = 2
    core.submit(ResetCmd(cmd_id="r", store_id="store_test", planogram=plan, products=products(), robots=robots()[:2]))
    res = results(out)
    assert res["n"].error_code == "cancelled"
    assert res["r"].ok
    hosted = [m for m in out if isinstance(m, SimHosted)]
    assert hosted and {s.store_id for s in hosted[-1].stores} == {"store_test", "store_two"}
    assert len(core.stores["store_test"].instances) == 2 + 3 + 6
    assert core.rovers[R1].pose == robots()[0].dock
    # hosted message precedes the reset result
    assert out.index(hosted[-1]) < out.index(res["r"])


def test_reset_pushes_a_new_store(rig):
    out: list = []
    core = WorkerCore(NullBackend(), out.append, loader=lambda sid: (_ for _ in ()).throw(KeyError(sid)))
    core.submit(ResetCmd(cmd_id="r", store_id="store_two", planogram=planogram2(), products=products()))
    res = results(out)["r"]
    assert res.ok, res.error
    assert core.hosted().robot_ids() == ["rover-two-1"]  # default robot from the id convention
    assert core.hosted().stores[0].camera_ids == ["store_two.ceiling-1"]
    core.submit(ResetCmd(cmd_id="r2", store_id="store_x"))
    assert results(out)["r2"].error_code == "not_ready"


def test_robot_reset(rig):
    core, out, clock, _ = rig
    rover = core.rovers[R1]
    rover.pose = SALSA_AP
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    run_until(core, out, clock, "p")
    rover.battery = 40
    core.submit(ResetCmd(cmd_id="wrong", robot_id=R1, store_id="store_two"))
    assert results(out)["wrong"].error_code == "unknown_robot"
    core.submit(ResetCmd(cmd_id="r", robot_id=R1))
    res = results(out)["r"]
    assert res.ok and res.state.pose == robots()[0].dock and res.state.holding is None
    assert res.state.battery_pct == robots()[0].battery_pct
    assert len([i for i in core.stores["store_test"].instances.values() if i.slot_id == "s_salsa"]) == 5
    core.submit(ResetCmd(cmd_id="ghost", robot_id="ghost"))
    assert results(out)["ghost"].error_code == "unknown_robot"


def test_parsed_wire_commands_round_trip(rig):
    core, out, clock, _ = rig
    raw = (
        '{"type":"navigate","cmd_id":"w1","robot_id":"rover-two-1",'
        '"waypoints":[{"x":3,"y":6,"theta":0,"extra":1}],"new":2}'
    )
    core.submit(parse_gateway_to_sim(raw))
    assert run_until(core, out, clock, "w1").ok
    core.submit(parse_gateway_to_sim('{"type":"welcome","ok":true}'))  # ignored, no result
    assert "w1" in results(out)


def test_pick_reaches_to_the_unit_without_a_jump(rig):
    """The arm stops HELD_CLEARANCE_M short of the front unit (not at ARM_FULL_EXT_M, inside it): at the attach the
    held unit is still where it stood, and the held label faces the robot (the wrist camera reads it)."""
    from arms.sim.geometry import ARM_FULL_EXT_M, ARM_STOW_EXT_M

    core, out, clock, backend = rig
    drive(core, out, clock, R1, SALSA_AP)
    scene = core.stores["store_test"]
    start = scene.instances["s_salsa#00"].position
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    rover = core.rovers[R1]
    exts, attach = [], None
    for _ in range(400):
        clock.t += 0.05
        core.tick(0.05)
        exts.append(rover.arm_ext)
        if attach is None and rover.holding is not None:
            attach = scene.instances[rover.holding].position
        if "p" in results(out):
            break
    assert results(out)["p"].ok and attach is not None
    assert ARM_STOW_EXT_M < max(exts) < ARM_FULL_EXT_M
    c, s = math.cos(rover.pose.theta), math.sin(rover.pose.theta)
    along = (attach.x - start.x) * c + (attach.y - start.y) * s
    assert abs(along) < 0.01  # no jump toward / away from the robot at the grasp (lateral = the rover's docking error)
    yaw = backend.yaws[("store_test", "s_salsa#00")] if hasattr(backend, "yaws") else None
    if yaw is not None:
        assert math.cos(yaw - (rover.pose.theta - math.pi / 2)) > 0.99
