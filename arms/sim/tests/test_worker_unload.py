"""0.2.4 checkout unload by the arm on the Isaac worker core (NullBackend, no GPU): the SO-101 reaches back into the
basket, the unit follows the gripper, and lands on the counter (``on_counter``); the handoff bags the counter."""

import math

from arms.sim.tests.test_worker import R1, SALSA_AP, drive, results, rig, run_until  # noqa: F401 - pytest fixture
from contracts.common import Point3, Pose2D
from contracts.sim import (
    BASKET_UNLOAD_CAPABILITY,
    ListInstancesCmd,
    PickCmd,
    PickFromBasketCmd,
    PlaceInBasketCmd,
    PlaceOnCounterCmd,
    UnloadBasketCmd,
    parse_gateway_to_sim,
)


def _basket_one(core, out, clock) -> str:
    drive(core, out, clock, R1, SALSA_AP)
    core.submit(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    assert run_until(core, out, clock, "p").ok
    core.submit(PlaceInBasketCmd(cmd_id="pl", robot_id=R1))
    place = run_until(core, out, clock, "pl")
    assert place.ok
    return place.state.basket[0]


def test_hello_advertises_basket_unload(rig):  # noqa: F811
    core, _, _, _ = rig
    assert BASKET_UNLOAD_CAPABILITY in core.hello("tok").capabilities


def test_unload_from_basket_to_counter_and_handoff(rig):  # noqa: F811
    core, out, clock, backend = rig
    iid = _basket_one(core, out, clock)
    scene = core.stores["store_test"]
    rv = core.rovers[R1]

    start = scene.instances[iid].position
    core.submit(PickFromBasketCmd(cmd_id="g", robot_id=R1, sku="salsa_hot"))
    mid = []
    for _ in range(200):  # the unit follows the gripper while the arm lifts it out
        if "g" in results(out):
            break
        clock.t += 0.05
        core.tick(0.05)
        mid.append(scene.instances[iid].position)
    grab = run_until(core, out, clock, "g")
    assert grab.ok, grab.error
    assert grab.instance_id == iid and grab.state.holding == iid and grab.state.basket == []
    assert not scene.instances[iid].in_basket and rv.arm_q is not None  # joint mode kept: no jump
    assert len({(round(p.x, 3), round(p.y, 3), round(p.z, 3)) for p in mid}) > 5
    end = scene.instances[iid].position
    assert math.dist((start.x, start.y, start.z), (end.x, end.y, end.z)) > 0.05  # lifted out of the basket

    c, s = math.cos(rv.pose.theta), math.sin(rv.pose.theta)
    spot = Point3(x=rv.pose.x + 0.43 * c, y=rv.pose.y + 0.43 * s, z=0.9)
    raw = PlaceOnCounterCmd(cmd_id="c", robot_id=R1, position=spot, yaw=0.0, spot=0).model_dump(mode="json")
    core.submit(parse_gateway_to_sim(raw))
    put = run_until(core, out, clock, "c")
    assert put.ok, put.error
    unit = scene.instances[iid]
    assert unit.on_counter and not unit.in_basket and put.state.holding is None
    assert math.hypot(unit.position.x - spot.x, unit.position.y - spot.y) < 1e-6 and unit.position.z > spot.z
    assert rv.counter == [iid] and rv.arm_q is None  # stowed, scripted rig again

    core.submit(ListInstancesCmd(cmd_id="ls", robot_id=R1, store_id="store_test"))
    listed = {i.instance_id: i for i in results(out)["ls"].instances}
    assert listed[iid].on_counter

    core.submit(UnloadBasketCmd(cmd_id="u", robot_id=R1))
    bag = run_until(core, out, clock, "u")
    assert bag.ok and [i.instance_id for i in bag.instances] == [iid]  # staff bag the counter
    assert iid not in scene.instances and rv.counter == []


def test_unload_errors(rig):  # noqa: F811
    core, out, clock, _ = rig
    core.submit(PickFromBasketCmd(cmd_id="e1", robot_id=R1, sku="salsa_hot"))
    assert results(out)["e1"].error_code == "no_instance"
    core.submit(PlaceOnCounterCmd(cmd_id="e2", robot_id=R1, position=Point3(x=1, y=1, z=0.9)))
    assert results(out)["e2"].error_code == "no_instance"
    _basket_one(core, out, clock)
    core.submit(PickFromBasketCmd(cmd_id="g", robot_id=R1, sku="salsa_hot"))
    assert run_until(core, out, clock, "g").ok
    far = Point3(x=core.rovers[R1].pose.x + 3.0, y=core.rovers[R1].pose.y, z=0.9)
    core.submit(PlaceOnCounterCmd(cmd_id="e3", robot_id=R1, position=far))
    assert results(out)["e3"].error_code == "grasp_failed"
    drive(core, out, clock, R1, Pose2D(x=3.4, y=8.0, theta=math.pi))  # a navigate leaves joint mode, still holding
    assert core.rovers[R1].holding is not None and core.rovers[R1].arm_q is None
