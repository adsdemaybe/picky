"""Fake conformance + demo-scenario behavior (base deps only, in-memory taco world; no data/ needed)."""

import pytest

from arms.recommender import create, create_fake
from arms.recommender.fake import FakeRecommenderService
from arms.recommender.tests import world as w
from contracts.profile import Feedback, PurchaseRecord
from contracts.services import RecommenderService, Services
from contracts.shopping import Cart, CartLine


def make(services: Services | None = None) -> FakeRecommenderService:
    return FakeRecommenderService(
        services, products=w.BY_SKU, planograms={w.STORE: w.planogram()}, users=w.users(), today=w.TODAY
    )


def test_factories_satisfy_protocol() -> None:
    assert isinstance(create_fake(Services()), RecommenderService)
    assert isinstance(create(Services()), RecommenderService)
    assert isinstance(make(), RecommenderService)


async def test_recommend_demo_trip() -> None:
    svc = make()
    sl = w.taco_list()
    recs = await svc.recommend("maya", w.STORE, sl, limit=3)
    assert len(recs) == 3
    list_skus = {i.sku for i in sl.items}
    for r in recs:
        assert r.reason and r.signals and r.sponsored is False
        assert r.signals["sponsored"] is False
        assert r.trip_id == w.TRIP and r.user_id == "maya" and r.store_id == w.STORE
        assert r.sku not in list_skus
        assert r.kind in {"detour", "near_expiry_offer"}
        assert 0.0 <= r.interest <= 1.0
        assert r.slot_id is not None and r.price_cents is not None
    assert [r.interest for r in recs] == sorted((r.interest for r in recs), reverse=True)
    guac = next(r for r in recs if r.sku == "guac_kit")
    assert guac.kind == "near_expiry_offer"
    assert "40% off, expires tomorrow" in guac.reason
    assert guac.price_cents == 389 and guac.deal is not None
    assert any(r.kind == "detour" and r.deal is not None for r in recs)  # mix: taste pick + markdown + deal
    top5 = await svc.recommend("maya", w.STORE, sl, limit=5)
    cholula = next(r for r in top5 if r.sku == "hot_sauce_cholula")
    assert "5★" in cholula.reason
    # deterministic: same inputs -> identical recs (incl. rec ids)
    assert [r.model_dump() for r in await svc.recommend("maya", w.STORE, sl, limit=3)] == [r.model_dump() for r in recs]


async def test_hard_filters() -> None:
    recs = await make().recommend("maya", w.STORE, w.taco_list(), limit=20, exclude_skus=["guac_kit"])
    skus = {r.sku for r in recs}
    assert "guac_kit" not in skus  # declined
    assert "mushroom_pack" not in skus  # disliked tag
    assert "chorizo" not in skus and "beef_ground" not in skus  # vegetarian
    assert "lime_bag" not in skus  # out of stock (and on the list)


async def test_focus_and_online_store() -> None:
    svc = make()
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=1, focus="hot sauce")
    assert recs[0].sku.startswith("hot_sauce")
    assert await svc.recommend("maya", "online", w.taco_list()) == []


async def test_feedback_makes_trip_two_spicier() -> None:
    svc = make()
    before = await svc.recommend("maya", w.STORE, w.taco_list("trip_1"), limit=5)
    update = await svc.record_feedback(
        Feedback(feedback_id="fb1", user_id="maya", sku="salsa_mild", rating=3, text="too mild", trip_id="trip_1")
    )
    assert update.version_after == update.version_before + 1
    assert any(c.key == "spicy" and c.new > c.old for c in update.changes)
    assert "hotter" in update.summary
    profile = await svc.get_profile("maya")
    assert profile.prefs["spicy"] > 0.2 and profile.version == update.version_after

    after = await svc.recommend("maya", w.STORE, w.taco_list("trip_2"), limit=5)
    assert [r.sku for r in after] != [r.sku for r in before]
    rank_of = lambda recs: [r.sku for r in recs].index("hot_sauce_ghost")  # noqa: E731
    assert rank_of(after) < rank_of(before)  # the spiciest pick moves up
    ghost_before = next(r for r in before if r.sku == "hot_sauce_ghost").interest
    ghost_after = next(r for r in after if r.sku == "hot_sauce_ghost").interest
    assert ghost_after > ghost_before
    assert all(r.trip_id == "trip_2" for r in after)


