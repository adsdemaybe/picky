"""Perception fake conformance + behavior (base deps only), plus a smoke test of the real skeleton's fallbacks."""

from pathlib import Path

import pytest

from arms.perception import create, create_fake
from arms.perception.fake import FakePerception, freshness_reason, is_synthetic_frame
from contracts.catalog import Lot, Product
from contracts.common import BBox, Point3, Pose2D, Rect2D
from contracts.perception import PICK_CHOICE_MAX
from contracts.planogram import Fixture, Planogram, Slot
from contracts.services import PerceptionService, Services
from contracts.sim import CameraFrame, ItemInstance

STORE = "store_test"
JPEG = "/9j/AAAA"  # not a real JPEG: the fake never decodes, the real service must degrade


def _product(sku: str, price: int, barcode: str, category: str = "produce") -> Product:
    return Product(sku=sku, name=sku.title(), category=category, price_cents=price, barcode=barcode)


PRODUCTS = {
    "avocado": _product("avocado", 129, "4000000000017"),
    "salsa_mild": _product("salsa_mild", 399, "4000000000024", "pantry"),
    "lime": _product("lime", 39, "4000000000031"),
}


def _slot(slot_id: str, sku: str, qty: int, **kw) -> Slot:
    return Slot(
        slot_id=slot_id,
        fixture_id="F1",
        sku=sku,
        qty=qty,
        item_position=Point3(x=2.0, y=1.0, z=0.5),
        approach_pose=Pose2D(x=2.0, y=2.0, theta=-1.5708),
        **kw,
    )


PLAN = Planogram(
    store_id=STORE,
    name="Test",
    bounds=Rect2D(x=0, y=0, w=10, h=10),
    entrance=Pose2D(x=1, y=1),
    checkout=Pose2D(x=9, y=1),
    fixtures=[Fixture(fixture_id="F1", kind="produce_bin", rect=Rect2D(x=1, y=0.2, w=3, h=0.6), yaw=1.5708)],
    slots=[
        _slot(
            "S_AVO",
            "avocado",
            5,
            lots=[Lot(lot_id="L_new", qty=3, freshness=0.9), Lot(lot_id="L_old", qty=2, freshness=0.3)],
        ),
        _slot("S_SALSA", "salsa_mild", 4, price_cents=349, shelf_tag_cents=399),
        _slot("S_LIME", "lime", 8),
    ],
)


