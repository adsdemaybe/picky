import asyncio
import base64
import math

import pytest

from arms.robot import FakeRobot, create_fake
from arms.robot.tests.conftest import STORE
from contracts.common import Pose2D
from contracts.services import RobotService, Services
from contracts.sim import RobotError, RobotStatus

INF = math.inf


def fast(services: Services, **kw) -> FakeRobot:
    return create_fake(services, realtime_factor=INF, **kw)


def test_conforms_to_protocol(services):
    robot = create_fake(services)
    assert isinstance(robot, FakeRobot)
    assert isinstance(robot, RobotService)
    assert isinstance(FakeRobot(), RobotService)  # no services needed to construct


async def test_construction_does_not_call_services(services):
    create_fake(services)
    assert services.catalog.calls == []


async def test_reset_places_robot_home_and_builds_deterministic_instances(services):
    robot = fast(services)
    st = await robot.reset(STORE)
    assert st.store_id == STORE and st.status == RobotStatus.IDLE
    assert (st.pose.x, st.pose.y) == (1.0, 1.0)
    avo = await robot.list_instances("S-avocado")
    assert [i.instance_id for i in avo] == [f"S-avocado#{n:02d}" for n in range(5)]
    assert [i.lot_id for i in avo] == ["L-avo-a"] * 3 + ["L-avo-b"] * 2
    assert all(i.freshness is not None for i in avo)
    assert len({i.freshness for i in avo}) > 1  # per-unit spread so top-3 ranking means something
    assert await robot.list_instances("S-limes") == []  # OOS slot
    # second robot -> identical ground truth
    robot2 = fast(services)
    await robot2.reset(STORE)
    assert [i.model_dump() for i in await robot2.list_instances("S-avocado")] == [i.model_dump() for i in avo]
    # instance centers sit on/behind the shelf face, z = base + height/2
    salsa = await robot.list_instances("S-salsa")
    assert all(i.position.y >= 4.0 for i in salsa)
    assert salsa[0].position.z == pytest.approx(0.4 + 0.06)


async def test_navigate_reaches_final_pose_in_sim_time(services):
    robot = fast(services)
    await robot.reset(STORE)
    target = Pose2D(x=5.0, y=3.2, theta=math.pi / 2)
    st = await robot.navigate([Pose2D(x=1.0, y=1.0), Pose2D(x=5.0, y=1.0), target])
    assert (st.pose.x, st.pose.y, st.pose.theta) == (5.0, 3.2, math.pi / 2)
    assert st.status == RobotStatus.IDLE
    dist = 4.0 + 2.2
    assert dist / 0.8 <= robot.sim_time_s < dist / 0.8 + 10


async def test_states_stream_reports_motion(services):
    robot = fast(services)
    await robot.reset(STORE)
    seen: list[RobotStatus] = []

    async def collect():
        async for s in robot.states():
            seen.append(s.status)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await robot.navigate([Pose2D(x=3.0, y=1.0, theta=0.0)])
    await asyncio.sleep(0)
    task.cancel()
    assert RobotStatus.NAVIGATING in seen and seen[-1] == RobotStatus.IDLE


async def test_scan_pick_place_round_trip(services):
    robot = fast(services)
    await robot.reset(STORE)
    await robot.navigate([Pose2D(x=5.0, y=3.2, theta=math.pi / 2)])
    frame = await robot.scan("S-avocado")
    assert frame.slot_id == "S-avocado" and frame.blurred is True
    assert base64.b64decode(frame.jpeg_b64)[:3] == b"\xff\xd8\xff"
    assert len(frame.instances) == 5 and all(i.bbox is not None for i in frame.instances)
    for i in frame.instances:
        assert 0 <= i.bbox.x and i.bbox.x + i.bbox.w <= frame.w + 0.5 and i.bbox.y + i.bbox.h <= frame.h + 0.5

    freshest = max(frame.instances, key=lambda i: i.freshness or 0)
    picked = await robot.pick("S-avocado", "avocado", freshest.instance_id)
    assert picked.instance_id == freshest.instance_id
    assert (await robot.state()).holding == freshest.instance_id
    assert len(await robot.list_instances("S-avocado")) == 4

    st = await robot.place_in_basket()
    assert st.holding is None and st.basket == [freshest.instance_id] and st.basket_skus == ["avocado"]
    basket = await robot.capture("basket")
    assert [i.instance_id for i in basket.instances] == [freshest.instance_id]
    assert basket.instances[0].in_basket
    everything = await robot.list_instances()
    assert sum(1 for i in everything if i.in_basket) == 1


async def test_pick_any_instance_takes_front_unit(services):
    robot = fast(services)
    await robot.reset(STORE)
    inst = await robot.pick("S-salsa", "salsa-mild")
    assert inst.instance_id == "S-salsa#00"


