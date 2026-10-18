"""The T-Mobile wireless store in the REAL data/ dir: planogram + catalog + the fixtures that have no contracts model
yet (data/fixtures/wireless.yaml, data/sim/demo_units.yaml). Cart Whisperer and the interactive Isaac scene rely on
these invariants."""

from datetime import date
from pathlib import Path

import pytest
import yaml

from arms.catalog.fake import FakeCatalogService
from contracts.catalog import SearchQuery
from contracts.services import Services
from utils import data as data_io

DATA = Path(__file__).resolve().parents[3] / "data"
STORE = "store_tmobile"
DEMO_FIXTURES = {"TABLE-A", "TABLE-B", "TABLE-C", "WEAR-CASE"}
TEAMMATE_SKUS = {  # arms/frontend src/data/stores/tmobile.ts: these ids must keep resolving
    "PX9-128", "IP16-128", "GS25-128", "CASE-PX9-CLR", "CASE-PX9-RUG", "CASE-PX7-BLK", "CASE-IP16-MAG", "GLASS-PX9",
    "CHG-20W", "CHG-45W", "CBL-USBC-2M", "BUDS-PRO", "BUDS-RUN", "WATCH-PX3", "GW-5G", "PB-10K",
}  # fmt: skip
NOT_IN_CATALOG = {"PX7"}  # Maya's Pixel 7: owned, not sold


@pytest.fixture(scope="module")
def plan():
    if STORE not in data_io.list_store_ids(DATA):
        pytest.skip("no store_tmobile planogram")
    return data_io.load_planogram(STORE, DATA)


@pytest.fixture(scope="module")
def products():
    return data_io.load_products(DATA)


def test_data_dir_validates_clean(plan):
    assert data_io.validate_data(DATA) == []


def test_catalog_keeps_teammate_skus_and_categories(plan, products):
    assert TEAMMATE_SKUS <= set(products)
    stocked = {s.sku for s in plan.slots}
    assert TEAMMATE_SKUS <= stocked
    wireless = {p.sku for p in products.values() if p.sku in stocked}
    cats = data_io.load_categories(DATA)
    assert {products[s].category for s in wireless} <= {c for c, d in cats.items() if d.parent_id is None}
    assert products["CHG-20W"].price_cents == 1499 and products["BUDS-RUN"].price_cents == 6999


def test_fits_tags_point_at_devices(products):
    devices = {s for s, p in products.items() if p.category in ("phone", "tablet", "home_internet")}
    for sku, p in products.items():
        for tag in p.tags:
            if tag.startswith("fits:"):
                target = tag.removeprefix("fits:").upper()
                assert target in devices or target in NOT_IN_CATALOG, (sku, tag)
    assert "fits:px7" in products["CASE-PX7-BLK"].tags and "fits:px9-128" in products["CASE-PX9-CLR"].tags


def test_demo_units_are_not_the_pick_location(plan):
    """Every tethered demo unit (qty 1, cap 1, low 0) has a boxed BACK-STOCK slot listed BEFORE it, so the first
    stocked location (what the orchestrator / brain pick from) is the box."""
    order = {s.slot_id: i for i, s in enumerate(plan.slots)}
    demo = [s for s in plan.slots if s.fixture_id in DEMO_FIXTURES]
    assert len(demo) == 33
    for s in demo:
        assert (s.qty, s.capacity, s.low_threshold) == (1, 1, 0), s.slot_id
        back = [b for b in plan.slots_for(s.sku) if b.fixture_id == "BACK-STOCK"]
        assert len(back) == 1 and order[back[0].slot_id] < order[s.slot_id], s.sku


async def test_fake_catalog_serves_the_store(plan):
    cat = FakeCatalogService(Services(), data_root=DATA, today=date(2026, 10, 17))
    assert STORE in {s.store_id for s in await cat.list_stores()}
    locs = await cat.get_locations("PX9-128", STORE)
    first = next(loc for loc in locs if loc.qty > 0)
    assert first.fixture_id == "BACK-STOCK"
    alerts = {a.sku: a.status for a in await cat.stock_alerts(STORE)}
    assert alerts == {"CASE-IP18P-CLR": "out", "GLASS-IP18P-XTR6": "low", "IPDUO-256": "low"}
    deals = {d.sku: d.price_cents for d in await cat.get_deals(STORE)}
    assert deals == {"GLASS-PX9": 3999, "CASE-PX7-BLK": 999, "WATCH-PX3": 39999}
    hits = await cat.search(SearchQuery(text="pixel 9 case", store_ids=[STORE]))
    assert {h.product.sku for h in hits[:2]} == {"CASE-PX9-CLR", "CASE-PX9-RUG"}


def test_wireless_fixture_cross_refs(products):
    doc = yaml.safe_load((DATA / "fixtures" / "wireless.yaml").read_text())
    assert doc["store_id"] == STORE
    users = {u.user_id: u for u in data_io.load_demo_fixtures(DATA).users}
    purchases = {p.purchase_id: p for u in users.values() for p in u.purchases}
    for acct in doc["accounts"]:
        assert acct["user_id"] in users
        assert acct["phone"]["sku"] in products or acct["phone"]["sku"] in NOT_IN_CATALOG
        for item in acct["owned"]:
            assert item["sku"] in products
            rec = purchases[item["purchase_id"]]
            assert (rec.sku, rec.user_id, rec.store_id) == (item["sku"], acct["user_id"], STORE)
            assert rec.ts.date().isoformat() == item["bought"]
    for promo in doc["upcoming_promos"]:
        assert promo["price_cents"] < products[promo["sku"]].price_cents == promo["was_cents"]
    for promo in doc["line_promos"]:
        assert "sku" not in promo or promo["sku"] in products
    for sale in doc["online_sale_prices"]:
        assert sale["price_cents"] < products[sale["sku"]].price_cents


def test_demo_units_sim_hints_match_planogram(plan, products):
    doc = yaml.safe_load((DATA / "sim" / "demo_units.yaml").read_text())
    demo_slots = {s.slot_id: s for s in plan.slots if s.fixture_id in DEMO_FIXTURES}
    assert {u["slot_id"] for u in doc["units"].values()} == set(demo_slots)
    for sku, unit in doc["units"].items():
        assert demo_slots[unit["slot_id"]].sku == sku
        assert (unit.get("shown") == "open") == ("foldable" in products[sku].tags), sku
        if unit.get("shown") == "open":
            continue  # displayed unfolded: bigger than its box
        w, d, h = unit["demo_m"]
        bw, bd, bh = products[sku].asset.dims_m
        assert w < bw and d < bd and h <= bh + 1e-9, sku  # the device fits in its retail box