async def test_low_rating_dislikes_sku() -> None:
    svc = make()
    up = await svc.record_feedback(Feedback(feedback_id="fb2", user_id="maya", sku="hot_sauce_cholula", rating=1))
    assert any(c.field == "disliked_skus" for c in up.changes)
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=10)
    assert "hot_sauce_cholula" not in {r.sku for r in recs}


async def test_near_expiry_only_for_likely_users() -> None:
    svc = make()
    maya = await svc.near_expiry_offers("maya", w.STORE, trip_id=w.TRIP)
    assert [r.sku for r in maya] == ["guac_kit"]
    assert maya[0].kind == "near_expiry_offer" and maya[0].trip_id == w.TRIP
    assert await svc.near_expiry_offers("sam", w.STORE) == []


async def test_checkout_batch() -> None:
    cart = Cart(
        cart_id="c1",
        trip_id=w.TRIP,
        user_id="maya",
        lines=[
            CartLine(line_id="l1", sku="tortilla_corn", unit_price_cents=349, store_id=w.STORE, name="Corn Tortillas"),
            CartLine(line_id="l2", sku="guac_kit", unit_price_cents=389, store_id=w.STORE, name="Guacamole Kit"),
        ],
    )
    recs = await make().checkout_batch("maya", w.STORE, cart, limit=10)
    assert 0 < len(recs) <= 3
    assert all(r.kind == "checkout_batch" and r.trip_id == w.TRIP for r in recs)
    assert not {r.sku for r in recs} & {"tortilla_corn", "guac_kit"}


async def test_profiles_and_handles() -> None:
    svc = make()
    assert (await svc.find_user_by_handle("(555) 555-0100")).user_id == "maya"
    assert (await svc.find_user_by_handle("maya@EXAMPLE.com ")).user_id == "maya"
    assert await svc.find_user_by_handle("+19999999999") is None
    with pytest.raises(KeyError):
        await svc.get_profile("nobody")
    prof = await svc.get_profile("maya")
    prof.prefs["organic"] = 0.5
    assert "organic" not in (await svc.get_profile("maya")).prefs  # get_profile returns a copy
    saved = await svc.save_profile(prof)
    assert saved.version == prof.version + 1 and saved.prefs["organic"] == 0.5


async def test_record_purchase_feeds_history() -> None:
    svc = make()
    await svc.record_purchase(
        [
            PurchaseRecord(
                purchase_id=f"px{i}", user_id="maya", sku="chips_tortilla", unit_price_cents=299, store_id=w.STORE
            )
            for i in range(2)
        ]
    )
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=10)
    chips = next(r for r in recs if r.sku == "chips_tortilla")
    assert chips.signals["times_bought"] == 2


class _Catalog:
    """Minimal catalog stand-in: proves the fake reads store state from services.catalog at call time."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.plan = w.planogram()
        for slot in self.plan.slots:
            if slot.sku == "hot_sauce_cholula":
                slot.qty = 0  # picked out since the files were written

    async def get_planogram(self, store_id):
        self.calls.append("get_planogram")
        if store_id != w.STORE:
            raise KeyError(store_id)
        return self.plan

    async def get_deals(self, store_id):
        self.calls.append("get_deals")
        return []

    async def get_near_expiry(self, store_id, within_days=3):
        self.calls.append("get_near_expiry")
        return []

    async def get_product(self, sku):
        return w.BY_SKU.get(sku)


async def test_uses_catalog_at_call_time() -> None:
    services = Services()
    svc = make(services)
    catalog = _Catalog()
    services.catalog = catalog  # wired after construction: must still be used
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=10)
    assert "get_planogram" in catalog.calls
    skus = {r.sku for r in recs}
    assert "hot_sauce_cholula" not in skus  # live stock says 0
    guac = next(r for r in recs if r.sku == "guac_kit")
    assert guac.kind == "detour" and guac.deal is None  # catalog reported no deals
