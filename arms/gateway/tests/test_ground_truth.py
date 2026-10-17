"""The robot fake and the Isaac scene share ground truth (composition-root test: imports two arms' public APIs).

The gateway falls back from the sim robot to the fake mid-trip, so instance ids seen in sim frames (PickChoice), the
per-unit freshness behind the produce top-3, and the basket drop pose must be identical in both (all three come from
contracts.sim: instance_id, unit_freshness, BASKET_OFFSET_LOCAL).
"""

from arms.gateway.tests.helpers import needs_data
from arms.robot import build_instances
from arms.sim import build_scene_spec
from utils.data import load_planogram, load_products


@needs_data
def test_robot_fake_and_sim_scene_agree_on_store_ave() -> None:
    plan = load_planogram("store_ave")
    by_sku = load_products()
    fake = {i.instance_id: i for insts in build_instances(plan, by_sku).values() for i in insts}
    scene = build_scene_spec(plan, by_sku)
    assert scene.instances, "store_ave scene has no product instances"
    for inst in scene.instances:  # the sim may show fewer units than fit; every one it shows exists in the fake
        twin = fake.get(inst.instance_id)
        assert twin is not None, inst.instance_id
        assert (twin.sku, twin.slot_id, twin.lot_id, twin.freshness) == (
            inst.sku,
            inst.slot_id,
            inst.lot_id,
            inst.freshness,
        ), inst.instance_id
    assert any(i.freshness is not None for i in scene.instances), "no produce freshness to rank"
