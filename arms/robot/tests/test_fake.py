import asyncio
import base64
import io
import math

import pytest
from PIL import Image

from arms.robot import FakeRobot, create_fake
from arms.robot.render import JPEG_COMMENT
from arms.robot.tests.conftest import R1, R2, STORE, UP, fleet_opts
from contracts.common import BATTERY_MOTION_MIN_PCT, Pose2D
from contracts.services import RobotService, Services
from contracts.sim import RobotError, RobotStatus

AT_SALSA = Pose2D(x=3.0, y=3.62, theta=UP)
AT_AVO = Pose2D(x=5.0, y=3.62, theta=UP)


def fast(services: Services, **kw) -> FakeRobot:
    return create_fake(services, **fleet_opts(**kw))


def jpeg(frame) -> Image.Image:  # noqa: ANN001
    raw = base64.b64decode(frame.jpeg_b64)
    assert raw[:3] == b"\xff\xd8\xff"
    img = Image.open(io.BytesIO(raw))
    assert img.size == (frame.w, frame.h)
    assert img.info.get("comment") == JPEG_COMMENT  # marked synthetic
    return img


def test_conforms_to_protocol(services):
    robot = create_fake(services)
    assert isinstance(robot, FakeRobot)
    assert isinstance(robot, RobotService)
    assert isinstance(FakeRobot(), RobotService)  # no services needed to construct


async def test_construction_does_not_call_services(services):
    create_fake(services)
    assert services.catalog.calls == []


async def test_list_robots_starts_everyone_on_their_dock(services):
    robot = fast(services)
    states = await robot.list_robots()
    assert [s.robot_id for s in states] == [R1, R2]
    assert services.catalog.calls == []  # listing needs no scene
    s1, s2 = states
    assert (s1.pose.x, s1.pose.y) == (1.0, 1.0) and s1.store_id == STORE and s1.backend == "fake"
    assert s1.status == RobotStatus.CHARGING  # idle on the dock
    assert s2.battery_pct == 80.0 and s1.battery_pct == 100.0
    assert s1.lift_height_m is not None
    with pytest.raises(RobotError) as exc:
        await robot.state("rover-nope")
    assert exc.value.code == "unknown_robot"


async def test_reset_puts_robot_on_dock_and_builds_deterministic_instances(services):
    robot = fast(services)
    st = await robot.reset(R1)
    assert st.robot_id == R1 and st.store_id == STORE
    assert (st.pose.x, st.pose.y) == (1.0, 1.0)
    avo = await robot.list_instances(R1, "S-avocado")
    assert [i.instance_id for i in avo] == [f"S-avocado#{n:02d}" for n in range(5)]
    assert [i.lot_id for i in avo] == ["L-avo-a"] * 3 + ["L-avo-b"] * 2
    assert all(i.store_id == STORE for i in avo)
    assert len({i.freshness for i in avo}) > 1  # per-unit spread so top-3 ranking means something
    assert await robot.list_instances(R1, "S-limes") == []  # OOS slot
    robot2 = fast(services)
    assert [i.model_dump() for i in await robot2.list_instances(R2, "S-avocado")] == [i.model_dump() for i in avo]
    with pytest.raises(RobotError) as exc:
        await robot.reset(R1, "store_elsewhere")
    assert exc.value.code == "unknown_robot"


async def test_navigate_reaches_final_pose_in_sim_time_and_drains_battery(services):
    robot = fast(services)
    st = await robot.navigate(R1, [Pose2D(x=5.0, y=1.0), AT_AVO])
    assert (st.pose.x, st.pose.y, st.pose.theta) == (5.0, 3.62, UP)
    assert st.status == RobotStatus.IDLE
    dist = 4.0 + 2.62
    assert dist / 0.8 <= robot.robot_sim_time_s(R1) < dist / 0.8 + 10
    assert 100.0 - dist * 0.2 - 0.1 <= st.battery_pct < 100.0


