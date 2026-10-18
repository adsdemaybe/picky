import math

import pytest

from arms.sim import build_scene_spec
from arms.sim.scene_spec import (
    BASKET_OFFSET_M,
    BASKET_SIZE_M,
    FRESHNESS_JITTER,
    STORE_GAP_M,
    basket_drop_position,
    build_rover_spec,
    build_world_spec,
    default_robots,
    item_yaw_for,
    slot_lateral_room,
    store_offsets,
    usd_name,
)
from arms.sim.tests.sim_fixtures import planogram, planogram2, products, robots, store_cameras
from contracts.common import Pose2D
from contracts.sim import BASKET_OFFSET_LOCAL, instance_id, planogram_hash, unit_freshness


@pytest.fixture
def spec():
    return build_scene_spec(planogram(), products(), seed=7)


def test_one_prim_and_instance_per_stocked_unit(spec):
    prods = spec.product_prims()
    assert len(prods) == len(spec.instances) == 6 + 3 + 6  # salsa + tortillas + avocados; OOS + unknown -> none
    assert {p.instance_id for p in prods} == {i.instance_id for i in spec.instances}
    assert spec.instances_in_slot("s_beans_e") == []
    assert len({p.path for p in spec.prims}) == len(spec.prims)  # unique USD paths
    assert all(p.path.startswith("/World/Stores/store_test/") for p in spec.prims)
    assert spec.root_path == "/World/Stores/store_test"
    assert all(i.store_id == "store_test" for i in spec.instances)


def test_deterministic_and_shared_with_the_robot_fake(spec):
    again = build_scene_spec(planogram(), products(), seed=7)
    assert again.model_dump() == spec.model_dump()
    other = build_scene_spec(planogram(), products(), seed=8)
    assert [i.freshness for i in other.instances_in_slot("s_avo")] == [
        i.freshness for i in spec.instances_in_slot("s_avo")
    ]
    for inst in spec.instances:
        n = int(inst.instance_id.rsplit("#", 1)[1])
        assert inst.instance_id == instance_id(inst.slot_id, n)
        lot = next((lt for lt in planogram().slot(inst.slot_id).lots if lt.lot_id == inst.lot_id), None)
        assert inst.freshness == unit_freshness(lot.freshness if lot else None, inst.instance_id)


def test_hash_and_store_poses(spec):
    plan = planogram()
    assert spec.data_hash == planogram_hash(plan)
    assert spec.robot_home == plan.entrance


def test_labels_face_the_approach_pose(spec):
    plan = planogram()
    for prim in spec.product_prims():
        slot = plan.slot(prim.slot_id)
        assert prim.yaw == pytest.approx(item_yaw_for(slot), abs=1e-5)
        label_normal = (math.sin(prim.yaw), -math.cos(prim.yaw))  # item-local -y in world
        to_robot = (slot.approach_pose.x - prim.center[0], slot.approach_pose.y - prim.center[1])
        assert label_normal[0] * to_robot[0] + label_normal[1] * to_robot[1] > 0, prim.instance_id


def test_items_sit_on_their_board_inside_the_fixture(spec):
    plan = planogram()
    for prim in spec.product_prims():
        slot = plan.slot(prim.slot_id)
        fx = plan.fixture(slot.fixture_id)
        h = prim.size[2]
        layer = round((prim.center[2] - h / 2 - slot.item_position.z) / h)
        assert prim.center[2] == pytest.approx(slot.item_position.z + h / 2 + layer * h, abs=1e-3)
        assert fx.rect.inflated(0.11).contains(prim.center[0], prim.center[1]), prim.instance_id
    first = spec.prim_for_instance("s_salsa#00")
    assert first.center[0] == pytest.approx(2.6 - 0.04, abs=1e-3)


def test_double_sided_units_stay_on_their_side(spec):
    tort = [p for p in spec.product_prims() if p.slot_id == "s_tort_w"]
    assert all(6.0 <= p.center[0] <= 6.5 for p in tort)


