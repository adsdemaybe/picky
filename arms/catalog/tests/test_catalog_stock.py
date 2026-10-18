"""Fake catalog v0.2: categories API + stock management (levels, alerts, restock, adjust, history, events, sim sync)."""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from arms.catalog.fake import FakeCatalogService
from arms.catalog.stock import actor_matches, next_alert
from contracts.catalog import RestockRequest, StockAdjustment
from contracts.perception import ScanReport
from contracts.planogram import Slot
from contracts.services import Services
from utils import data as data_io

TEST_DATA = Path(__file__).parent / "data"
TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


class RecordingEvents:
    def __init__(self, fail: bool = False) -> None:
        self.events: list = []
        self.fail = fail

    async def publish(self, event):
        if self.fail:
            raise RuntimeError("bus down")
        self.events.append(event)
        return event

    def of(self, type_: str) -> list:
        return [e for e in self.events if e.type == type_]


class RecordingRobot:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[str] | None]] = []
        self.fail = fail

    async def sync_stock(self, store_id, slot_ids=None):
        self.calls.append((store_id, slot_ids))
        if self.fail:
            raise RuntimeError("sim offline")


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t

    def tick(self, seconds: int = 60) -> None:
        self.t = self.t + timedelta(seconds=seconds)


@pytest.fixture
def bus() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def robot() -> RecordingRobot:
    return RecordingRobot()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cat(bus, robot, clock) -> FakeCatalogService:
    return FakeCatalogService(Services(events=bus, robot=robot), data_root=TEST_DATA, today=TODAY, now=clock)


def add_slot(cat: FakeCatalogService, store_id: str, **kw) -> Slot:
    """Inject an extra slot (tests for multi-slot SKUs) by copying an existing slot of the same SKU."""
    cat._ensure()
    plan = cat._plans[store_id]
    base = plan.slots_for(kw["sku"])[0]
    slot = base.model_copy(deep=True, update={"lots": [], "deal": None, "shelf_tag_cents": None, **kw})
    plan.slots.append(slot)
    return slot


# ----------------------------------------------------------------------------------------------- categories
async def test_list_categories_order_names_and_counts(cat):
    cats = await cat.list_categories("store_ave")
    ids = [c.category_id for c in cats]
    assert ids == ["produce", "dairy", "meat", "deli", "bakery", "pantry", "salsa", "condiments", "canned", "alcohol"]
    by = {c.category_id: c for c in cats}
    assert by["dairy"].name == "Dairy & Eggs" and by["dairy"].icon == "milk" and by["dairy"].color == "#2563eb"
    assert by["salsa"].parent_id == "pantry" and by["salsa"].name == "Salsa & Dips"
    assert by["canned"].parent_id == "pantry"  # derived from products (missing in categories.yaml)
    pantry = by["pantry"]
    assert (pantry.product_count, pantry.in_stock_count, pantry.out_of_stock_count) == (4, 3, 1)  # black beans OOS
    assert pantry.aisle_labels == ["Aisle 1"] and pantry.store_id == "store_ave"
    assert by["dairy"].product_count == 2  # cotija is online-only: not carried in store_ave
    assert by["salsa"].product_count == 2 and by["salsa"].aisle_labels == ["Aisle 1"]
    assert by["produce"].aisle_labels == ["Produce"]
    assert by["alcohol"].product_count == 0 and by["alcohol"].aisle_labels == []


async def test_list_categories_scopes(cat):
    every = {c.category_id: c for c in await cat.list_categories()}
    assert every["pantry"].store_id is None
    assert (every["pantry"].in_stock_count, every["pantry"].out_of_stock_count) == (4, 0)  # hill has black beans
    hill = {c.category_id: c for c in await cat.list_categories("store_hill")}
    assert (hill["pantry"].product_count, hill["pantry"].low_stock_count) == (2, 1)  # salsa_hot 3 <= threshold 3
    assert hill["produce"].product_count == 0
    online = {c.category_id: c for c in await cat.list_categories("online")}
    assert online["dairy"].product_count >= 1 and online["dairy"].aisle_labels == []
    with pytest.raises(KeyError):
        await cat.list_categories("nowhere")


