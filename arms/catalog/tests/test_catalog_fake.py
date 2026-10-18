"""Fake catalog: Protocol conformance + behavior on a tiny taco-night fixture (tests/data, same schema as data/)."""

import importlib.util
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from arms.catalog import create, create_fake
from arms.catalog.fake import FakeCatalogService, text_score, tokenize
from arms.catalog.service import PostgresCatalogService
from contracts.catalog import SearchQuery
from contracts.common import Pose2D
from contracts.perception import Detection, ScanReport
from contracts.services import CatalogService, Services
from utils import data as data_io

TEST_DATA = Path(__file__).parent / "data"
TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


@pytest.fixture
def cat() -> FakeCatalogService:
    return FakeCatalogService(Services(), data_root=TEST_DATA, today=TODAY, now=lambda: NOW)


def scan(**kw) -> ScanReport:
    kw.setdefault("scan_id", "scan_t1")
    kw.setdefault("store_id", "store_ave")
    return ScanReport(**kw)


# ----------------------------------------------------------------------------------------------- conformance
def test_protocol_conformance_and_lazy_construction(tmp_path):
    fake = create_fake(Services())
    assert isinstance(fake, CatalogService)
    real = PostgresCatalogService(Services())  # must not need sqlalchemy / a DB at construction
    assert isinstance(real, CatalogService)
    if importlib.util.find_spec("sqlalchemy") is None:  # preflight: registry falls back to the fake
        with pytest.raises(RuntimeError, match="extra catalog"):
            create(Services())
    else:  # demo paths still raise NotImplementedError: create() refuses so the registry boots the fake (§6)
        with pytest.raises(RuntimeError, match="incomplete"):
            create(Services())
        assert isinstance(create(Services(), allow_incomplete=True), CatalogService)
    # construction never touches the disk: a missing data dir only fails at call time
    FakeCatalogService(data_root=tmp_path / "nope")


async def test_missing_data_fails_at_call_time(tmp_path):
    fake = FakeCatalogService(data_root=tmp_path / "nope")
    with pytest.raises(data_io.DataError):
        await fake.list_stores()


# ----------------------------------------------------------------------------------------------- reads
async def test_list_stores_and_planogram(cat):
    stores = {s.store_id: s for s in await cat.list_stores()}
    assert set(stores) == {"store_ave", "store_hill", "online"}
    assert stores["store_ave"].width_m == 14 and stores["store_ave"].depth_m == 22
    online = stores["online"]
    assert online.kind == "online" and not online.has_planogram
    assert online.shipping_cents == 599 and online.free_shipping_over_cents == 3500
    plan = await cat.get_planogram("store_ave")
    assert plan.slot("A1-1-0").sku == "tortillas_corn"
    with pytest.raises(KeyError):
        await cat.get_planogram("online")
    with pytest.raises(KeyError):
        await cat.get_planogram("nowhere")


async def test_planogram_is_a_copy(cat):
    plan = await cat.get_planogram("store_ave")
    plan.slot("A1-1-0").qty = 0
    assert (await cat.get_planogram("store_ave")).slot("A1-1-0").qty == 10


def test_tokenize_and_score():
    assert tokenize("Limes and Avocados for the tacos") == ["lime", "avocado", "taco"]
    assert tokenize("berries tomatoes") == ["berry", "tomato"]


@pytest.mark.parametrize(
    "text,sku",
    [
        ("tortillas", "tortillas_corn"),
        ("ground beef", "ground_beef"),
        ("black beans", "black_beans"),
        ("shredded cheese", "cheese_mexican"),
        ("avocados", "avocado"),
        ("limes", "lime"),
        ("cilantro", "cilantro"),
        ("sour cream", "sour_cream"),
        ("hot sauce", "hot_sauce"),
        ("guacamole", "guac_kit"),
    ],
)
async def test_taco_list_items_resolve(cat, text, sku):
    hits = await cat.search(SearchQuery(text=text))
    assert hits, text
    assert hits[0].product.sku == sku