class StubCatalog:
    """Just the two catalog calls perception makes."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_planogram(self, store_id: str) -> Planogram:
        self.calls += 1
        if store_id != STORE:
            raise KeyError(store_id)
        return PLAN.model_copy(deep=True)

    async def get_product(self, sku: str) -> Product | None:
        return PRODUCTS.get(sku)


def _inst(iid: str, sku: str, slot: str | None, fresh: float | None = None, lot: str | None = None, **kw):
    return ItemInstance(
        instance_id=iid,
        sku=sku,
        slot_id=slot,
        position=Point3(x=2.0, y=1.0, z=0.55),
        freshness=fresh,
        lot_id=lot,
        bbox=BBox(x=10, y=10, w=40, h=40),
        **kw,
    )


AVOCADOS = [
    _inst("avo_1", "avocado", "S_AVO", 0.92, "L_new"),
    _inst("avo_2", "avocado", "S_AVO", 0.88, "L_new"),
    _inst("avo_3", "avocado", "S_AVO", 0.95, "L_new"),
    _inst("avo_4", "avocado", "S_AVO", 0.35, "L_old"),
    _inst("avo_5", "avocado", "S_AVO", 0.25, "L_old"),
]


def _frame(instances, slot_id=None, frame_id="frame_t1", camera="head") -> CameraFrame:
    return CameraFrame(
        frame_id=frame_id,
        camera=camera,
        jpeg_b64=JPEG,
        w=640,
        h=480,
        slot_id=slot_id,
        pose=Pose2D(x=2.0, y=2.0, theta=-1.5708),
        instances=list(instances),
    )


@pytest.fixture
def services() -> Services:
    return Services(catalog=StubCatalog())


# ------------------------------------------------------------------------------------------------------------------
# Conformance
# ------------------------------------------------------------------------------------------------------------------
def test_factories_satisfy_protocol(services):
    catalog = services.catalog
    fake = create_fake(services)
    real = create(services)
    assert isinstance(fake, PerceptionService)
    assert isinstance(real, PerceptionService)
    assert catalog.calls == 0  # factories never call other services


# ------------------------------------------------------------------------------------------------------------------
# blur_faces
# ------------------------------------------------------------------------------------------------------------------
async def test_blur_only_certifies_synthetic_frames(services):
    p = create_fake(services)
    gt = _frame(AVOCADOS, "S_AVO")
    out = await p.blur_faces(gt)
    assert out.blurred and not gt.blurred  # copy, input untouched
    assert (await p.blur_faces(_frame([], frame_id="sim_000042"))).blurred
    real_cam = _frame([], frame_id="webcam_7")
    assert not is_synthetic_frame(real_cam)
    assert not (await p.blur_faces(real_cam)).blurred


# ------------------------------------------------------------------------------------------------------------------
# identify
# ------------------------------------------------------------------------------------------------------------------
async def test_identify_ground_truth_slot(services):
    p = create_fake(services)
    rep = await p.identify(_frame(AVOCADOS, "S_AVO"), STORE, slot_id="S_AVO")
    assert rep.expected_sku == "avocado"
    assert rep.observed_count == 5 and rep.count_is_exact and not rep.mismatch
    assert {d.method for d in rep.detections} == {"ground_truth"}
    assert rep.detections[0].text == PRODUCTS["avocado"].barcode
    assert rep.lot_freshness == {"L_new": pytest.approx(0.9167, abs=1e-3), "L_old": pytest.approx(0.30)}
    assert rep.lot_counts == {"L_new": 3, "L_old": 2}  # v0.2: units per lot when the count is exact
    assert rep.freshness["avo_3"] == 0.95
    assert rep.shelf_tag_cents == 129 and rep.pose is not None and rep.frame_id == "frame_t1"


async def test_identify_mispricing_misplaced_and_empty(services):
    p = create_fake(services)
    frame = _frame([_inst("s1", "salsa_mild", "S_SALSA"), _inst("x1", "lime", "S_SALSA")], "S_SALSA")
    rep = await p.identify(frame, STORE)  # slot comes from the frame hint
    assert rep.slot_id == "S_SALSA" and rep.observed_count == 1
    assert rep.mismatch and "lime" in rep.notes
    assert rep.shelf_tag_cents == 399  # the tag, not the 349 store price

    empty = await p.identify(_frame([], "S_LIME", frame_id="fake_1"), STORE, slot_id="S_LIME")
    assert empty.observed_count == 0 and empty.count_is_exact and empty.mismatch


async def test_identify_without_ground_truth_does_not_zero_stock(services):
    p = create_fake(services)
    rep = await p.identify(_frame([], slot_id=None, frame_id="webcam_1"), STORE, slot_id="S_LIME")
    assert rep.observed_count == 8 and not rep.count_is_exact and not rep.mismatch


async def test_identify_unknown_store_degrades(services):
    p = create_fake(services)
    rep = await p.identify(_frame(AVOCADOS), "online", expected_sku="avocado")
    assert rep.observed_count == 5 and rep.shelf_tag_cents is None


async def test_ids_are_deterministic(services):
    a, b = FakePerception(services), FakePerception(services)
    f = _frame(AVOCADOS, "S_AVO")
    assert (await a.identify(f, STORE)).scan_id == (await b.identify(f, STORE)).scan_id
    assert (await a.rank_freshness(f, "S_AVO", "avocado")).choice_id == (
        await b.rank_freshness(f, "S_AVO", "avocado")
    ).choice_id


# ------------------------------------------------------------------------------------------------------------------
# rank_freshness (demo: produce top-3)
# ------------------------------------------------------------------------------------------------------------------
async def test_rank_freshness_top3(services):
    p = create_fake(services)
    noise = [_inst("lime_1", "lime", "S_AVO", 0.99), _inst("avo_b", "avocado", None, 1.0, in_basket=True)]
    choice = await p.rank_freshness(_frame(AVOCADOS + noise, "S_AVO"), "S_AVO", "avocado", k=10)
    assert len(choice.candidates) == PICK_CHOICE_MAX
    assert [c.instance_id for c in choice.candidates] == ["avo_3", "avo_1", "avo_2"]
    assert [c.rank for c in choice.candidates] == [1, 2, 3]
    assert choice.recommended.instance_id == "avo_3"
    assert all(c.reason and c.bbox is not None for c in choice.candidates)
    assert "Peak fresh" in choice.candidates[0].reason


async def test_rank_freshness_small_and_empty(services):
    p = create_fake(services)
    two = await p.rank_freshness(_frame(AVOCADOS[3:], "S_AVO"), "S_AVO", "avocado")
    assert [c.instance_id for c in two.candidates] == ["avo_4", "avo_5"]
    none = await p.rank_freshness(_frame([], "S_AVO"), "S_AVO", "avocado")
    assert none.candidates == [] and none.recommended is None


def test_freshness_reason_buckets():
    assert "Peak" in freshness_reason(0.9, 1, 3)
    assert "prime" in freshness_reason(0.1, 3, 3)
    assert "No freshness" in freshness_reason(None, 1, 1)


# ------------------------------------------------------------------------------------------------------------------
# verify_basket
# ------------------------------------------------------------------------------------------------------------------
async def test_verify_basket_multiset_diff(services):
    p = create_fake(services)
    basket = [
        _inst("b1", "avocado", None, in_basket=True),
        _inst("b2", "avocado", None, in_basket=True),
        _inst("b3", "salsa_mild", None, in_basket=True),
        _inst("shelf", "lime", "S_LIME"),  # on the shelf, not in the basket
    ]
    ok = await p.verify_basket(_frame(basket, camera="basket"), {"avocado": 2, "salsa_mild": 1})
    assert ok.ok and ok.method == "ground_truth"
    bad = await p.verify_basket(_frame(basket, camera="basket"), {"avocado": 3, "lime": 1})
    assert not bad.ok
    assert bad.missing == {"avocado": 1, "lime": 1} and bad.unexpected == {"salsa_mild": 1}
    # basket camera with no in_basket flags -> everything visible counts
    flat = await p.verify_basket(_frame([_inst("c1", "lime", None)], camera="basket"), {"lime": 1})
    assert flat.ok


# ------------------------------------------------------------------------------------------------------------------
# real skeleton degrades to ground truth with base deps / garbage JPEG
# ------------------------------------------------------------------------------------------------------------------
async def test_real_service_falls_back(services):
    real = create(services)
    f = _frame(AVOCADOS, "S_AVO")
    rep = await real.identify(f, STORE)
    assert rep.observed_count == 5 and rep.count_is_exact
    assert (await real.blur_faces(f)).blurred  # synthetic frame; JPEG undecodable -> synthetic rule
    choice = await real.rank_freshness(f, "S_AVO", "avocado")
    assert choice.recommended.instance_id == "avo_3"
    assert (await real.verify_basket(f, {})).ok  # head cam, nothing flagged in_basket -> empty basket
    assert not (await real.blur_faces(_frame([], frame_id="webcam_2"))).blurred


# ------------------------------------------------------------------------------------------------------------------
# demo data (written concurrently by the data agent; skipped until it exists)
# ------------------------------------------------------------------------------------------------------------------
DATA_PLAN = Path(__file__).resolve().parents[3] / "data" / "planograms" / "store_ave.yaml"


@pytest.mark.skipif(not DATA_PLAN.exists(), reason="data/planograms/store_ave.yaml not written yet")
async def test_demo_store_produce_scan_and_rank():
    from utils.data import load_planogram

    plan = load_planogram("store_ave")
    produce = [s for s in plan.slots if any(lot.freshness is not None for lot in s.lots)]
    assert produce, "store_ave has no produce slot with lot freshness"
    slot = produce[0]
    instances = []
    for lot in slot.lots:
        for n in range(lot.qty):
            iid = f"{slot.slot_id}_{lot.lot_id}_{n}"
            instances.append(_inst(iid, slot.sku, slot.slot_id, lot.freshness, lot.lot_id))
    p = FakePerception(Services())  # catalog unwired -> utils.data fallback
    frame = _frame(instances, slot.slot_id, frame_id="sim_demo")
    rep = await p.identify(frame, "store_ave")
    assert rep.expected_sku == slot.sku and rep.observed_count == len(instances) and rep.count_is_exact
    choice = await p.rank_freshness(frame, slot.slot_id, slot.sku)
    assert 1 <= len(choice.candidates) <= PICK_CHOICE_MAX
    scores = [c.score for c in choice.candidates]
    assert scores == sorted(scores, reverse=True)