async def test_categories_fallback_without_file(tmp_path):
    root = tmp_path / "data"
    shutil.copytree(TEST_DATA, root)
    (root / "categories.yaml").unlink()
    data_io.clear_cache()
    fake = FakeCatalogService(Services(), data_root=root, today=TODAY, now=lambda: NOW)
    cats = {c.category_id: c for c in await fake.list_categories()}
    assert cats["dairy"].name == "Dairy & Eggs" and cats["salsa"].parent_id == "pantry"
    assert "alcohol" not in cats  # only what products use


async def test_category_products(cat):
    hits = await cat.category_products("salsa", "store_ave")
    assert [h.product.sku for h in hits] == ["salsa_hot", "salsa_mild"]  # in stock, then name
    assert all({o.store_id for o in h.offers} == {"store_ave"} for h in hits)
    pantry = await cat.category_products("PANTRY")
    skus = [h.product.sku for h in pantry]
    assert set(skus) == {"black_beans", "salsa_mild", "salsa_hot", "hot_sauce"}
    beans = next(h for h in pantry if h.product.sku == "black_beans")
    assert {o.store_id for o in beans.offers} == {"store_ave", "store_hill", "online"}
    dairy_ave = {h.product.sku for h in await cat.category_products("dairy", "store_ave")}
    assert "cotija" not in dairy_ave
    assert "cotija" in {h.product.sku for h in await cat.category_products("dairy")}
    assert await cat.category_products("alcohol") == []
    with pytest.raises(KeyError):
        await cat.category_products("nope")


# ----------------------------------------------------------------------------------------------- levels + alerts
async def test_stock_levels_fields_order_and_filters(cat):
    levels = await cat.stock_levels("store_ave")
    assert len(levels) == 12
    assert levels[0].sku == "black_beans" and levels[0].status == "out"
    by = {lv.sku: lv for lv in levels}
    beef = by["ground_beef"]
    assert (beef.unit, beef.size, beef.category, beef.capacity, beef.low_threshold) == ("pack", "1 lb", "meat", 12, 3)
    assert beef.slot_ids == ["D1-0-0"] and beef.aisle_label == "Dairy & Meat" and beef.status == "ok"
    assert by["hot_sauce"].price_cents == 299  # deal applied
    assert by["sour_cream"].price_cents == 259  # slot override
    assert by["guac_kit"].near_expiry_qty == 3 and by["avocado"].near_expiry_qty == 6
    assert by["lime"].fill_ratio == 0.75 and by["lime"].low_threshold == 10
    assert by["salsa_mild"].subcategory == "salsa"

    assert [lv.sku for lv in await cat.stock_levels("store_ave", status="out")] == ["black_beans"]
    assert {lv.sku for lv in await cat.stock_levels("store_ave", category="salsa")} == {"salsa_mild", "salsa_hot"}
    assert len(await cat.stock_levels("store_ave", category="pantry")) == 4
    assert {lv.sku for lv in await cat.stock_levels("store_ave", q="SALSA")} == {"salsa_mild", "salsa_hot"}
    assert [lv.sku for lv in await cat.stock_levels("store_ave", q="la prueba")] == ["tortillas_corn"]  # brand
    assert [lv.sku for lv in await cat.stock_levels("store_ave", q="beef_")] == []
    for bad in ("online", "nowhere"):
        with pytest.raises(KeyError):
            await cat.stock_levels(bad)


async def test_initial_alerts_without_events(cat, bus):
    alerts = await cat.stock_alerts("store_ave")
    assert [(a.sku, a.status, a.alert_id) for a in alerts] == [("black_beans", "out", "store_ave:black_beans")]
    a = alerts[0]
    assert a.message == "Black Beans 15 oz is out of stock (Aisle 1)"
    assert a.suggested_restock_qty == 12 and a.slot_ids == ["A1-1-1"] and a.active and a.actor == "system"
    hill = await cat.stock_alerts("store_hill")
    assert [(x.sku, x.status) for x in hill] == [("salsa_hot", "low")]
    assert bus.events == []
    with pytest.raises(KeyError):
        await cat.stock_alerts("online")