def test_lots_and_freshness_jitter(spec):
    avo = spec.instances_in_slot("s_avo")
    assert [i.lot_id for i in avo] == ["AV_OLD"] * 3 + ["AV_NEW"] * 3
    for inst in avo:
        base = 0.5 if inst.lot_id == "AV_OLD" else 0.9
        assert abs(inst.freshness - base) <= FRESHNESS_JITTER + 1e-9
    assert len({i.freshness for i in avo}) == 6
    salsa = spec.instances_in_slot("s_salsa")
    assert all(i.lot_id == "L1" and i.freshness is None for i in salsa)


def test_produce_spread_side_by_side(spec):
    avo = [p for p in spec.product_prims() if p.slot_id == "s_avo"]
    assert len({round(p.center[0], 3) for p in avo}) >= 3


def test_fixture_prims(spec):
    f1 = [p for p in spec.prims if p.fixture_id == "f1" and p.role in ("board", "panel")]
    assert sum(p.role == "board" for p in f1) == 5  # 4 levels + top
    panel = next(p for p in f1 if p.role == "panel")
    assert panel.center[0] < 2.1
    g_panel = next(p for p in spec.prims if p.fixture_id == "g1" and p.role == "panel")
    assert g_panel.center[0] == pytest.approx(6.5)
    assert any(p.fixture_id == "p1" and p.role == "base" for p in spec.prims)
    assert any(p.fixture_id == "co" and p.role == "base" for p in spec.prims)
    assert len(spec.by_role("wall")) == 4 and len(spec.by_role("floor")) == 1
    assert spec.by_role("light")


def test_overflow_spawns_every_unit_with_a_warning():
    plan = planogram()
    plan.slot("s_tort_w").qty = 40
    spec = build_scene_spec(plan, products())
    assert any("does_not_exist" in w for w in spec.warnings)
    assert any("s_tort_w" in w for w in spec.warnings)
    # exact ground truth: every unit exists, so a scan count equals the catalog qty
    assert len(spec.instances_in_slot("s_tort_w")) == 40
    assert len({i.instance_id for i in spec.instances_in_slot("s_tort_w")}) == 40


def test_slot_layout_positions_front_first(spec):
    layout = spec.slot_layouts["s_salsa"]
    assert len(layout.positions) >= 6
    assert layout.position(0) == spec.prim_for_instance("s_salsa#00").center
    assert layout.position(len(layout.positions) + 3)  # overflow still has a position


def test_shelf_slot_fills_wide_from_the_center(spec):
    """6 salsa jars (2 facings in the data) spread across the slot's room in ONE front row, rank 0 on the tag."""
    plan = planogram()
    slot = plan.slot("s_salsa")
    room = slot_lateral_room(plan, slot, plan.fixture("f1"))
    assert 0.3 < room <= 0.6
    layout = spec.slot_layouts["s_salsa"]
    ip = slot.item_position
    front = [layout.position(k) for k in range(slot.qty)]
    assert len({round(p[0], 4) for p in front}) == 1  # every unit in the front row (same depth)
    assert front[0][1] == pytest.approx(ip.y)  # rank 0 = the unit on the tag (a default pick, straight ahead)
    ys = sorted(p[1] for p in front)
    assert ys[-1] - ys[0] <= room
    assert abs((ys[0] + ys[-1]) / 2 - ip.y) <= 0.08 / 2 + 0.01  # centered block (+- half a unit)
    assert len({round(y, 4) for y in ys}) == slot.qty  # side by side, no overlap


def test_neighbouring_slots_bound_the_room():
    plan = planogram()
    first = plan.slot("s_tort_w")
    moved = first.item_position.model_copy(update={"y": 6.3})
    plan.slots.append(first.model_copy(update={"slot_id": "s_tort_w2", "item_position": moved}))
    assert slot_lateral_room(plan, plan.slot("s_tort_w"), plan.fixture("g1")) == pytest.approx(0.3)
    # the beans slot faces the other side of the gondola (and sits on another level): not a neighbour
    assert slot_lateral_room(plan, plan.slot("s_beans_e"), plan.fixture("g1")) == pytest.approx(0.6)