@pytest.mark.parametrize(
    ("slot", "sku", "iid", "code"),
    [
        ("nope", "avocado", None, "unknown_slot"),
        ("S-avocado", "salsa-mild", None, "no_instance"),
        ("S-limes", "limes", None, "no_instance"),
        ("S-avocado", "avocado", "S-avocado#99", "no_instance"),
    ],
)
async def test_pick_errors(services, slot, sku, iid, code):
    robot = fast(services)
    await robot.reset(STORE)
    with pytest.raises(RobotError) as exc:
        await robot.pick(slot, sku, iid)
    assert exc.value.code == code


async def test_place_with_empty_gripper_and_double_pick(services):
    robot = fast(services)
    await robot.reset(STORE)
    with pytest.raises(RobotError) as exc:
        await robot.place_in_basket()
    assert exc.value.code == "no_instance"
    await robot.pick("S-salsa", "salsa-mild")
    with pytest.raises(RobotError) as exc:
        await robot.pick("S-salsa", "salsa-mild")
    assert exc.value.code == "grasp_failed"


async def test_reset_unknown_store_is_not_ready(services):
    with pytest.raises(RobotError) as exc:
        await fast(services).reset("store_nowhere")
    assert exc.value.code == "not_ready"


async def test_path_through_shelf_is_blocked(services):
    robot = fast(services)
    await robot.reset(STORE)
    with pytest.raises(RobotError) as exc:
        await robot.navigate([Pose2D(x=5.0, y=6.0)])
    assert exc.value.code == "path_blocked"
    assert (await robot.state()).status == RobotStatus.IDLE


async def test_busy_and_stop_cancels_motion(services):
    robot = create_fake(services, realtime_factor=50.0)
    await robot.reset(STORE)
    nav = asyncio.create_task(robot.navigate([Pose2D(x=9.0, y=1.0, theta=0.0)]))
    await asyncio.sleep(0.02)
    with pytest.raises(RobotError) as exc:
        await robot.pick("S-salsa", "salsa-mild")
    assert exc.value.code == "busy"
    # capture / list_instances / state always allowed while moving
    await robot.capture()
    await robot.list_instances("S-salsa")
    st = await robot.stop()
    with pytest.raises(RobotError) as exc:
        await nav
    assert exc.value.code == "cancelled"
    assert st.status == RobotStatus.IDLE
    assert 1.0 < (await robot.state()).pose.x < 9.0


async def test_optional_state_event_publishing(services):
    quiet = fast(services)
    await quiet.reset(STORE)
    assert services.events.events == []  # default: the gateway publishes robot.state, not the robot
    loud = fast(services, publish_state_events=True)
    loud.set_trip("trip_demo")
    await loud.reset(STORE)
    await loud.navigate([Pose2D(x=3.0, y=1.0)])
    evs = services.events.events
    assert evs and all(e.type == "robot.state" and e.trip_id == "trip_demo" for e in evs)


async def test_without_catalog_uses_data_dir_or_reports_not_ready():
    """No catalog wired: falls back to utils.data (data/ may not exist yet while the data agent works)."""
    from utils.data import list_store_ids

    robot = FakeRobot(Services(), realtime_factor=INF)
    if "store_ave" not in list_store_ids():
        with pytest.raises(RobotError) as exc:
            await robot.reset("store_ave")
        assert exc.value.code == "not_ready"
        return
    st = await robot.reset("store_ave")
    assert st.store_id == "store_ave"
    assert len(await robot.list_instances()) > 0


async def test_demo_store_scenario_when_data_present():
    """Demo store (data/planograms/store_ave.yaml): every stocked unit becomes one instance with a unique id, and a
    produce freshness top-3 pick lands in the basket. Skipped until the data agent's files exist."""
    from utils.data import DataError, list_store_ids, load_planogram

    if "store_ave" not in list_store_ids():
        pytest.skip("data/planograms/store_ave.yaml not written yet")
    try:
        plan = load_planogram("store_ave")
    except DataError as exc:
        pytest.skip(f"store_ave planogram invalid right now: {exc}")
    robot = FakeRobot(Services(), realtime_factor=INF)
    await robot.reset("store_ave")
    insts = await robot.list_instances()
    assert len(insts) == sum(s.qty for s in plan.slots)
    assert len({i.instance_id for i in insts}) == len(insts)
    produce = next((s for s in plan.slots if s.qty > 0 and any(lot.freshness is not None for lot in s.lots)), None)
    if produce is None:
        pytest.skip("no produce slot with lot freshness in store_ave yet")
    frame = await robot.scan(produce.slot_id)
    top3 = sorted(frame.instances, key=lambda i: -(i.freshness or 0))[:3]
    await robot.pick(produce.slot_id, produce.sku, top3[0].instance_id)
    st = await robot.place_in_basket()
    assert st.basket == [top3[0].instance_id]
