from datetime import timedelta

from arms.trust.mandates import (
    AuditChain,
    build_cart_mandate,
    check_checkout,
    evaluate_mandate,
    project_cart,
    sign_model,
    verify_model,
)
from arms.trust.tests.helpers import TRIP, USER, checkout_request, line, maya_mandate, taco_cart
from contracts.common import utcnow
from contracts.trust import ApprovalKind, compute_cart_hash
from contracts.trust import MandateReason as R
from contracts.trust import MandateVerdict as V

KEY = "test-key"


def ev(mandate, cart, ln=None, **kw):
    return evaluate_mandate(mandate, cart, ln, user_id=USER, trip_id=TRIP, **kw)


def test_taco_cart_within_mandate_approves():
    cart = taco_cart()
    assert cart.total_cents == 3639
    d = ev(maya_mandate(), cart, budget_cents=4000)
    assert d.verdict == V.APPROVE and d.reasons == [R.OK]
    assert d.scope == "cart" and d.total_cents == 3639
    assert d.remaining_cents == 4000 - 3639
    assert d.cart_hash == compute_cart_hash(cart, TRIP)


def test_over_cap_denies():
    d = ev(maya_mandate(cap_cents=3000), taco_cart())
    assert d.verdict == V.DENY and R.OVER_CAP in d.reasons
    assert d.remaining_cents == 3000 - 3639


def test_over_budget_needs_approval():
    d = ev(maya_mandate(), taco_cart(), budget_cents=3000)
    assert d.verdict == V.NEEDS_APPROVAL and d.reasons == [R.OVER_BUDGET]
    assert d.remaining_cents == 3000 - 3639


def test_ask_above_threshold_is_per_line_and_strict():
    m = maya_mandate(ask_above_cents=1000)
    cart = taco_cart()
    exact = line("l9", "hot-sauce-xl", 1000)
    assert ev(m, cart, exact).verdict == V.APPROVE
    pricey = line("l9", "hot-sauce-xl", 550, qty=2)  # line total 1100 > 1000
    d = ev(m, cart, pricey)
    assert d.verdict == V.NEEDS_APPROVAL and d.reasons == [R.ABOVE_ASK_THRESHOLD]
    assert d.scope == "line" and d.sku == "hot-sauce-xl"
    assert d.violations[0].line_id == "l9"
    assert d.total_cents == 3639 + 1100
    assert d.cart_hash == compute_cart_hash(project_cart(cart, pricey), TRIP)
    # cart scope never re-asks for lines already added (beef line is 1398 > 1000)
    assert ev(m, cart).verdict == V.APPROVE


def test_categories_blocked_and_not_allowed():
    cart = taco_cart()
    d = ev(maya_mandate(), cart, line("l9", "beer-6pk", 899, category="Alcohol"))
    assert d.verdict == V.DENY and d.reasons == [R.CATEGORY_BLOCKED]
    m = maya_mandate(allowed_categories=["bakery", "meat", "pantry", "dairy", "produce"])
    assert ev(m, cart).verdict == V.APPROVE
    d = ev(m, cart, line("l9", "paper-towels", 499, category="household"))
    assert d.verdict == V.DENY and d.reasons == [R.CATEGORY_NOT_ALLOWED]


def test_expired_and_revoked_deny():
    d = ev(maya_mandate(expires_at=utcnow() - timedelta(minutes=1)), taco_cart())
    assert d.verdict == V.DENY and R.MANDATE_EXPIRED in d.reasons
    d = ev(maya_mandate(revoked=True), taco_cart())
    assert d.verdict == V.DENY and R.MANDATE_REVOKED in d.reasons
    future = utcnow() + timedelta(days=30)
    assert ev(maya_mandate(), taco_cart(), now=future).verdict == V.DENY


def test_merchant_online_item_cap():
    cart = taco_cart()
    assert R.ONLINE_NOT_ALLOWED in ev(maya_mandate(allow_online=False), cart).reasons
    d = ev(maya_mandate(allowed_merchants=["store_hill"]), cart)
    assert d.verdict == V.DENY and R.MERCHANT_NOT_ALLOWED in d.reasons
    d = ev(maya_mandate(per_item_cap_cents=1000), cart)
    assert d.verdict == V.DENY and [v.line_id for v in d.violations] == ["l2"]