async def test_two_robots_move_concurrently(services):
    robot = create_fake(services, **fleet_opts(realtime_factor=200.0))
    a = asyncio.create_task(robot.navigate(R1, [Pose2D(x=5.0, y=1.0, theta=0.0)]))
    b = asyncio.create_task(robot.navigate(R2, [Pose2D(x=5.0, y=2.4, theta=0.0)]))
    await asyncio.sleep(0.02)
    s1, s2 = await robot.state(R1), await robot.state(R2)
    assert s1.status == RobotStatus.NAVIGATING and s2.status == RobotStatus.NAVIGATING
    await asyncio.gather(a, b)
    assert (await robot.state(R1)).pose.x == 5.0 and (await robot.state(R2)).pose.x == 5.0


async def test_states_stream_filters_by_robot(services):
    robot = fast(services)
    seen: list[tuple[str, RobotStatus]] = []

    async def collect():
        async for s in robot.states(R1):
            seen.append((s.robot_id, s.status))

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await robot.navigate(R1, [Pose2D(x=3.0, y=1.0, theta=0.0)])
    await robot.navigate(R2, [Pose2D(x=3.0, y=2.2, theta=0.0)])
    await asyncio.sleep(0)
    task.cancel()
    assert {rid for rid, _ in seen} == {R1}
    assert (R1, RobotStatus.NAVIGATING) in seen and seen[-1] == (R1, RobotStatus.IDLE)


async def test_scan_pick_place_unload_round_trip(services):
    robot = fast(services)
    await robot.navigate(R1, [AT_AVO])
    frame = await robot.scan(R1, "S-avocado")
    assert frame.slot_id == "S-avocado" and frame.blurred is False  # RAW: CameraService blurs
    assert frame.camera_id == f"{R1}.front" and frame.robot_id == R1 and frame.store_id == STORE
    assert frame.camera_pose is not None and frame.camera_pose.frame == "world" and frame.camera_pose.pitch < 0
    jpeg(frame)
    assert len(frame.instances) == 5 and all(i.bbox is not None for i in frame.instances)
    for i in frame.instances:
        assert 0 <= i.bbox.x and i.bbox.x + i.bbox.w <= frame.w + 0.5 and i.bbox.y + i.bbox.h <= frame.h + 0.5

    freshest = max(frame.instances, key=lambda i: i.freshness or 0)
    picked = await robot.pick(R1, "S-avocado", "avocado", freshest.instance_id)
    assert picked.instance_id == freshest.instance_id and picked.store_id == STORE
    st = await robot.state(R1)
    assert st.holding == freshest.instance_id and st.lift_height_m == pytest.approx(0.5)
    assert len(await robot.list_instances(R1, "S-avocado")) == 4
    assert len(await robot.list_instances(R2, "S-avocado")) == 4  # one shared store world

    st = await robot.place_in_basket(R1)
    assert st.holding is None and st.basket == [freshest.instance_id] and st.basket_skus == ["avocado"]
    basket = await robot.capture(R1, "basket")
    assert basket.camera_id == f"{R1}.basket"
    assert [i.instance_id for i in basket.instances] == [freshest.instance_id] and basket.instances[0].in_basket
    everything = await robot.list_instances(R1)
    assert sum(1 for i in everything if i.in_basket) == 1

    unloaded = await robot.unload_basket(R1)
    assert [i.instance_id for i in unloaded] == [freshest.instance_id]
    assert (await robot.state(R1)).basket == []


async def test_pick_requires_reach_unless_relaxed(services):
    robot = fast(services)
    with pytest.raises(RobotError) as exc:  # still on the dock
        await robot.pick(R1, "S-salsa", "salsa-mild")
    assert exc.value.code == "grasp_failed"
    lenient = fast(services, strict_reach=False)
    inst = await lenient.pick(R1, "S-salsa", "salsa-mild")
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
    robot = fast(services, strict_reach=False)
    with pytest.raises(RobotError) as exc:
        await robot.pick(R1, slot, sku, iid)
    assert exc.value.code == code