def test_basket_drop_keeps_big_units_inside_the_basket():
    pose = Pose2D(x=0, y=0, theta=0.0)
    bx, by, _ = BASKET_SIZE_M
    for k in range(12):
        p = basket_drop_position(pose, k, (0.2, 0.05, 0.25))  # a tortilla pack: 0.2 m wide along the robot's y
        assert abs(p.y - BASKET_OFFSET_M[1]) + 0.2 / 2 <= by / 2 + 1e-9
        assert abs(p.x - BASKET_OFFSET_M[0]) + 0.05 / 2 <= bx / 2 + 1e-9


def test_basket_drop_is_behind_the_robot():
    pose = Pose2D(x=5, y=5, theta=math.pi / 2)  # facing +y -> basket at -y
    p = basket_drop_position(pose, 0)
    assert p.y < 5 and abs(p.x - 5) < 0.2
    assert p.z > BASKET_OFFSET_M[2]
    assert basket_drop_position(pose, 6).z > p.z
    assert BASKET_OFFSET_M == BASKET_OFFSET_LOCAL


def test_usd_name():
    assert usd_name("store_ave:aisle-3/s1") == "store_ave_aisle_3_s1"
    assert usd_name("3abc") == "_3abc"


def test_rover_spec_has_lift_arm_basket_and_cameras():
    rover = build_rover_spec(robots()[0])
    groups = {p.group for p in rover.prims}
    assert groups == {"base", "lift", "arm"}
    names = {p.path.rsplit("/", 1)[1] for p in rover.prims}
    assert {"chassis", "mast", "carriage", "basket_floor", "front_cam_head", "so101_jaw_fixed"} <= names
    assert rover.root_path == "/World/Stores/store_test/Robots/rover_test_1"
    assert rover.camera_ids == ["rover-test-1.front", "rover-test-1.wrist", "rover-test-1.basket"]
    # the arm reaches ~0.35 m from the mast
    jaw = next(p for p in rover.prims if p.path.endswith("so101_jaw_fixed"))
    assert 0.35 < jaw.center[0] < 0.55


def test_world_spec_two_stores_side_by_side():
    world = build_world_spec([planogram(), planogram2()], products(), robots(), store_cameras())
    assert set(world.stores) == {"store_test", "store_two"}
    off_a, off_b = world.offsets["store_test"], world.offsets["store_two"]
    assert off_a == (0.0, 0.0, 0.0)
    assert off_b[0] >= 22 + STORE_GAP_M and off_b[1:] == (0.0, 0.0)
    assert set(world.rovers) == {"rover-test-1", "rover-test-2", "rover-two-1"}
    ids = [c.camera_id for c in world.cameras]
    assert "rover-two-1.wrist" in ids and "store_test.ceiling-1" in ids and "store_two.ceiling-1" in ids
    assert all(c.source == "isaac" for c in world.cameras)


def test_store_offsets_grow_with_big_stores():
    offs = store_offsets(["a", "b", "c"], {"a": (14, 22), "b": (80, 30), "c": (9, 14)})
    assert offs["b"][0] >= 80 + STORE_GAP_M
    assert offs["c"][0] == 2 * offs["b"][0]


def test_default_robots_follow_the_id_convention():
    (r,) = default_robots(planogram())
    assert r.robot_id == "rover-test-1" and r.store_id == "store_test"


def test_real_demo_stores_if_data_present():
    from utils.data import DataError, load_planogram, load_products

    try:
        prods = load_products()
        plans = [load_planogram("store_ave"), load_planogram("store_hill")]
    except (DataError, FileNotFoundError, KeyError) as exc:
        pytest.skip(f"data/ not ready: {exc}")
    for plan in plans:
        spec = build_scene_spec(plan, prods)
        for slot in plan.slots:
            if slot.qty > 0 and slot.sku in prods:
                assert len(spec.instances_in_slot(slot.slot_id)) == min(slot.qty, 120), slot.slot_id
        assert not [w for w in spec.warnings if "unknown sku" in w]