def test_no_mandate_asks():
    d = ev(None, taco_cart())
    assert d.verdict == V.NEEDS_APPROVAL and d.reasons == [R.NO_MANDATE] and d.mandate_id is None


def test_deny_wins_over_needs_approval():
    d = ev(maya_mandate(cap_cents=3000), taco_cart(), line("l9", "x", 2000), budget_cents=2000)
    assert d.verdict == V.DENY
    assert {R.OVER_CAP, R.OVER_BUDGET, R.ABOVE_ASK_THRESHOLD} <= set(d.reasons)


def test_cart_mandate_hash_and_signature():
    cart = taco_cart()
    cm = build_cart_mandate(cart, trip_id=TRIP, user_id=USER, mandate=maya_mandate(), key=KEY)
    assert cm.cart_hash == compute_cart_hash(cart, TRIP) and cm.hash_matches()
    assert cm.intent_mandate_id == "mandate_maya" and cm.total_cents == 3639
    assert verify_model(cm, KEY)
    assert not verify_model(cm.model_copy(update={"total_cents": 1}), KEY)
    assert not verify_model(cm, "other-key")
    m = sign_model(maya_mandate(), KEY)
    assert verify_model(m, KEY) and not verify_model(m.model_copy(update={"cap_cents": 99999}), KEY)


def test_checkout_preconditions_ok():
    d = check_checkout(checkout_request(budget_cents=4000), maya_mandate())
    assert d.verdict == V.APPROVE and d.reasons == [R.OK]


def test_checkout_over_budget_with_explicit_approval_passes():
    d = check_checkout(checkout_request(budget_cents=3000), maya_mandate())
    assert d.verdict == V.APPROVE and d.reasons == [R.OVER_BUDGET]


def test_checkout_cart_changed_after_approval():
    approved = taco_cart()
    changed = taco_cart()
    changed.lines.append(line("l8", "lime", 50, qty=4, category="produce"))
    d = check_checkout(checkout_request(changed, approved_cart=approved), maya_mandate())
    assert d.verdict == V.DENY and R.CART_HASH_MISMATCH in d.reasons


def test_checkout_rejected_timeout_wrong_kind_replay():
    m = maya_mandate()
    assert R.APPROVAL_REJECTED in check_checkout(checkout_request(decision="reject"), m).reasons
    assert R.APPROVAL_EXPIRED in check_checkout(checkout_request(decision="timeout"), m).reasons
    d = check_checkout(checkout_request(kind=ApprovalKind.RECOMMENDATION), m)
    assert d.verdict == V.DENY and R.APPROVAL_MISSING in d.reasons
    d = check_checkout(checkout_request(), m, used_approval_ids={"appr_checkout1"})
    assert d.verdict == V.DENY and R.APPROVAL_REPLAYED in d.reasons


def test_checkout_late_answer_and_mandate_deny():
    req = checkout_request()
    late = req.approval.model_copy(update={"ts": req.approval_request.created_at + timedelta(seconds=500)})
    d = check_checkout(req.model_copy(update={"approval": late}), maya_mandate())
    assert d.verdict == V.DENY and R.APPROVAL_EXPIRED in d.reasons
    d = check_checkout(checkout_request(), maya_mandate(cap_cents=1000))
    assert d.verdict == V.DENY and R.OVER_CAP in d.reasons


def test_audit_chain_links_and_detects_tampering():
    chain = AuditChain(KEY)
    a = chain.append("trust", "mandate.evaluate", trip_id=TRIP, data={"verdict": "approve"})
    b = chain.append("trust", "checkout.charge", trip_id=TRIP, data={"amount_cents": 3639})
    assert a.prev_hash is None and b.prev_hash == a.hash and b.seq == 1
    assert chain.verify()
    chain.entries[0] = a.model_copy(update={"data": {"verdict": "deny"}})
    assert not chain.verify()