# ----------------------------------------------------------------------------------------------- picks + scans
async def test_record_pick_movement_alerts_and_events(cat, bus, robot):
    r = await cat.record_pick("store_ave", "A1-1-3", "salsa_hot", trip_id="trip_1", robot_id="rover-ave-1")
    assert r.applied and (r.qty_before, r.qty_after) == (4, 3)
    [upd] = bus.of("stock.updated")
    assert upd.store_id == "store_ave" and upd.robot_id == "rover-ave-1" and upd.trip_id is None
    assert upd.source == "catalog" and upd.level.qty == 3 and upd.level.status == "low"
    mv = upd.movement
    assert (mv.reason, mv.actor, mv.ref_id, mv.delta, mv.qty_before, mv.qty_after) == (
        "pick",
        "robot:rover-ave-1",
        "trip_1",
        -1,
        4,
        3,
    )
    [alert_ev] = bus.of("stock.alert")
    assert alert_ev.alert.status == "low" and alert_ev.alert.active and alert_ev.alert.trigger == "pick"
    assert alert_ev.alert.actor == "robot:rover-ave-1" and alert_ev.robot_id == "rover-ave-1"

    await cat.record_pick("store_ave", "A1-1-3", "salsa_hot", robot_id="rover-ave-1")  # 2: still low -> no alert
    assert len(bus.of("stock.alert")) == 1
    [low] = [a for a in await cat.stock_alerts("store_ave") if a.sku == "salsa_hot"]
    assert low.qty == 2 and low.status == "low"
    await cat.record_pick("store_ave", "A1-1-3", "salsa_hot", qty=2, robot_id="rover-ave-1")
    assert bus.of("stock.alert")[-1].alert.status == "out"
    alerts = await cat.stock_alerts("store_ave")
    assert [a.sku for a in alerts] == ["black_beans", "salsa_hot"]  # both out, by name
    assert robot.calls == []  # picks come from the sim: never synced back

    hist = await cat.stock_history("store_ave")
    assert [m.delta for m in hist] == [-2, -1, -1]  # newest first
    assert len({m.movement_id for m in hist}) == 3


async def test_record_pick_lot_id_and_stale(cat, bus):
    await cat.record_pick("store_ave", "P1-0-0", "avocado", robot_id="rover-ave-1", lot_id="avo-L2")
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert {lt.lot_id: lt.qty for lt in slot.lots} == {"avo-L1": 6, "avo-L2": 3}
    assert bus.of("stock.updated")[-1].movement.lot_id == "avo-L2"
    # picking from a slot recorded as empty changes nothing -> no movement, no event
    n = len(bus.events)
    r = await cat.record_pick("store_ave", "A1-1-1", "black_beans", robot_id="rover-ave-1")
    assert r.applied and r.qty_after == 0 and "stale" in r.notes
    assert len(bus.events) == n and all(m.sku != "black_beans" for m in await cat.stock_history("store_ave"))
    # legacy positional call (no robot) still works; actor 'system'
    await cat.record_pick("store_ave", "P1-0-1", "lime", 2, "trip_9")
    assert (await cat.stock_history("store_ave", sku="lime"))[0].actor == "system"


async def test_apply_scan_movements_and_lot_counts(cat, bus):
    r = await cat.apply_scan(
        ScanReport(
            scan_id="scan_1",
            store_id="store_ave",
            slot_id="P1-0-0",
            expected_sku="avocado",
            observed_count=5,
            count_is_exact=True,
            lot_counts={"avo-L1": 2, "avo-L2": 3},
            robot_id="rover-ave-2",
        )
    )
    assert (r.qty_before, r.qty_after) == (10, 5)
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert {lt.lot_id: lt.qty for lt in slot.lots} == {"avo-L1": 2, "avo-L2": 3}
    mv = bus.of("stock.updated")[-1].movement
    assert (mv.reason, mv.actor, mv.ref_id, mv.delta) == ("scan", "robot:rover-ave-2", "scan_1", -5)
    # lot_counts that disagree with the count are reconciled so sum(lots) == qty
    await cat.apply_scan(
        ScanReport(
            scan_id="scan_2",
            store_id="store_ave",
            slot_id="P1-0-0",
            observed_count=4,
            count_is_exact=True,
            lot_counts={"avo-L1": 1, "avo-L2": 1},
        )
    )
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert slot.qty == 4 and sum(lt.qty for lt in slot.lots) == 4
    # a scan that changes nothing records no movement
    n = len(bus.events)
    await cat.apply_scan(ScanReport(scan_id="scan_3", store_id="store_ave", slot_id="A1-1-0", observed_count=2))
    assert len(bus.events) == n
    # units seen on the OOS slot: out -> low (re-raised with the new status)
    await cat.apply_scan(
        ScanReport(scan_id="scan_4", store_id="store_ave", slot_id="A1-1-1", observed_count=3, robot_id="rover-ave-1")
    )
    ev = bus.of("stock.alert")[-1]
    assert ev.alert.sku == "black_beans" and ev.alert.status == "low" and ev.alert.trigger == "scan"
    assert (await cat.stock_history("store_ave", reason="scan"))[0].ref_id == "scan_4"
    # a scan-driven OOS raises an out alert
    await cat.apply_scan(
        ScanReport(scan_id="scan_5", store_id="store_ave", slot_id="A1-1-0", observed_count=0, robot_id="rover-ave-1")
    )
    assert bus.of("stock.alert")[-1].alert.sku == "tortillas_corn"
    assert bus.of("stock.alert")[-1].alert.status == "out"


