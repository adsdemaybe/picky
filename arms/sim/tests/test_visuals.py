"""Store visuals (arms/sim/visuals.yaml): theme, table style, models and decor, checked on the real store data."""

import pytest

from arms.sim import build_scene_spec
from arms.sim.scene_spec import FIXTURE_COLORS, WALL_COLOR
from arms.sim.visuals import load_visuals, store_visuals
from utils.data import load_planogram, load_products

STORE = "store_tmobile"


@pytest.fixture(scope="module")
def spec():
    return build_scene_spec(load_planogram(STORE), load_products())


def test_every_visual_points_at_a_known_asset_and_sku():
    vis = load_visuals()
    skus = set(load_products())
    for sku, pv in vis.products.items():
        assert pv.asset in vis.assets, sku
        assert sku in skus, f"visuals.yaml maps unknown sku {sku}"
    for store in vis.stores.values():
        for d in store.decor:
            assert d.kind != "asset" or d.asset in vis.assets, d.name


def test_theme_only_changes_its_store(spec):
    theme = store_visuals(STORE)
    walls = [p for p in spec.prims if p.role == "wall"]
    assert walls and all(p.color == theme.colors["wall"] for p in walls)
    grocery = build_scene_spec(load_planogram("store_hill"), load_products())
    assert all(p.color == WALL_COLOR for p in grocery.prims if p.role == "wall")
    assert not [p for p in grocery.prims if p.role in ("decor", "sign")]
    shelves = [p for p in grocery.prims if p.role == "board" and p.fixture_id == "WALL-L"]
    assert shelves and all(p.color == FIXTURE_COLORS["shelf"] for p in shelves)


def test_tables_hold_one_layer_side_by_side(spec):
    plan = load_planogram(STORE)
    for slot in [s for s in plan.slots if s.fixture_id in ("TABLE-A", "TABLE-B")]:
        layout = spec.slot_layouts[slot.slot_id]
        zs = {round(z, 4) for _, _, z in layout.positions}
        assert len(zs) == 1, f"{slot.slot_id} units are stacked"
        front = layout.positions[: min(slot.qty, 4)]
        face = slot.slot_id.rsplit("-", 2)[1][0]  # pick-face letter, e.g. W in TMO-TABLE-A-W0-00
        lateral = {round(y if face in "EW" else x, 3) for x, y, _ in front}
        assert len(lateral) == len(front), f"{slot.slot_id} front units are not side by side"
    tops = [p for p in spec.prims if p.fixture_id == "TABLE-A" and p.role == "board"]
    assert len(tops) == 1 and tops[0].color == store_visuals(STORE).table.top_color


def test_models_and_price_cards(spec):
    units = [p for p in spec.prims if p.role == "product"]
    phones = [p for p in units if p.sku == "IP17PRO-256"]
    assert phones and all(p.asset == "phone_iphone_blue" and p.display == "stand" for p in phones)
    assert sum(p.price_card for p in phones) == 1  # one card per slot
    buds = sorted((p for p in units if p.sku == "APP3"), key=lambda p: p.instance_id)
    assert buds[0].asset == "earbuds_white" and buds[0].model_dims is not None
    assert all(p.asset is None for p in buds[1:])  # the stock behind the display unit stays boxed
    boxes = [p for p in units if p.sku == "CASE-IP16-MAG"]
    assert boxes and all(p.asset is None for p in boxes)


def test_decor_and_signs_are_visual_only(spec):
    decor = [p for p in spec.prims if p.role in ("decor", "sign")]
    assert {p.label_text for p in decor if p.role == "sign"} >= {"T-Mobile", "Accessories"}
    assert all(not p.collider for p in decor)
    assert len(spec.instances) == sum(min(s.qty, 120) for s in load_planogram(STORE).slots)
