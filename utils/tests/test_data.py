"""utils.data loaders against a tiny inline data dir."""

import textwrap
from pathlib import Path

import pytest

from utils import data as d
from utils.config import reset_settings, set_settings

PRODUCTS = """
products:
  - sku: salsa_hot
    name: Hot Salsa
    category: pantry
    tags: [salsa, spicy]
    spice_level: 4
    price_cents: 399
    barcode: "4006381333931"
    asset: {shape: cylinder, dims_m: [0.08, 0.08, 0.12], label_color: "#c0392b"}
  - sku: avocado
    name: Avocado
    category: produce
    price_cents: 129
    barcode: "4006381333948"
    perishable: true
    shelf_life_days: 5
    asset: {shape: sphere, dims_m: [0.08, 0.08, 0.1], label_color: "#2e7d32"}
  - sku: sour_cream_online
    name: Sour Cream
    category: dairy
    price_cents: 249
    barcode: "4006381333955"
    online_only: true
"""

PLANOGRAM = """
store_id: store_ave
name: Picky Ave
bounds: {x: 0, y: 0, w: 14, h: 22}
entrance: {x: 1, y: 1.0, theta: 1.5708}
checkout: {x: 3, y: 1.5}
fixtures:
  - {fixture_id: f1, kind: shelf, aisle_id: a1, rect: {x: 2, y: 4, w: 0.6, h: 8}, yaw: 0.0, levels: 4}
  - {fixture_id: p1, kind: produce_bin, rect: {x: 10, y: 4, w: 1.2, h: 2}, levels: 1}
aisles:
  - {aisle_id: a1, label: Aisle 1, rect: {x: 2.6, y: 4, w: 1.6, h: 8}, categories: [pantry]}
slots:
  - slot_id: a1-f1-L1-0
    fixture_id: f1
    aisle_id: a1
    sku: salsa_hot
    level: 1
    index: 0
    item_position: {x: 2.55, y: 5.0, z: 0.6}
    approach_pose: {x: 3.2, y: 5.0, theta: 3.14159}
    qty: 6
    lots: [{lot_id: L1, qty: 6, expiry: 2026-10-20}]
  - slot_id: prod-avocado
    fixture_id: p1
    sku: avocado
    item_position: {x: 10.6, y: 5.0, z: 0.9}
    approach_pose: {x: 9.3, y: 5.0, theta: 0.0}
    qty: 9
    lots: [{lot_id: A1, qty: 9, freshness: 0.8}]
    deal: {kind: near_expiry, price_cents: 99, original_cents: 129, reason: Ripe today}
"""

ONLINE = """
store_id: online
items:
  - {sku: sour_cream_online, price_cents: 279}
  - {sku: salsa_hot}
"""