# ----------------------------------------------------------------------------------------------- restock
async def test_restock_fill_clears_alert_and_syncs(cat, bus, robot):
    res = await cat.restock("store_ave", RestockRequest(sku="black_beans", actor="staff:ana", note="truck"))
    assert res.level.qty == 12 and res.level.status == "ok"
    [mv] = res.movements
    assert (mv.delta, mv.reason, mv.actor, mv.slot_id, mv.note, mv.lot_id) == (
        12,
        "restock",
        "staff:ana",
        "A1-1-1",
        "truck",
        None,  # lot-less non-perishable stays lot-less
    )
    assert res.alert is not None and not res.alert.active and res.alert.cleared_at == NOW
    assert "back in stock" in res.alert.message
    assert await cat.stock_alerts("store_ave") == []
    assert bus.of("stock.alert")[-1].alert.active is False
    assert bus.of("stock.updated")[-1].level.qty == 12
    assert robot.calls == [("store_ave", ["A1-1-1"])]
    offer = (await cat.get_offers("black_beans", ["store_ave"]))[0]
    assert offer.in_stock and offer.qty == 12
    # already full: nothing to do, no events, no sync
    n = len(bus.events)
    again = await cat.restock("store_ave", RestockRequest(sku="black_beans"))
    assert again.movements == [] and len(bus.events) == n and len(robot.calls) == 1


async def test_restock_qty_lots_and_rejections(cat, bus, robot):
    res = await cat.restock("store_ave", RestockRequest(sku="guac_kit", qty=2))  # perishable: new lot w/ expiry
    [mv] = res.movements
    assert mv.lot_id == "D1-1-0-R1"
    slot = (await cat.get_planogram("store_ave")).slot("D1-1-0")
    lot = next(lt for lt in slot.lots if lt.lot_id == "D1-1-0-R1")
    assert lot.qty == 2 and lot.expiry == TODAY + timedelta(days=7)
    assert slot.qty == 9 and sum(lt.qty for lt in slot.lots) == 9

    res = await cat.restock(
        "store_ave", RestockRequest(sku="avocado", qty=1, lot_id="avo-L9", expiry=date(2026, 10, 25))
    )
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert next(lt for lt in slot.lots if lt.lot_id == "avo-L9").expiry == date(2026, 10, 25)

    n_events, n_moves = len(bus.events), len(await cat.stock_history("store_ave"))
    with pytest.raises(ValueError, match="exceeds free shelf capacity"):
        await cat.restock("store_ave", RestockRequest(sku="tortillas_corn", qty=3))  # 10/12 -> only 2 free
    assert (await cat.get_planogram("store_ave")).slot("A1-1-0").qty == 10
    assert len(bus.events) == n_events and len(await cat.stock_history("store_ave")) == n_moves
    for store, req in [
        ("store_ave", RestockRequest(sku="cotija")),  # online-only: not stocked here
        ("nowhere", RestockRequest(sku="lime")),
        ("online", RestockRequest(sku="lime")),
        ("store_ave", RestockRequest(sku="lime", slot_id="A1-1-0")),  # slot holds another sku
    ]:
        with pytest.raises(KeyError):
            await cat.restock(store, req)