async def test_place_with_empty_gripper_double_pick_and_full_basket(services):
    from arms.robot.tests.conftest import make_robots

    specs = make_robots()
    specs[0] = specs[0].model_copy(update={"basket_capacity": 1})
    robot = fast(services, strict_reach=False, robots=specs)
    with pytest.raises(RobotError) as exc:
        await robot.place_in_basket(R1)
    assert exc.value.code == "no_instance"
    await robot.pick(R1, "S-salsa", "salsa-mild")
    with pytest.raises(RobotError) as exc:
        await robot.pick(R1, "S-salsa", "salsa-mild")
    assert exc.value.code == "grasp_failed"
    await robot.place_in_basket(R1)
    await robot.pick(R1, "S-salsa", "salsa-mild")
    with pytest.raises(RobotError) as exc:
        await robot.place_in_basket(R1)
    assert exc.value.code == "grasp_failed" and "basket full" in exc.value.message


async def test_unknown_store_is_not_ready():
    from arms.robot.tests.conftest import make_robots

    spec = make_robots()[0].model_copy(update={"store_id": "store_nowhere"})
    robot = FakeRobot(Services(), realtime_factor=math.inf, robots=[spec])
    with pytest.raises(RobotError) as exc:
        await robot.reset(R1)
    assert exc.value.code == "not_ready"


async def test_path_through_shelf_is_blocked(services):
    robot = fast(services)
    with pytest.raises(RobotError) as exc:
        await robot.navigate(R1, [Pose2D(x=5.0, y=6.0)])
    assert exc.value.code == "path_blocked"
    assert (await robot.state(R1)).status == RobotStatus.CHARGING  # back to idle, still on its dock


async def test_busy_and_stop_cancel_one_robot_only(services):
    robot = create_fake(services, **fleet_opts(realtime_factor=50.0, strict_reach=False))
    nav = asyncio.create_task(robot.navigate(R1, [Pose2D(x=9.0, y=1.0, theta=0.0)]))
    other = asyncio.create_task(robot.navigate(R2, [Pose2D(x=3.0, y=2.2, theta=0.0)]))
    await asyncio.sleep(0.02)
    with pytest.raises(RobotError) as exc:
        await robot.pick(R1, "S-salsa", "salsa-mild")
    assert exc.value.code == "busy"
    await robot.capture(R1)  # capture / list_instances / state always allowed while moving
    await robot.list_instances(R1, "S-salsa")
    st = await robot.stop(R1)
    with pytest.raises(RobotError) as exc:
        await nav
    assert exc.value.code == "cancelled"
    assert st.status == RobotStatus.IDLE and 1.0 < (await robot.state(R1)).pose.x < 9.0
    await other  # R2 kept going
    assert (await robot.state(R2)).pose.x == 3.0


async def test_battery_low_refuses_motion_except_to_dock(services):
    robot = fast(services)
    robot._rover(R1).battery.pct = BATTERY_MOTION_MIN_PCT - 1
    with pytest.raises(RobotError) as exc:
        await robot.navigate(R1, [Pose2D(x=3.0, y=1.0)])
    assert exc.value.code == "battery_low"
    robot._rover(R1).pose = Pose2D(x=3.0, y=1.0)
    st = await robot.navigate(R1, [Pose2D(x=1.0, y=1.0, theta=0.0)])  # its dock
    assert (st.pose.x, st.pose.y) == (1.0, 1.0)


async def test_charging_on_dock_with_finite_realtime(services):
    robot = create_fake(services, **fleet_opts(realtime_factor=6000.0))
    robot._rover(R2).battery.pct = 50.0
    robot._rover(R2).battery.last_t = robot._now()
    await asyncio.sleep(0.05)  # ~5 sim-minutes
    st = await robot.state(R2)
    assert st.status == RobotStatus.CHARGING and st.charging and st.battery_pct > 55.0