async def test_search_salsa_ranks_and_spans_stores(cat):
    hits = await cat.search(SearchQuery(text="salsa"))
    skus = [h.product.sku for h in hits]
    assert set(skus[:2]) == {"salsa_mild", "salsa_hot"}
    hot = next(h for h in hits if h.product.sku == "salsa_hot")
    assert {o.store_id for o in hot.offers} == {"store_ave", "store_hill", "online"}
    online = hot.offer_for("online")
    assert online.price_cents == 399 and online.deal.deal_id == "online:salsa_hot" and online.eta_hours == 24
    spicy = await cat.search(SearchQuery(text="salsa", tags=["spicy"]))
    assert [h.product.sku for h in spicy] == ["salsa_hot"]


async def test_split_sourcing_black_beans_oos_in_store(cat):
    hit = (await cat.search(SearchQuery(text="black beans")))[0]
    ave = hit.offer_for("store_ave")
    assert ave.qty == 0 and not ave.in_stock and ave.location.slot_id == "A1-1-1"
    assert hit.offer_for("online").in_stock and hit.offer_for("online").price_cents == 149
    assert hit.cheapest_in_stock().store_id == "store_hill"  # 119 override
    only_ave = await cat.search(SearchQuery(text="black beans", store_ids=["store_ave"], in_stock_only=True))
    assert all(h.product.sku != "black_beans" for h in only_ave)
    ave_online = await cat.search(SearchQuery(text="black beans", store_ids=["store_ave", "online"]))
    assert {o.store_id for o in ave_online[0].offers} == {"store_ave", "online"}


async def test_search_filters(cat):
    veg = await cat.search(SearchQuery(text="taco", dietary=["vegetarian"], limit=50))
    skus = {h.product.sku for h in veg}
    assert "ground_beef" not in skus and "black_beans" in skus
    dairy = await cat.search(SearchQuery(category="dairy", limit=50))
    assert {h.product.sku for h in dairy} == {"cheese_mexican", "cotija", "sour_cream"}
    cheap = await cat.search(SearchQuery(text="salsa", max_price_cents=400))
    assert all(o.price_cents <= 400 for h in cheap for o in h.offers)
    assert await cat.search(SearchQuery(text="xyzzy")) == []
    assert len(await cat.search(SearchQuery(limit=3))) == 3


async def test_online_only_product(cat):
    hit = (await cat.search(SearchQuery(text="cotija")))[0]
    assert hit.product.online_only
    assert [o.store_id for o in hit.offers] == ["online"]
    assert hit.offers[0].eta_hours == 6
    assert await cat.get_locations("cotija") == []


async def test_locations_and_offers(cat):
    locs = await cat.get_locations("black_beans")
    assert {loc.store_id for loc in locs} == {"store_ave", "store_hill"}
    ave = (await cat.get_locations("avocado", "store_ave"))[0]
    assert ave.aisle_label == "Produce" and ave.approach_pose.y == 16.25 and ave.item_position.z == 0.8
    assert await cat.get_locations("avocado", "online") == []
    offers = await cat.get_offers("hot_sauce")
    assert len(offers) == 1
    o = offers[0]
    assert o.price_cents == 299 and o.list_price_cents == 399 and o.deal.deal_id == "store_ave:A1-2-0"
    sour = (await cat.get_offers("sour_cream", ["store_ave"]))[0]
    assert sour.price_cents == 259  # slot override
    assert await cat.get_product("nope") is None
    assert (await cat.get_product("lime")).name == "Limes"


async def test_deals(cat):
    deals = {d.deal_id: d for d in await cat.get_deals("store_ave")}
    assert set(deals) == {"store_ave:A1-2-0", "store_ave:D1-1-0"}
    guac = deals["store_ave:D1-1-0"]
    assert guac.kind == "near_expiry" and guac.lot_id == "guac-L1" and guac.price_cents == 299
    assert [d.deal_id for d in await cat.get_deals("online")] == ["online:salsa_hot"]
    assert await cat.get_deals("store_hill") == []
    with pytest.raises(KeyError):
        await cat.get_deals("nowhere")