async def test_restock_spreads_over_slots_in_order(cat, robot):
    add_slot(cat, "store_ave", slot_id="A1-0-9", sku="salsa_mild", qty=2, capacity=6)
    res = await cat.restock("store_ave", RestockRequest(sku="salsa_mild", qty=6))  # 8/12 + 2/6 -> 4 then 2
    assert [(m.slot_id, m.delta) for m in res.movements] == [("A1-1-2", 4), ("A1-0-9", 2)]
    assert [(m.qty_before, m.qty_after) for m in res.movements] == [(10, 14), (14, 16)]
    assert res.level.qty == 16 and res.level.capacity == 18
    assert robot.calls[-1] == ("store_ave", ["A1-1-2", "A1-0-9"])


# ----------------------------------------------------------------------------------------------- adjust
async def test_adjust_delta_damage_and_rules(cat, bus, robot):
    res = await cat.adjust_stock(
        "store_ave", StockAdjustment(sku="sour_cream", delta=-2, reason="damage", actor="staff:ana", note="dropped")
    )
    [mv] = res.movements
    assert (mv.delta, mv.qty_before, mv.qty_after, mv.reason, mv.actor) == (-2, 7, 5, "damage", "staff:ana")
    assert res.level.qty == 5 and robot.calls == [("store_ave", ["D1-0-2"])]
    res = await cat.adjust_stock("store_ave", StockAdjustment(sku="sour_cream", delta=3, actor="staff:bo"))
    assert res.level.qty == 8 and res.movements[0].reason == "manual"
    before = len(bus.events)
    with pytest.raises(ValueError):
        await cat.adjust_stock("store_ave", StockAdjustment(sku="sour_cream", delta=-9))  # below 0
    with pytest.raises(ValueError):
        await cat.adjust_stock("store_ave", StockAdjustment(sku="sour_cream", delta=5))  # 8 + 5 > 12
    with pytest.raises(ValueError):
        await cat.adjust_stock("store_ave", StockAdjustment(sku="sour_cream", set_qty=13))
    assert len(bus.events) == before and (await cat.stock_levels("store_ave", q="sour"))[0].qty == 8
    with pytest.raises(KeyError):
        await cat.adjust_stock("store_ave", StockAdjustment(sku="cotija", delta=1))


async def test_adjust_set_qty_lots_alerts_and_noop(cat, bus):
    res = await cat.adjust_stock("store_ave", StockAdjustment(sku="avocado", set_qty=2, actor="staff:ana"))
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert slot.qty == 2 and {lt.lot_id: lt.qty for lt in slot.lots} == {"avo-L1": 0, "avo-L2": 2}  # FIFO
    assert res.alert is not None and res.alert.status == "low" and res.alert.trigger == "manual"
    n = len(bus.events)
    noop = await cat.adjust_stock("store_ave", StockAdjustment(sku="avocado", set_qty=2))
    assert noop.movements == [] and len(bus.events) == n and noop.alert is not None and noop.alert.active
    # a positive delta on a lot-tracked slot joins the latest lot unless lot_id is given
    await cat.adjust_stock("store_ave", StockAdjustment(sku="avocado", delta=1))
    await cat.adjust_stock("store_ave", StockAdjustment(sku="avocado", delta=1, lot_id="avo-L1"))
    slot = (await cat.get_planogram("store_ave")).slot("P1-0-0")
    assert {lt.lot_id: lt.qty for lt in slot.lots} == {"avo-L1": 1, "avo-L2": 3}


async def test_adjust_multi_slot_rules(cat, robot):
    add_slot(cat, "store_ave", slot_id="A1-0-9", sku="salsa_mild", qty=5)
    with pytest.raises(ValueError, match="slot_id"):
        await cat.adjust_stock("store_ave", StockAdjustment(sku="salsa_mild", set_qty=3))
    res = await cat.adjust_stock("store_ave", StockAdjustment(sku="salsa_mild", set_qty=3, slot_id="A1-0-9"))
    assert res.level.qty == 11
    res = await cat.adjust_stock("store_ave", StockAdjustment(sku="salsa_mild", delta=-10, reason="damage"))
    assert [(m.slot_id, m.delta) for m in res.movements] == [("A1-1-2", -8), ("A1-0-9", -2)]  # spills in order
    assert res.level.qty == 1 and robot.calls[-1] == ("store_ave", ["A1-1-2", "A1-0-9"])
    with pytest.raises(ValueError):  # slot_id pins the slot: no spill
        await cat.adjust_stock("store_ave", StockAdjustment(sku="salsa_mild", delta=-2, slot_id="A1-1-2"))