USERS = """
users:
  - user_id: maya
    profile:
      user_id: maya
      display_name: Maya
      phone: "+15555550100"
      prefs: {spicy: 0.6}
      favorite_skus: [salsa_hot]
    mandate:
      mandate_id: m_maya
      user_id: maya
      cap_cents: 4000
      ask_above_cents: 1500
      expires_at: 2026-12-31
    purchases:
      - {purchase_id: p1, user_id: maya, sku: avocado, qty: 2, unit_price_cents: 129, store_id: store_ave}
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


@pytest.fixture
def data_root(tmp_path: Path):
    _write(tmp_path, "products.yaml", PRODUCTS)
    _write(tmp_path, "planograms/store_ave.yaml", PLANOGRAM)
    _write(tmp_path, "online_store.yaml", ONLINE)
    _write(tmp_path, "fixtures/users.yaml", USERS)
    set_settings(picky_data_dir=str(tmp_path))
    d.clear_cache()
    yield tmp_path
    reset_settings()


def test_data_dir_resolution(tmp_path: Path):
    try:
        set_settings(picky_data_dir="data")
        assert d.data_dir() == d.REPO_ROOT / "data"
        set_settings(picky_data_dir="somewhere")  # PICKY_DATA_DIR comes from utils.config.settings (env or .env)
        assert d.data_dir() == d.REPO_ROOT / "somewhere"
        assert d.data_dir(tmp_path) == tmp_path
        assert d.data_dir("rel") == d.REPO_ROOT / "rel"
    finally:
        reset_settings()


def test_load_products(data_root: Path):
    products = d.load_products()
    assert list(products) == ["salsa_hot", "avocado", "sour_cream_online"]
    assert products["salsa_hot"].asset.shape == "cylinder"
    assert products["sour_cream_online"].online_only


def test_load_planogram_and_store_ids(data_root: Path):
    assert d.list_store_ids() == ["store_ave"]
    assert d.list_store_ids(include_online=True) == ["store_ave", "online"]
    plan = d.load_planogram("store_ave")
    assert plan.slot("prod-avocado").deal.price_cents == 99
    assert set(d.load_planograms()) == {"store_ave"}
    with pytest.raises(d.DataError, match="no planogram"):
        d.load_planogram("store_hill")


def test_load_online_and_fixtures(data_root: Path):
    online = d.load_online_catalog()
    assert online.item("sour_cream_online").price_cents == 279
    fixtures = d.load_demo_fixtures()
    maya = d.load_demo_user("maya")
    assert fixtures.users[0].user_id == "maya"
    assert maya.mandate.cap_cents == 4000
    with pytest.raises(KeyError):
        d.load_demo_user("nobody")


def test_cache_returns_copies_and_sees_edits(data_root: Path):
    plan = d.load_planogram("store_ave")
    plan.slots[0].qty = 0
    assert d.load_planogram("store_ave").slots[0].qty == 6  # cache not mutated
    _write(data_root, "planograms/store_ave.yaml", PLANOGRAM.replace("name: Picky Ave", "name: Picky Avenue"))
    import os

    st = (data_root / "planograms/store_ave.yaml").stat()
    os.utime(data_root / "planograms/store_ave.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert d.load_planogram("store_ave").name == "Picky Avenue"


def test_bare_list_and_errors(data_root: Path):
    _write(data_root, "fixtures/users.yaml", USERS.replace("users:\n", "", 1))
    d.clear_cache()
    assert d.load_demo_fixtures().user("maya") is not None
    _write(data_root, "products.yaml", "products:\n  - sku: x\n    name: X\n")
    d.clear_cache()
    with pytest.raises(d.DataError, match="ProductsFile"):
        d.load_products()


def test_store_id_must_match_filename(data_root: Path):
    _write(data_root, "planograms/store_hill.yaml", PLANOGRAM)
    with pytest.raises(d.DataError, match="does not match"):
        d.load_planogram("store_hill")


def test_validate_data(data_root: Path):
    assert d.validate_data() == []
    _write(data_root, "planograms/store_ave.yaml", PLANOGRAM.replace("sku: avocado", "sku: mystery"))
    d.clear_cache()
    problems = d.validate_data()
    assert any("unknown sku 'mystery'" in p for p in problems)


def test_validate_flags_online_only_in_store(data_root: Path):
    _write(data_root, "planograms/store_ave.yaml", PLANOGRAM.replace("sku: avocado", "sku: sour_cream_online"))
    d.clear_cache()
    assert any("online_only" in p for p in d.validate_data())


def _plan_with(data_root: Path, *pairs: tuple[str, str]) -> list[str]:
    text = PLANOGRAM
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    _write(data_root, "planograms/store_ave.yaml", text)
    d.clear_cache()
    return d.validate_data()


def test_validate_robot_clearance(data_root: Path):
    problems = _plan_with(data_root, ("approach_pose: {x: 9.3, y: 5.0", "approach_pose: {x: 9.6, y: 5.0"))
    assert any("prod-avocado: approach_pose" in p and "fixture p1" in p for p in problems)
    problems = _plan_with(data_root, ("entrance: {x: 1, y: 1.0", "entrance: {x: 1, y: 0.3"))
    assert any("entrance" in p and "outline" in p for p in problems)


def test_validate_approach_heading_and_geometry(data_root: Path):
    problems = _plan_with(data_root, ("{x: 3.2, y: 5.0, theta: 3.14159}", "{x: 3.2, y: 5.0, theta: 0.0}"))
    assert any("does not face item_position" in p for p in problems)
    problems = _plan_with(data_root, ("item_position: {x: 2.55, y: 5.0,", "item_position: {x: 2.55, y: 13,"))
    assert any("not on fixture f1" in p for p in problems)
    problems = _plan_with(
        data_root,
        ("aisles:\n", "aisles:\n  - {aisle_id: a2, label: Aisle 2, rect: {x: 5, y: 4, w: 1.6, h: 8}}\n"),
        ("    aisle_id: a1\n    sku: salsa_hot", "    aisle_id: a2\n    sku: salsa_hot"),
    )
    assert any("aisle_id 'a2' != fixture aisle_id 'a1'" in p for p in problems)


def test_validate_level_z_and_deal_lot(data_root: Path):
    problems = _plan_with(data_root, ("yaw: 0.0, levels: 4}", "yaw: 0.0, levels: 4, level_z_m: [0.1, 0.5, 0.9, 1.3]}"))
    assert any("level_z_m[1]" in p for p in problems)
    problems = _plan_with(data_root, ("reason: Ripe today}", "reason: Ripe today, lot_id: NOPE}"))
    assert any("deal lot_id 'NOPE'" in p for p in problems)


def test_validate_barcode_check_digit(data_root: Path):
    _write(data_root, "products.yaml", PRODUCTS.replace("4006381333948", "4006381333949"))
    d.clear_cache()
    assert any("EAN-13 check digit" in p for p in d.validate_data())


def test_validate_mandate_rules(data_root: Path):
    _write(data_root, "fixtures/users.yaml", USERS.replace("expires_at: 2026-12-31", "expires_at: 2020-01-01"))
    d.clear_cache()
    problems = d.validate_data()
    assert any(d.is_warning(p) and "mandate expires" in p for p in problems)
    _write(data_root, "fixtures/users.yaml", USERS.replace("ask_above_cents: 1500", "ask_above_cents: 5000"))
    d.clear_cache()
    assert any("ask_above_cents 5000 > cap_cents" in p for p in d.validate_data())
    blocked = USERS.replace("ask_above_cents: 1500", "ask_above_cents: 1500\n      blocked_categories: [produce]")
    _write(data_root, "fixtures/users.yaml", blocked)
    d.clear_cache()
    assert any("blocks category 'produce'" in p for p in d.validate_data())


def test_main_exit_codes(data_root: Path, capsys: pytest.CaptureFixture[str]):
    assert d.main() == 0
    _write(data_root, "fixtures/users.yaml", USERS.replace("expires_at: 2026-12-31", "expires_at: 2020-01-01"))
    d.clear_cache()
    assert d.main() == 0  # warnings only
    assert "warning:" in capsys.readouterr().out
