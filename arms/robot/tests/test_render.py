"""Pillow frames: valid JPEGs, marked synthetic, boxes inside the image, every unit of a scanned slot listed."""

import io

from PIL import Image

from arms.robot.render import (
    JPEG_COMMENT,
    RoverView,
    SceneView,
    ceiling_window,
    facing_slots,
    render_basket,
    render_ceiling,
    render_front,
    render_wrist,
)
from arms.robot.tests.conftest import R1, make_cameras, make_planogram, make_products
from arms.robot.world import StoreWorld
from contracts.common import Pose2D


def _scene() -> tuple[StoreWorld, SceneView]:
    world = StoreWorld.build(make_planogram(), make_products())
    scene = SceneView(
        plan=world.plan,
        products=world.products,
        units={k: list(v) for k, v in world.shelves.items()},
        tags={s.slot_id: world.price_tag_cents(s) for s in world.plan.slots},
    )
    return world, scene


def _rover(pose: Pose2D, **kw) -> RoverView:
    return RoverView(robot_id=R1, name="Tess", color="#2563eb", pose=pose, **kw)


def _check(r) -> Image.Image:  # noqa: ANN001
    img = Image.open(io.BytesIO(r.jpeg))
    assert img.format == "JPEG" and img.size == (r.w, r.h) and img.info.get("comment") == JPEG_COMMENT
    for i in r.instances:
        if i.bbox is not None:
            assert 0 <= i.bbox.x and 0 <= i.bbox.y
            assert i.bbox.x + i.bbox.w <= r.w + 0.5 and i.bbox.y + i.bbox.h <= r.h + 0.5
    return img


def test_scan_view_lists_every_unit_of_the_slot():
    world, scene = _scene()
    slot = world.plan.slot("S-avocado")
    r = render_front(scene, _rover(slot.approach_pose), focus=slot)
    _check(r)
    assert r.slot_id == "S-avocado"
    assert [i.instance_id for i in r.instances] == [u.instance_id for u in world.units("S-avocado")]
    assert all(i.bbox is not None for i in r.instances)


def test_front_view_sees_the_facing_shelf_and_an_empty_aisle():
    world, scene = _scene()
    at = world.plan.slot("S-salsa").approach_pose
    assert {s.slot_id for s, _, _ in facing_slots(world.plan, at)} >= {"S-salsa"}
    r = render_front(scene, _rover(at, task="pick Mild Salsa"))
    _check(r)
    assert r.slot_id == "S-salsa" and r.instances
    away = render_front(scene, _rover(Pose2D(x=5.0, y=2.0, theta=-1.57)))
    _check(away)
    assert away.instances == [] and away.slot_id is None


def test_wrist_basket_and_ceiling_views():
    world, scene = _scene()
    slot = world.plan.slot("S-salsa")
    held = world.units("S-salsa")[0]
    _check(render_wrist(scene, _rover(slot.approach_pose), slot))
    w = render_wrist(scene, _rover(slot.approach_pose, holding=held), None)
    _check(w)
    assert [i.instance_id for i in w.instances] == [held.instance_id]
    oos = render_wrist(scene, _rover(slot.approach_pose), world.plan.slot("S-limes"))
    assert oos.instances == []
    basket = [u.model_copy(update={"in_basket": True}) for u in world.units("S-avocado")[:4]]
    b = render_basket(scene, _rover(slot.approach_pose, basket=basket))
    _check(b)
    assert len(b.instances) == 4 and all(i.bbox is not None for i in b.instances)
    rovers = [
        _rover(slot.approach_pose, status="navigating", route=[Pose2D(x=9.0, y=1.0)], dock=Pose2D(x=1, y=1)),
        RoverView(robot_id="rover-t-2", name="Uno", color="#db2777", pose=Pose2D(x=1.0, y=2.2)),
    ]
    c = render_ceiling(world.plan, rovers, make_cameras()[0])
    _check(c)
    assert ceiling_window(world.plan, make_cameras()[0]) == world.plan.bounds  # ceiling-1 = whole store