async def test_rovers_yield_to_a_moving_rover_with_smaller_id(services):
    robot = fast(services)
    r1 = robot._rover(R1)
    r1.motion = "navigate"  # pretend R1 is parked mid-move right in R2's way
    r1.pose = Pose2D(x=3.0, y=2.2)
    try:
        assert robot._must_yield(robot._rover(R2), 2.6, 2.2)
        assert not robot._must_yield(r1, 2.6, 2.2)  # R1 never yields to R2
    finally:
        r1.motion = None


async def test_cameras_render_every_kind(services):
    robot = fast(services)
    assert robot.camera_ids() == [
        f"{R1}.front",
        f"{R1}.wrist",
        f"{R1}.basket",
        f"{R2}.front",
        f"{R2}.wrist",
        f"{R2}.basket",
        f"{STORE}.ceiling-1",
    ]
    await robot.navigate(R1, [AT_SALSA])
    front = await robot.capture_camera(f"{R1}.front")
    assert front.camera == "front" and front.slot_id == "S-salsa" and front.pose.y == 3.62
    assert {i.slot_id for i in front.instances} >= {"S-salsa"}
    wrist = await robot.capture(R1, "wrist")
    assert wrist.camera == "wrist" and wrist.camera_id == f"{R1}.wrist" and wrist.slot_id == "S-salsa"
    ceiling = await robot.capture_camera(f"{STORE}.ceiling-1")
    assert ceiling.camera == "ceiling" and ceiling.robot_id is None and ceiling.store_id == STORE
    assert ceiling.camera_pose is not None and ceiling.camera_pose.frame == "world"
    head = await robot.capture(R1, "head")  # v0.1 alias
    assert head.camera_id == f"{R1}.front"
    for f in (front, wrist, ceiling, head):
        assert not f.blurred
        jpeg(f)
    for bad in ("nope.front", "rover-t-1.thermal", "store_nowhere.ceiling-1", "garbage"):
        with pytest.raises(RobotError) as exc:
            await robot.capture_camera(bad)
        assert exc.value.code == "unknown_robot"


async def test_frames_stream_on_demand_and_stop_on_close(services):
    robot = create_fake(services, **fleet_opts(stream_fps=15.0))
    stream = robot.frames(camera_id=f"{STORE}.ceiling-1")
    first = await asyncio.wait_for(stream.__anext__(), 2.0)
    assert first.camera_id == f"{STORE}.ceiling-1" and first.instances == []
    by_robot = robot.frames(robot_id=R2)
    seen = set()
    while len(seen) < 3:
        f = await asyncio.wait_for(by_robot.__anext__(), 2.0)
        assert f.robot_id == R2
        seen.add(f.camera_id)
    assert seen == {f"{R2}.front", f"{R2}.wrist", f"{R2}.basket"}
    await stream.aclose()
    await by_robot.aclose()
    assert not robot._demand
    await asyncio.sleep(0.15)
    assert robot._streamer is None


async def test_sync_stock_restock_and_write_off(services):
    robot = fast(services, strict_reach=False)
    await robot.pick(R1, "S-salsa", "salsa-mild")  # S-salsa#00 leaves the shelf
    await robot.place_in_basket(R1)
    plan = services.catalog.plan
    salsa = plan.slot("S-salsa")
    salsa.qty = 6  # catalog: 3 left after the pick + a restock of 3
    await robot.sync_stock(STORE, ["S-salsa"])
    ids = [i.instance_id for i in await robot.list_instances(R1, "S-salsa")]
    assert ids == ["S-salsa#01", "S-salsa#02", "S-salsa#03", "S-salsa#04", "S-salsa#05", "S-salsa#06"]
    assert "S-salsa#00" in (await robot.state(R1)).basket  # never reused
    avo = plan.slot("S-avocado")
    avo.lots[1].qty = 0  # write-off of lot b
    avo.qty = 3
    await robot.sync_stock(STORE)
    units = await robot.list_instances(R1, "S-avocado")
    assert [i.lot_id for i in units] == ["L-avo-a"] * 3
    assert [i.instance_id for i in units] == ["S-avocado#00", "S-avocado#01", "S-avocado#02"]
    await FakeRobot(services).sync_stock(STORE)  # nothing loaded yet: no-op


