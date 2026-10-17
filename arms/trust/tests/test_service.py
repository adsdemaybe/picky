import pytest

import arms.trust as trust_arm
from arms.trust.fake import FakeTrustService, demo_mandate
from arms.trust.mandates import verify_model
from arms.trust.payments import MockPaymentAdapter, StripeNotConfigured, VisaIntelligentCommerceAdapter
from arms.trust.service import TrustService as RealTrustService
from arms.trust.tests.helpers import TRIP, USER, checkout_request, line, maya_mandate, taco_cart
from contracts.services import Services, TrustService
from contracts.shopping import Recommendation
from contracts.trust import MandateReason as R
from contracts.trust import MandateVerdict as V
from contracts.trust import TransparencyEntry
from utils.config import reset_settings, set_settings


@pytest.fixture
async def svc():
    s = trust_arm.create_fake(Services())
    await s.set_mandate(maya_mandate())
    return s


def test_factories_satisfy_protocol():
    assert isinstance(trust_arm.create_fake(Services()), TrustService)
    set_settings(stripe_secret_key="")
    try:
        real = trust_arm.create(Services())
        assert isinstance(real, TrustService) and isinstance(real, RealTrustService)
        assert isinstance(real.payments, MockPaymentAdapter)  # degrades without a Stripe key
    finally:
        reset_settings()


def test_vic_adapter_refuses_live_or_missing_keys():
    with pytest.raises(StripeNotConfigured):
        VisaIntelligentCommerceAdapter("")
    with pytest.raises(StripeNotConfigured):
        VisaIntelligentCommerceAdapter("sk_live_abc")


async def test_mandate_signed_and_default_fallback():
    s = FakeTrustService()
    m = await s.set_mandate(maya_mandate())
    assert m.signature and verify_model(m)
    other = await s.get_mandate("nobody_in_fixtures_zz")
    assert other is not None and other.mandate_id == demo_mandate("nobody_in_fixtures_zz").mandate_id
    assert other.signature


async def test_evaluate_records_decision(svc):
    d = await svc.evaluate(USER, taco_cart(), line("l9", "hot-sauce-xl", 1299), budget_cents=4000, trip_id=TRIP)
    assert d.verdict == V.NEEDS_APPROVAL
    assert set(d.reasons) == {R.ABOVE_ASK_THRESHOLD, R.OVER_BUDGET}
    ledger = await svc.ledger(trip_id=TRIP)
    assert [x.decision_id for x in ledger.decisions] == [d.decision_id]
    assert ledger.audit[-1].action == "mandate.evaluate"


async def test_demo_checkout_mock_receipt(svc):
    req = checkout_request(budget_cents=4000, idempotency_key="idem1")
    r = await svc.checkout(req)
    assert r.status == "succeeded" and r.provider == "mock"
    assert r.amount_cents == 3639 and r.in_store_cents == 3290 and r.online_cents == 349
    assert r.online_order_id and r.online_order_id.startswith("ord_")
    assert r.cart_hash == req.cart_hash and r.approval_id == "appr_checkout1" and r.mandate_id == "mandate_maya"
    assert verify_model(r)
    assert await svc.checkout(req) == r  # idempotent retry
    replay = await svc.checkout(req.model_copy(update={"idempotency_key": None}))
    assert replay.status == "declined" and "already used" in (replay.error or "")
    ledger = await svc.ledger(user_id=USER, trip_id=TRIP)
    assert ledger.mandate is not None and len(ledger.receipts) == 2
    assert svc.audit.verify()


async def test_checkout_refusals_return_declined_receipt(svc):
    changed = taco_cart()
    changed.lines.append(line("l8", "lime", 50, qty=4, category="produce"))
    r = await svc.checkout(checkout_request(changed, approved_cart=taco_cart()))
    assert r.status == "declined" and r.provider == "mock" and r.online_order_id is None
    decline = await svc.checkout(checkout_request(approval_id="appr_2", payment_token="pm_card_chargeDeclined"))
    assert decline.status == "declined"
    ok = await svc.checkout(checkout_request(approval_id="appr_3"))
    assert ok.status == "succeeded"


async def test_transparency_upsert(svc):
    rec = Recommendation(
        rec_id="rec_hot1",
        user_id=USER,
        trip_id=TRIP,
        sku="hot-sauce-habanero",
        store_id="store_ave",
        interest=0.8,
        reason="You rated spicy salsa 5/5",
        signals={"similarity": 0.82},
    )
    entry = TransparencyEntry.from_recommendation(rec)
    await svc.record_transparency(entry)
    await svc.record_transparency(entry.model_copy(update={"outcome": "accepted"}))
    ledger = await svc.ledger(user_id=USER)
    assert len(ledger.transparency) == 1
    t = ledger.transparency[0]
    assert t.outcome == "accepted" and t.sponsored is False and t.reason


async def test_fixture_mandates_when_data_present():
    from utils.data import DataError, load_demo_fixtures

    try:
        users = load_demo_fixtures().users
    except DataError:
        pytest.skip("data/fixtures/users.yaml not written yet")
    s = FakeTrustService()
    for u in users:
        m = await s.get_mandate(u.user_id)
        assert m is not None and m.mandate_id == u.mandate.mandate_id and verify_model(m)
