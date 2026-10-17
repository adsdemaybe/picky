from datetime import timedelta

from contracts.common import utcnow
from contracts.shopping import Cart, CartLine
from contracts.trust import (
    ApprovalKind,
    ApprovalRequest,
    ApprovalResponse,
    CheckoutRequest,
    IntentMandate,
    compute_cart_hash,
)

TRIP = "trip_demo1"
USER = "maya"


def line(line_id, sku, cents, qty=1, category="grocery", store_id="store_ave", **kw) -> CartLine:
    return CartLine(
        line_id=line_id,
        sku=sku,
        name=kw.pop("name", sku),
        category=category,
        qty=qty,
        unit_price_cents=cents,
        source="online" if store_id == "online" else "in_store",
        store_id=store_id,
        **kw,
    )


def taco_cart() -> Cart:
    """'tacos for 6, under $40': in-store basket + one OOS item sourced online. Total 3639 cents."""
    return Cart(
        cart_id="cart_demo1",
        trip_id=TRIP,
        user_id=USER,
        lines=[
            line("l1", "tortillas-corn-30", 399, category="bakery"),
            line("l2", "beef-ground-1lb", 699, qty=2, category="meat"),
            line("l3", "beans-black-15oz", 129, qty=2, category="pantry"),
            line("l4", "cheese-mex-blend", 449, category="dairy"),
            line("l5", "avocado-hass", 129, qty=3, category="produce"),
            line("l6", "salsa-roja-mild", 399, category="pantry"),
            line("l7", "crema-mexicana", 349, category="dairy", store_id="online"),
        ],
        shipping_cents=0,
    )


def maya_mandate(**kw) -> IntentMandate:
    data = {
        "mandate_id": "mandate_maya",
        "user_id": USER,
        "cap_cents": 6000,
        "ask_above_cents": 1000,
        "blocked_categories": ["alcohol", "tobacco"],
        "expires_at": utcnow() + timedelta(days=7),
    }
    data.update(kw)
    return IntentMandate(**data)


def checkout_request(cart: Cart | None = None, *, approved_cart: Cart | None = None, **kw) -> CheckoutRequest:
    """Checkout request whose approval is bound to ``approved_cart`` (default: the cart being paid)."""
    cart = cart or taco_cart()
    approved = compute_cart_hash(approved_cart or cart, TRIP)
    areq = ApprovalRequest(
        approval_id=kw.pop("approval_id", "appr_checkout1"),
        trip_id=TRIP,
        user_id=USER,
        kind=kw.pop("kind", ApprovalKind.CHECKOUT),
        title="Pay?",
        amount_cents=cart.total_cents,
        cart_hash=approved,
    )
    appr = ApprovalResponse(
        approval_id=areq.approval_id,
        decision=kw.pop("decision", "approve"),
        user_id=USER,
        cart_hash=approved,
    )
    return CheckoutRequest(
        trip_id=TRIP,
        user_id=USER,
        cart=cart,
        cart_hash=compute_cart_hash(cart, TRIP),
        approval_request=areq,
        approval=appr,
        **kw,
    )
