import math

import pytest

from arms.sim import build_scene_spec
from arms.sim.scene_spec import (
    BASKET_OFFSET_M,
    FRESHNESS_JITTER,
    basket_drop_position,
    item_yaw_for,
    usd_name,
)
from arms.sim.tests.sim_fixtures import planogram, products
from contracts.common import Pose2D
from contracts.sim import instance_id, planogram_hash, unit_freshness


@pytest.fixture
def spec():
    return build_scene_spec(planogram(), products(), seed=7)


def test_one_prim_and_instance_per_stocked_unit(spec):
    prods = spec.product_prims()
    assert len(prods) == len(spec.instances) == 6 + 3 + 6  # salsa + tortillas + avocados; OOS + unknown -> none
    assert {p.instance_id for p in prods} == {i.instance_id for i in spec.instances}
    assert spec.instances_in_slot("s_beans_e") == []
    assert len({p.path for p in spec.prims}) == len(spec.prims)  # unique USD paths
    assert all(p.path.startswith("/World/") for p in spec.prims)


def test_deterministic_and_shared_with_the_robot_fake(spec):
    again = build_scene_spec(planogram(), products(), seed=7)
    assert again.model_dump() == spec.model_dump()
    # ids + freshness come from contracts.sim, independent of the seed, so a mid-trip fallback to the robot fake
    # (which uses the same helpers) keeps PickChoice ids and the top-3 ranking
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
    assert spec.store_id == "store_test"


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
    # front unit of the salsa slot is pushed back by half its depth from the pick-face edge
    first = spec.prim_for_instance("s_salsa#00")
    assert first.center[0] == pytest.approx(2.6 - 0.04, abs=1e-3)


def test_double_sided_units_stay_on_their_side(spec):
    tort = [p for p in spec.product_prims() if p.slot_id == "s_tort_w"]
    assert all(6.0 <= p.center[0] <= 6.5 for p in tort)  # west half of the 1 m gondola


def test_lots_and_freshness_jitter(spec):
    avo = spec.instances_in_slot("s_avo")
    assert [i.lot_id for i in avo] == ["AV_OLD"] * 3 + ["AV_NEW"] * 3
    for inst in avo:
        base = 0.5 if inst.lot_id == "AV_OLD" else 0.9
        assert abs(inst.freshness - base) <= FRESHNESS_JITTER + 1e-9
    assert len({i.freshness for i in avo}) == 6  # distinct -> top-3 ranking is meaningful
    salsa = spec.instances_in_slot("s_salsa")
    assert all(i.lot_id == "L1" and i.freshness is None for i in salsa)


def test_produce_spread_side_by_side(spec):
    avo = [p for p in spec.product_prims() if p.slot_id == "s_avo"]
    assert len({round(p.center[0], 3) for p in avo}) >= 3  # several units visible across the bin front


def test_fixture_prims(spec):
    f1 = [p for p in spec.prims if p.fixture_id == "f1" and p.role in ("board", "panel")]
    assert sum(p.role == "board" for p in f1) == 4
    panel = next(p for p in f1 if p.role == "panel")
    assert panel.center[0] < 2.1  # back panel on the -x edge (pick face points +x)
    g_panel = next(p for p in spec.prims if p.fixture_id == "g1" and p.role == "panel")
    assert g_panel.center[0] == pytest.approx(6.5)  # gondola center divider
    assert any(p.fixture_id == "p1" and p.role == "base" for p in spec.prims)
    assert any(p.fixture_id == "co" and p.role == "base" for p in spec.prims)
    assert len(spec.by_role("wall")) == 4 and len(spec.by_role("floor")) == 1


def test_unknown_sku_and_overflow_are_warnings():
    plan = planogram()
    slot = plan.slot("s_tort_w")
    slot.qty = 500
    spec = build_scene_spec(plan, products())
    assert any("does_not_exist" in w for w in spec.warnings)
    assert any("s_tort_w" in w and "fit" in w for w in spec.warnings)
    assert 0 < len(spec.instances_in_slot("s_tort_w")) <= 24


def test_basket_drop_is_behind_the_robot():
    pose = Pose2D(x=5, y=5, theta=math.pi / 2)  # facing +y -> basket at -y
    p = basket_drop_position(pose, 0)
    assert p.y < 5 and abs(p.x - 5) < 0.2
    assert p.z > BASKET_OFFSET_M[2]
    assert basket_drop_position(pose, 4).z > p.z  # 5th item stacks


def test_usd_name():
    assert usd_name("store_ave:aisle-3/s1") == "store_ave_aisle_3_s1"
    assert usd_name("3abc") == "_3abc"


def test_real_demo_store_if_data_present():
    from utils.data import DataError, load_planogram, load_products

    try:
        plan, prods = load_planogram("store_ave"), load_products()
    except DataError as exc:
        pytest.skip(f"data/ not written yet: {exc}")
    spec = build_scene_spec(plan, prods)
    stocked = [s for s in plan.slots if s.qty > 0 and s.sku in prods]
    assert stocked, "store_ave has no stocked slots"
    for slot in stocked:
        assert spec.instances_in_slot(slot.slot_id), f"no sim units for {slot.slot_id}"
    assert not [w for w in spec.warnings if "unknown sku" in w]