async def test_reset_store_and_adopt_state(services):
    robot = fast(services)
    await robot.navigate(R1, [AT_SALSA])
    await robot.reset_store(STORE)
    assert (await robot.state(R1)).pose.x == 1.0
    st = (await robot.state(R2)).model_copy(
        update={
            "pose": AT_AVO,
            "basket": ["S-avocado#00"],
            "basket_skus": ["avocado"],
            "holding": "S-salsa#03",
            "battery_pct": 42.0,
        }
    )
    adopted = await robot.adopt_state(st)
    assert adopted.pose.x == 5.0 and adopted.battery_pct == pytest.approx(42.0, abs=0.1)
    assert adopted.basket == ["S-avocado#00"] and adopted.holding == "S-salsa#03"
    assert "S-avocado#00" not in [i.instance_id for i in await robot.list_instances(R2, "S-avocado")]
    await robot.place_in_basket(R2)
    assert (await robot.state(R2)).basket == ["S-avocado#00", "S-salsa#03"]


async def test_optional_state_event_publishing(services):
    quiet = fast(services)
    await quiet.reset(R1)
    assert services.events.events == []  # default: the gateway publishes robot.state, not the robot
    loud = fast(services, publish_state_events=True)
    loud.set_trip("trip_demo")
    await loud.navigate(R1, [Pose2D(x=3.0, y=1.0)])
    evs = services.events.events
    assert evs and all(e.type == "robot.state" and e.trip_id == "trip_demo" and e.robot_id == R1 for e in evs)


async def test_fleet_from_data_dir_when_present():
    """Default fleet (data/fixtures/robots.yaml, or the built-in fallback): Pico / Rhea / Hazel across two stores,
    a produce top-3 pick lands in the basket. Skipped until the data agent's files exist."""
    from utils.data import DataError, list_store_ids, load_planogram

    if "store_ave" not in list_store_ids():
        pytest.skip("data/planograms/store_ave.yaml not written yet")
    try:
        plan = load_planogram("store_ave")
    except DataError as exc:
        pytest.skip(f"store_ave planogram invalid right now: {exc}")
    robot = FakeRobot(Services(), realtime_factor=math.inf, render_in_thread=False)
    ids = robot.robot_ids()
    assert {"rover-ave-1", "rover-ave-2"} <= set(ids)
    assert robot.spec("rover-ave-1").name == "Pico"
    assert set(robot.robot_ids("store_ave")) >= {"rover-ave-1", "rover-ave-2"}
    insts = [i for i in await robot.list_instances("rover-ave-1") if not i.in_basket]
    assert len(insts) == sum(s.qty for s in plan.slots)
    assert len({i.instance_id for i in insts}) == len(insts)
    produce = next((s for s in plan.slots if s.qty > 0 and any(lot.freshness is not None for lot in s.lots)), None)
    if produce is None:
        pytest.skip("no produce slot with lot freshness in store_ave yet")
    robot._rover("rover-ave-1").pose = produce.approach_pose  # teleport (routing is another arm)
    frame = await robot.scan("rover-ave-1", produce.slot_id)
    top3 = sorted(frame.instances, key=lambda i: -(i.freshness or 0))[:3]
    await robot.pick("rover-ave-1", produce.slot_id, produce.sku, top3[0].instance_id)
    st = await robot.place_in_basket("rover-ave-1")
    assert st.basket == [top3[0].instance_id]
    cams = robot.camera_ids(store_id="store_ave")
    assert "store_ave.ceiling-1" in cams and "rover-ave-1.front" in cams
    jpeg(await robot.capture_camera("store_ave.ceiling-1"))
