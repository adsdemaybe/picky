"""Wireless engine + fake service over the shipped data (data/wireless.yaml, products, store_tmobile planogram)."""

import asyncio

import pytest

from arms.wireless.data import load_wireless
from arms.wireless.engine import WirelessEngine
from arms.wireless.fake import FakeWirelessService
from contracts.wireless import (
    FinancingRequest,
    HouseholdRequest,
    PromoCondition,
    QuoteItem,
    QuoteRequest,
    TradeInCondition,
    TradeInRequest,
    TradeInSelection,
)
from utils.data import load_planogram, load_products


@pytest.fixture(scope="module")
def svc() -> FakeWirelessService:
    return FakeWirelessService()


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def test_data_references_known_skus_and_stocked_store() -> None:
    data = load_wireless()
    products = load_products()
    for acc, fits in data.compatibility.items():
        assert acc in products
        assert all(s in products or s in ("*", "PX7") for s in fits)
    for p in data.promotions:
        assert set(p.skus) <= set(products) and set(p.with_skus) <= set(products)
    slots = {s.sku for s in load_planogram("store_tmobile").slots}
    assert set(data.compatibility) <= slots, "every compatible accessory is on a store_tmobile shelf"


def test_plan_prices_lines_and_autopay(svc: FakeWirelessService) -> None:
    q = run(svc.quote_plan("value_unlimited", 2))
    assert (q.advertised_monthly_cents, q.monthly_cents, q.per_line_cents) == (9000, 9000, 4500)
    assert q.est_taxes_fees_cents == 1080  # 12% estimate
    no_autopay = run(svc.quote_plan("value_unlimited", 2, autopay=False))
    assert no_autopay.monthly_cents == 10000
    six = run(svc.quote_plan("plus_unlimited", 6))
    assert six.advertised_monthly_cents == 19000 + 2 * 3000
    with pytest.raises(ValueError):
        run(svc.quote_plan("unlimited_55", 3))
    with pytest.raises(KeyError):
        run(svc.quote_plan("nope", 1))


def test_compare_plans_puts_ineligible_last(svc: FakeWirelessService) -> None:
    quotes = run(svc.compare_plans(2))
    assert [q.plan_id for q in quotes][-1] == "unlimited_55" and not quotes[-1].eligible
    assert all("prepaid" not in q.plan_id for q in quotes), "prepaid is single-line"
    elig = run(svc.compare_plans(2, eligibility=["age_55"]))
    assert elig[0].plan_id == "unlimited_55" and elig[0].monthly_cents == 8000


def test_financing_rounds_and_applies_only_eligible_promos(svc: FakeWirelessService) -> None:
    plain = run(svc.finance(FinancingRequest(sku="IP17-256")))
    assert plain.monthly_cents == 92999 // 24
    assert plain.monthly_cents * 23 + plain.last_month_cents == 92999
    assert plain.promo_ids == [] and plain.total_cost_cents == 92999
    promo = run(svc.finance(FinancingRequest(sku="PX10-128", plan_id="plus_unlimited", new_line=True)))
    assert promo.promo_ids == ["px10_new_line"] and promo.net_monthly_cents == 0
    no_line = run(svc.finance(FinancingRequest(sku="PX10-128", plan_id="plus_unlimited")))
    assert no_line.promo_ids == []
    value_plan = run(svc.finance(FinancingRequest(sku="PX10-128", plan_id="value_unlimited", new_line=True)))
    assert value_plan.promo_ids == [], "needs Plus or Premium"
    down = run(svc.finance(FinancingRequest(sku="GS26U-256", down_payment_cents=20000)))
    assert down.down_payment_cents == 20000 and down.monthly_cents == (139999 - 20000) // 24
    with pytest.raises(ValueError):
        run(svc.finance(FinancingRequest(sku="CASE-PX9-CLR")))
    with pytest.raises(ValueError):
        run(svc.finance(FinancingRequest(sku="PX10-128", months=12)))


def test_trade_in_says_wait_for_maya(svc: FakeWirelessService) -> None:
    q = run(
        svc.estimate_trade_in(
            TradeInRequest(model_id="pixel-7", condition=TradeInCondition.GOOD, user_id="maya", target_sku="PX9-128")
        )
    )
    assert q.value_cents == 8000 and q.remaining_installments_cents == 4 * 1650 and q.net_cents == 1400
    assert q.keep_recommended
    assert any("Nov 27" in r for r in q.reasons)