async def test_near_expiry(cat):
    items = await cat.get_near_expiry("store_ave", within_days=3)
    assert [(i.sku, i.lot.lot_id, i.days_left) for i in items] == [("guac_kit", "guac-L1", 1), ("avocado", "avo-L1", 2)]
    guac, avo = items
    assert guac.price_cents == 299 and guac.original_cents == 599 and guac.deal is not None
    assert avo.price_cents == 129 and avo.deal is None
    assert len(await cat.get_near_expiry("store_ave", within_days=5)) == 3  # + beef (Oct 22)
    assert await cat.get_near_expiry("online") == []


# ----------------------------------------------------------------------------------------------- writes
async def test_record_pick_fifo_and_oos(cat):
    r = await cat.record_pick("store_ave", "D1-1-0", "guac_kit", qty=2, trip_id="trip_1")
    assert r.applied and (r.qty_before, r.qty_after) == (7, 5)
    plan = await cat.get_planogram("store_ave")
    lots = {lt.lot_id: lt.qty for lt in plan.slot("D1-1-0").lots}
    assert lots == {"guac-L1": 1, "guac-L2": 4}  # earliest expiry first
    await cat.record_pick("store_ave", "D1-1-0", "guac_kit")
    assert "store_ave:D1-1-0" not in {d.deal_id for d in await cat.get_deals("store_ave")}  # lot sold out
    offer = (await cat.get_offers("guac_kit", ["store_ave"]))[0]
    assert offer.price_cents == 599 and offer.deal is None

    r = await cat.record_pick("store_ave", "A1-1-3", "salsa_hot", qty=4)
    assert r.out_of_stock and r.qty_after == 0
    dash = await cat.merchant_dashboard("store_ave")
    assert "salsa_hot" in {o.sku for o in dash.out_of_stock}
    oos = next(o for o in dash.out_of_stock if o.sku == "salsa_hot")
    assert oos.last_qty == 4
    cell = next(c for c in dash.heatmap if c.slot_id == "A1-1-3")
    assert cell.picks == 4 and cell.oos_events == 1 and cell.value == 1.0
    assert dash.kpis["picks_total"] == 7


async def test_record_pick_bad_inputs(cat):
    assert not (await cat.record_pick("store_ave", "nope", "lime")).applied
    assert not (await cat.record_pick("online", "x", "lime")).applied
    r = await cat.record_pick("store_ave", "P1-0-1", "avocado")
    assert not r.applied and r.qty_after == 30


async def test_apply_scan_exact_count_with_lots_and_freshness(cat):
    dets = [
        Detection(sku="avocado", confidence=1.0, method="ground_truth", instance_id=f"i{i}", lot_id="avo-L2")
        for i in range(3)
    ] + [Detection(sku="avocado", confidence=1.0, method="ground_truth", instance_id="i9", lot_id="avo-L1")]
    r = await cat.apply_scan(
        scan(
            slot_id="P1-0-0",
            expected_sku="avocado",
            detections=dets,
            observed_count=4,
            count_is_exact=True,
            lot_freshness={"avo-L1": 0.4, "avo-L2": 0.95},
        )
    )
    assert r.applied and (r.qty_before, r.qty_after) == (10, 4) and not r.out_of_stock
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert {lt.lot_id: (lt.qty, lt.freshness) for lt in slot.lots} == {"avo-L1": (1, 0.4), "avo-L2": (3, 0.95)}


async def test_apply_scan_count_rules(cat):
    # not exact + some visible: never lowers qty to the facing count
    r = await cat.apply_scan(scan(slot_id="A1-1-0", expected_sku="tortillas_corn", observed_count=2))
    assert r.qty_after == 10
    # not exact + nothing visible: empty
    r = await cat.apply_scan(scan(slot_id="A1-1-0", expected_sku="tortillas_corn", observed_count=0))
    assert r.qty_after == 0 and r.out_of_stock
    # restock seen on a slot recorded as empty
    r = await cat.apply_scan(scan(slot_id="A1-1-1", expected_sku="black_beans", observed_count=3))
    assert (r.qty_before, r.qty_after) == (0, 3)
    hit = (await cat.search(SearchQuery(text="black beans", store_ids=["store_ave"], in_stock_only=True)))[0]
    assert hit.offers[0].qty == 3
    # exact count without lot info, lots trimmed FIFO
    r = await cat.apply_scan(scan(slot_id="D1-0-0", observed_count=2, count_is_exact=True))
    assert r.qty_after == 2
    slot = (await cat.get_planogram("store_ave")).slot("D1-0-0")
    assert sum(lt.qty for lt in slot.lots) == 2