# ----------------------------------------------------------------------------------------------- history
async def test_stock_history_filters_and_paging(cat, clock):
    await cat.record_pick("store_ave", "P1-0-1", "lime", robot_id="rover-ave-1")
    clock.tick()
    await cat.adjust_stock("store_ave", StockAdjustment(sku="lime", delta=-1, reason="damage", actor="staff:ana"))
    clock.tick()
    await cat.restock("store_ave", RestockRequest(sku="lime", qty=5, actor="staff:bo"))
    clock.tick()
    await cat.record_pick("store_ave", "P1-0-2", "cilantro", robot_id="rover-ave-2")

    hist = await cat.stock_history("store_ave")
    assert [m.reason for m in hist] == ["pick", "restock", "damage", "pick"]
    assert [m.sku for m in await cat.stock_history("store_ave", sku="lime")] == ["lime"] * 3
    assert len(await cat.stock_history("store_ave", limit=2)) == 2
    page2 = await cat.stock_history("store_ave", before=hist[1].ts)
    assert [m.reason for m in page2] == ["damage", "pick"]
    assert [m.actor for m in await cat.stock_history("store_ave", actor="robot:")] == [
        "robot:rover-ave-2",
        "robot:rover-ave-1",
    ]
    assert [m.reason for m in await cat.stock_history("store_ave", actor="staff:ana")] == ["damage"]
    assert [m.reason for m in await cat.stock_history("store_ave", reason="restock")] == ["restock"]
    assert await cat.stock_history("store_hill") == []
    levels = {lv.sku: lv for lv in await cat.stock_levels("store_ave")}
    assert levels["lime"].last_movement_at == NOW + timedelta(seconds=120)
    with pytest.raises(KeyError):
        await cat.stock_history("nowhere")


async def test_reset_clears_stock_state(cat):
    await cat.restock("store_ave", RestockRequest(sku="black_beans"))
    cat.reset()
    assert await cat.stock_history("store_ave") == []
    assert [a.sku for a in await cat.stock_alerts("store_ave")] == ["black_beans"]


# ----------------------------------------------------------------------------------------------- robustness
async def test_bus_and_sim_failures_never_raise():
    fake = FakeCatalogService(
        Services(events=RecordingEvents(fail=True), robot=RecordingRobot(fail=True)),
        data_root=TEST_DATA,
        today=TODAY,
        now=lambda: NOW,
    )
    res = await fake.restock("store_ave", RestockRequest(sku="black_beans"))
    assert res.level.qty == 12
    r = await fake.record_pick("store_ave", "A1-1-1", "black_beans", robot_id="rover-ave-1")
    assert r.qty_after == 11


async def test_works_without_services():
    fake = FakeCatalogService(None, data_root=TEST_DATA, today=TODAY, now=lambda: NOW)
    assert (await fake.restock("store_ave", RestockRequest(sku="black_beans"))).level.qty == 12


def test_actor_matches_and_alert_lifecycle_helpers():
    assert actor_matches("robot:rover-ave-1", "robot:")
    assert actor_matches("staff:ana", "staff:ana") and not actor_matches("staff:ana", "staff")
    from contracts.catalog import StockLevel

    lv = StockLevel(store_id="s", sku="x", name="X", category="pantry", qty=0, capacity=10, low_threshold=3)
    alert, changed = next_alert(None, lv, trigger="pick", actor="system", now=NOW)
    assert changed and alert.status == "out"
    ok = lv.model_copy(update={"qty": 8})
    cleared, changed = next_alert(alert, ok, trigger="restock", actor="staff", now=NOW)
    assert changed and not cleared.active and cleared.cleared_at == NOW
    assert next_alert(cleared, ok, trigger="restock", actor="staff", now=NOW) == (None, False)


# ----------------------------------------------------------------------------------------------- real data/
REAL = data_io.REPO_ROOT / "data"


@pytest.mark.skipif(not (REAL / "products.yaml").exists(), reason="data/ not written yet (data agent)")
async def test_real_data_stock_and_categories_smoke():
    fake = FakeCatalogService(Services(), data_root=REAL)
    cats = await fake.list_categories("store_ave")
    assert cats and cats[0].parent_id is None
    assert sum(c.product_count for c in cats if c.parent_id is None) > 0
    levels = await fake.stock_levels("store_ave")
    assert levels and all(lv.capacity >= 0 for lv in levels)
    await fake.stock_alerts("store_ave")