def test_trade_in_keep_young_phone_and_bonus(svc: FakeWirelessService) -> None:
    ruth = run(svc.estimate_trade_in(TradeInRequest(model_id="iphone-15", condition="excellent", user_id="ruth")))
    assert ruth.keep_recommended and ruth.remaining_installments_cents == 0
    jordan = run(
        svc.estimate_trade_in(
            TradeInRequest(
                model_id="iphone-13",
                condition="good",
                user_id="jordan",
                target_sku="IP18PRO-256",
                plan_id="premium_unlimited",
            )
        )
    )
    assert jordan.promo_ids == ["ip18p_trade_in"] and jordan.value_cents + jordan.bonus_cents == 80000
    assert not jordan.keep_recommended
    with pytest.raises(KeyError):
        run(svc.estimate_trade_in(TradeInRequest(model_id="nokia-3310", condition="good")))


def test_household_optimizer(svc: FakeWirelessService) -> None:
    garcia = run(svc.optimize_household(HouseholdRequest(user_id="garcia")))
    assert garcia.recommended_plan_id == "value_unlimited" and not garcia.keep_current
    assert garcia.options[0].savings_monthly_cents == 24000 - 13000
    maya = run(svc.optimize_household(HouseholdRequest(user_id="maya")))
    assert maya.keep_current and maya.recommended_plan_id is None
    dev = run(svc.optimize_household(HouseholdRequest(user_id="dev")))
    assert dev.recommended_plan_id == "premium_unlimited" and dev.options[0].savings_monthly_cents < 0
    with pytest.raises(KeyError):
        run(svc.optimize_household(HouseholdRequest(user_id="nobody")))


def test_compatible_accessories(svc: FakeWirelessService) -> None:
    px9 = run(svc.compatible_accessories("PX9-128"))
    skus = [a.sku for a in px9]
    assert skus[:2] == ["CASE-PX9-CLR", "CASE-PX9-RUG"] and "GLASS-PX9" in skus
    assert "CASE-IP16-MAG" not in skus and "CHG-PXSNAP" not in skus
    assert all(a.optional for a in px9) and all(a.in_stock > 0 for a in px9)
    flip = run(svc.compatible_accessories("GZF8-256"))
    assert not any(a.subcategory == "cases" for a in flip), "no Flip8 case in the catalog: never invent one"


def test_quote_separates_upfront_and_monthly(svc: FakeWirelessService) -> None:
    req = QuoteRequest(
        user_id="maya",
        items=[QuoteItem(sku="PX9-128", financed=True), QuoteItem(sku="CASE-PX9-CLR")],
        plan_id="value_unlimited",
        lines=2,
    )
    b = run(svc.quote(req))
    assert b.ok and not b.warnings
    tax = (79999 + 2499) * 1035 // 10000 + (1 if ((79999 + 2499) * 1035) % 10000 >= 5000 else 0)
    assert b.est_tax_upfront_cents == tax
    assert b.upfront_cents == 2499 + tax
    assert b.monthly_cents == 79999 // 24 + 9000 + 1080
    assert b.total_over_term_cents == b.upfront_cents + 79999 + (9000 + 1080) * 24
    assert b.synthetic and "Synthetic" in b.notice


def test_quote_reports_problems_without_raising(svc: FakeWirelessService) -> None:
    b = run(
        svc.quote(
            QuoteRequest(
                items=[QuoteItem(sku="NOPE"), QuoteItem(sku="CASE-PX9-RUG", qty=10), QuoteItem(sku="CASE-IP16-MAG")],
                plan_id="unlimited_55",
                lines=3,
                add_on_promo_ids=["home_internet_bundle"],
            )
        )
    )
    text = " ".join(b.errors)
    assert not b.ok
    assert "NOPE" in text and "in stock" in text and "2 lines" in text and "Plus or Premium" in text
    assert any("iPhone 16" in w for w in b.warnings)


def test_quote_trade_in_and_bundle_promo(svc: FakeWirelessService) -> None:
    b = run(
        svc.quote(
            QuoteRequest(
                items=[QuoteItem(sku="GS26-256", financed=True), QuoteItem(sku="CHG-45W")],
                plan_id="plus_unlimited",
                port_in=True,
                new_lines=1,
                trade_in=TradeInSelection(model_id="galaxy-s22", condition="good"),
            )
        )
    )
    kinds = {(ln.kind, ln.promo_id) for ln in b.lines}
    assert ("credit", "gs26_switcher") in kinds and ("credit", "charger_with_phone") in kinds
    trade = next(ln for ln in b.lines if ln.kind == "trade_in")
    assert trade.upfront_cents == -10000
    assert any(ln.kind == "fee" and "New line" in ln.label for ln in b.lines)


def test_upcoming_sale_is_not_applied() -> None:
    eng = WirelessEngine(load_wireless())
    elig = {e.promo.promo_id: e for e in eng.promotions(QuoteRequest(items=[QuoteItem(sku="BUDS-RUN")]))}
    assert not elig["buds4_weekend"].eligible and elig["buds4_weekend"].upcoming
    assert PromoCondition.ACTIVE in elig["buds4_weekend"].unmet