async def test_apply_scan_mispricing_misplaced_and_location(cat):
    r = await cat.apply_scan(
        scan(
            slot_id="A1-1-3",
            expected_sku="salsa_hot",
            observed_count=4,
            shelf_tag_cents=399,
            detections=[Detection(sku="salsa_mild", confidence=0.9, method="barcode")],
        )
    )
    assert r.mispricing and r.misplaced_skus == ["salsa_mild"]
    dash = await cat.merchant_dashboard("store_ave")
    mp = {m.sku: m for m in dash.mispricings}
    assert mp["salsa_hot"].delta_cents == -50 and mp["salsa_mild"].shelf_tag_cents == 349
    # a correct tag clears it; the deal price also counts as correct
    assert not (await cat.apply_scan(scan(slot_id="A1-1-2", shelf_tag_cents=399))).mispricing
    assert not (await cat.apply_scan(scan(slot_id="A1-2-0", shelf_tag_cents=299))).mispricing
    dash = await cat.merchant_dashboard("store_ave")
    assert {m.sku for m in dash.mispricings} == {"salsa_hot"}
    # slot-less scan located by robot pose
    r = await cat.apply_scan(
        scan(expected_sku="lime", observed_count=0, count_is_exact=True, pose=Pose2D(x=9.1, y=16.2, theta=1.57))
    )
    assert r.applied and r.slot_id == "P1-0-1" and r.out_of_stock
    r = await cat.apply_scan(scan(observed_count=0, pose=Pose2D(x=1, y=20)))
    assert not r.applied
    assert not (await cat.apply_scan(scan(slot_id="nope"))).applied
    assert not (await cat.apply_scan(scan(store_id="online", slot_id="x"))).applied


async def test_merchant_dashboard_initial(cat):
    dash = await cat.merchant_dashboard("store_ave")
    assert dash.scans_total == 0
    assert [o.sku for o in dash.out_of_stock] == ["black_beans"]
    assert [(m.sku, m.system_price_cents, m.shelf_tag_cents) for m in dash.mispricings] == [("salsa_mild", 399, 349)]
    assert [n.sku for n in dash.near_expiry] == ["guac_kit", "avocado"]
    assert len(dash.heatmap) == 12 and all(c.value == 0 for c in dash.heatmap)
    assert dash.kpis["on_shelf_availability"] == pytest.approx(11 / 12, abs=1e-3)
    await cat.apply_scan(scan(slot_id="A1-1-0", observed_count=10, count_is_exact=True))
    dash = await cat.merchant_dashboard("store_ave")
    assert dash.scans_total == 1 and dash.kpis["scan_coverage"] == pytest.approx(1 / 12, abs=1e-3)
    with pytest.raises(KeyError):
        await cat.merchant_dashboard("online")


async def test_reset_restores_data(cat):
    await cat.record_pick("store_ave", "P1-0-1", "lime", qty=30)
    cat.reset()
    assert (await cat.get_offers("lime", ["store_ave"]))[0].qty == 30


def test_text_score_zero_for_unrelated():
    p = data_io.load_products(TEST_DATA)["lime"]
    assert text_score(p, "beef") == 0
    assert text_score(p, "") == 1.0


# ----------------------------------------------------------------------------------------------- real data/
REAL = data_io.REPO_ROOT / "data"


@pytest.mark.skipif(not (REAL / "products.yaml").exists(), reason="data/ not written yet (data agent)")
async def test_real_data_demo_store_smoke():
    cat = FakeCatalogService(Services(), data_root=REAL)
    stores = {s.store_id for s in await cat.list_stores()}
    assert "store_ave" in stores
    plan = await cat.get_planogram("store_ave")
    assert plan.slots
    for text in ["tortillas", "ground beef", "black beans", "cheese", "salsa", "avocado", "lime", "sour cream"]:
        assert await cat.search(SearchQuery(text=text)), f"demo search found nothing for {text!r}"
    dash = await cat.merchant_dashboard("store_ave")
    assert len(dash.heatmap) == len(plan.slots)
