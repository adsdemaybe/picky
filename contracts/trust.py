"""Trust contracts: AP2-style mandates, approvals, checkout, receipts, transparency + audit ledger.

Deterministic code decides; the LLM only proposes (CLAUDE.md §7-8). Checkout requires an ApprovalResponse whose
``cart_hash`` equals ``compute_cart_hash(cart, trip_id)`` of the cart being paid.

Limits: the standing ``IntentMandate.cap_cents`` (hard: over it -> DENY, OVER_CAP) and the trip's stated budget
(``ShoppingList.budget_cents``, 'under $40'; soft: over it -> NEEDS_APPROVAL, OVER_BUDGET) are both checked by
``TrustService.evaluate``/``checkout``. ``remaining_cents`` = min(cap, budget) - total. ``ask_above_cents`` applies
to each added LINE total (qty x unit price), not the whole cart.
"""

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, computed_field

from contracts.common import (
    ApprovalId,
    Cents,
    Channel,
    JsonScalar,
    LineId,
    MandateId,
    Model,
    OrderId,
    ReceiptId,
    RecId,
    Score01,
    Sku,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)
from contracts.shopping import Cart, CartLine, RecKind, Recommendation

PaymentProvider = Literal["stripe_test", "visa_sandbox", "mock"]
PaymentStatus = Literal["succeeded", "declined", "error"]
ApprovalDecision = Literal["approve", "reject", "timeout", "choice"]
ApprovalChannel = Literal["web", "voice", "photon", "system"]


# --------------------------------------------------------------------------------------------------------------------
# Mandates
# --------------------------------------------------------------------------------------------------------------------
class IntentMandate(Model):
    """The shopper's standing instructions to the agent (set before the trip, editable on the web)."""

    mandate_id: MandateId
    user_id: UserId
    cap_cents: Cents = Field(description="Max total spend per trip (in-store + online).")
    per_item_cap_cents: Cents | None = Field(None, description="Max for any single line total.")
    ask_above_cents: Cents = Field(description="'Ask me above $X': any added line whose total exceeds this asks.")
    allowed_categories: list[str] = Field(default_factory=list, description="Empty = all not blocked.")
    blocked_categories: list[str] = Field(default_factory=list)
    allowed_merchants: list[StoreId] = Field(default_factory=list, description="Empty = any store incl. online.")
    allow_online: bool = True
    require_checkout_approval: bool = Field(True, description="Always true for the demo; never auto-pay.")
    expires_at: Timestamp
    created_at: Timestamp = Field(default_factory=utcnow)
    revoked: bool = False
    signature: str | None = Field(None, description="HMAC over the canonical mandate JSON (trust arm).")


class MandateLine(Model):
    """Canonical, hash-relevant view of a cart line."""

    sku: Sku
    qty: int
    unit_price_cents: Cents
    source: Literal["in_store", "online"]
    store_id: StoreId


def _canonical_lines(lines: Iterable[CartLine]) -> list[dict[str, Any]]:
    rows = [
        MandateLine(
            sku=ln.sku, qty=ln.qty, unit_price_cents=ln.unit_price_cents, source=ln.source, store_id=ln.store_id
        ).model_dump()
        for ln in lines
        if ln.active
    ]
    rows.sort(key=lambda r: (r["store_id"], r["source"], r["sku"], r["unit_price_cents"], r["qty"]))
    return rows


def compute_cart_hash(cart: Cart, trip_id: TripId) -> str:
    """sha256 hex over the money-relevant content of a cart. ``trip_id`` is REQUIRED (everyone hashes the same way).

    Includes trip id, currency, shipping, total and the active lines (sku, qty, unit price, source, store), sorted.
    Excludes line ids, statuses, names, slots and instance ids, so picking/verifying does not change the hash, but any
    change to what is bought or what it costs does. Declined lines are excluded.
    Raises ValueError if ``cart.trip_id`` is set and differs from ``trip_id``.
    """
    if not trip_id:
        raise ValueError("compute_cart_hash: trip_id is required")
    if cart.trip_id is not None and cart.trip_id != trip_id:
        raise ValueError(f"compute_cart_hash: cart.trip_id {cart.trip_id!r} != trip_id {trip_id!r}")
    payload = {
        "trip_id": trip_id,
        "currency": cart.currency,
        "shipping_cents": cart.shipping_cents if cart.online_cents else 0,
        "total_cents": cart.total_cents,
        "lines": _canonical_lines(cart.lines),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CartMandate(Model):
    """A specific cart the agent proposes to buy, bound to an IntentMandate by hash."""

    cart_mandate_id: str
    trip_id: TripId
    user_id: UserId
    intent_mandate_id: MandateId | None = None
    currency: Literal["USD"] = "USD"
    lines: list[CartLine] = Field(default_factory=list, description="Active (non-declined) lines.")
    shipping_cents: Cents = 0
    total_cents: Cents
    cart_hash: str = Field(description="compute_cart_hash of the cart; sha256 hex.")
    created_at: Timestamp = Field(default_factory=utcnow)
    signature: str | None = None

    @classmethod
    def from_cart(
        cls, cart: Cart, *, trip_id: TripId, user_id: UserId, intent_mandate_id: MandateId | None = None
    ) -> "CartMandate":
        digest = compute_cart_hash(cart, trip_id)
        return cls(
            cart_mandate_id=f"cm_{digest[:16]}",
            trip_id=trip_id,
            user_id=user_id,
            intent_mandate_id=intent_mandate_id,
            currency=cart.currency,
            lines=[ln.model_copy() for ln in cart.active_lines()],
            shipping_cents=cart.shipping_cents,
            total_cents=cart.total_cents,
            cart_hash=digest,
        )

    def as_cart(self) -> Cart:
        return Cart(
            cart_id=self.cart_mandate_id,
            trip_id=self.trip_id,
            user_id=self.user_id,
            currency=self.currency,
            lines=list(self.lines),
            shipping_cents=self.shipping_cents,
        )

    def hash_matches(self) -> bool:
        cart = self.as_cart()
        return cart.total_cents == self.total_cents and self.cart_hash == compute_cart_hash(cart, self.trip_id)


class MandateVerdict(StrEnum):
    APPROVE = "approve"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class MandateReason(StrEnum):
    OK = "ok"
    NO_MANDATE = "no_mandate"
    MANDATE_EXPIRED = "mandate_expired"
    MANDATE_REVOKED = "mandate_revoked"
    OVER_CAP = "over_cap"
    OVER_ITEM_CAP = "over_item_cap"
    ABOVE_ASK_THRESHOLD = "above_ask_threshold"
    OVER_BUDGET = "over_budget"
    CATEGORY_BLOCKED = "category_blocked"
    CATEGORY_NOT_ALLOWED = "category_not_allowed"
    MERCHANT_NOT_ALLOWED = "merchant_not_allowed"
    ONLINE_NOT_ALLOWED = "online_not_allowed"
    CHECKOUT_REQUIRES_APPROVAL = "checkout_requires_approval"
    CART_HASH_MISMATCH = "cart_hash_mismatch"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_REPLAYED = "approval_replayed"


class MandateViolation(Model):
    """One failed check, pointing at the offending line when there is one (so the cart UI can highlight it)."""

    reason: MandateReason
    sku: Sku | None = None
    line_id: LineId | None = None
    message: str = ""


class MandateDecision(Model):
    decision_id: str
    user_id: UserId
    trip_id: TripId | None = None
    mandate_id: MandateId | None = None
    verdict: MandateVerdict
    reasons: list[MandateReason] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="Shopper-facing explanation per reason.")
    violations: list[MandateViolation] = Field(default_factory=list, description="Per-line detail of the reasons.")
    scope: Literal["cart", "line"] = "cart"
    sku: Sku | None = Field(None, description="Set when scope='line'.")
    total_cents: Cents | None = Field(None, description="Projected cart total evaluated.")
    budget_cents: Cents | None = Field(None, description="Trip budget used (None = no stated budget).")
    remaining_cents: int | None = Field(None, description="min(cap, budget) - total (negative = over).")
    cart_hash: str | None = None
    evaluated_at: Timestamp = Field(default_factory=utcnow)


# --------------------------------------------------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------------------------------------------------
class ApprovalKind(StrEnum):
    RECOMMENDATION = "recommendation"
    OVER_THRESHOLD = "over_threshold"
    SUBSTITUTION = "substitution"
    ONLINE_ORDER = "online_order"
    CHECKOUT = "checkout"
    PICK_CHOICE = "pick_choice"


class ApprovalOption(Model):
    option_id: str
    label: str
    detail: str | None = None
    thumbnail_b64: str | None = None


class ApprovalRequest(Model):
    approval_id: ApprovalId
    trip_id: TripId
    user_id: UserId
    kind: ApprovalKind
    title: str = Field(description="Short question: 'Add pepper-jack? 30% off'.")
    body: str = ""
    amount_cents: Cents | None = None
    sku: Sku | None = None
    rec_id: RecId | None = None
    options: list[ApprovalOption] = Field(
        default_factory=list, description="Empty = yes/no. pick_choice: one option per candidate instance."
    )
    cart_hash: str | None = Field(None, description="Required for kind=checkout: the cart the user is approving.")
    multi: bool = Field(False, description="True = several options may be chosen (checkout batch 'want any?').")
    expires_s: float = Field(120.0, gt=0)
    channels: list[Channel] = Field(default_factory=lambda: ["web"], description="Where to ask.")
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: Timestamp = Field(default_factory=utcnow)


class ApprovalResponse(Model):
    approval_id: ApprovalId
    decision: ApprovalDecision
    choice: str | None = Field(None, description="option_id when decision='choice' (single select).")
    choices: list[str] = Field(default_factory=list, description="option_ids when decision='choice' and multi=True.")
    channel: ApprovalChannel = "web"
    user_id: UserId | None = None
    cart_hash: str | None = Field(
        None,
        description="Checkout approvals: the approved cart hash. Web answers MUST echo it; for photon/voice answers "
        "the ApprovalBroker copies it from the pending request before resolving.",
    )
    note: str | None = None
    ts: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def approved(self) -> bool:
        return self.decision in ("approve", "choice")

    def chosen(self) -> list[str]:
        """All chosen option_ids (``choice`` and ``choices`` merged, order kept)."""
        out = list(self.choices)
        if self.choice is not None and self.choice not in out:
            out.insert(0, self.choice)
        return out


# --------------------------------------------------------------------------------------------------------------------
# Checkout + receipts
# --------------------------------------------------------------------------------------------------------------------
class CheckoutRequest(Model):
    """Built by the gateway (TripController.start_checkout) after the shopper approved the checkout approval.

    Trust verifies: approval_request.kind == CHECKOUT, approval.approved, and approval_request.cart_hash ==
    approval.cart_hash == cart_hash == compute_cart_hash(cart, trip_id); then mandate (cap + budget), then charges.
    """

    trip_id: TripId
    user_id: UserId
    cart: Cart
    cart_hash: str = Field(description="compute_cart_hash(cart, trip_id) as shown to the user.")
    budget_cents: Cents | None = Field(None, description="Trip budget (ShoppingList.budget_cents).")
    approval_request: ApprovalRequest = Field(description="The kind=checkout request the shopper answered.")
    approval: ApprovalResponse = Field(description="Explicit checkout approval bound to cart_hash.")
    payment_token: str = Field("pm_card_visa", description="Tokenized instrument ref; never a card number.")
    idempotency_key: str | None = None


class Receipt(Model):
    receipt_id: ReceiptId
    trip_id: TripId
    user_id: UserId
    provider: PaymentProvider
    status: PaymentStatus
    amount_cents: Cents
    currency: Literal["USD"] = "USD"
    lines: list[CartLine] = Field(default_factory=list)
    in_store_cents: Cents = 0
    online_cents: Cents = 0
    online_order_id: OrderId | None = Field(
        None, description="Minted by TrustService.checkout (mock order id) when the cart has online lines."
    )
    provider_ref: str | None = Field(None, description="Stripe PaymentIntent id (pi_...) or sandbox ref.")
    cart_hash: str
    mandate_id: MandateId | None = None
    approval_id: ApprovalId | None = None
    error: str | None = None
    signature: str | None = None
    created_at: Timestamp = Field(default_factory=utcnow)


# --------------------------------------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------------------------------------
class TransparencyEntry(Model):
    """Why a recommendation was shown (no hidden paid placement).

    Recorded by the GATEWAY (it knows the trip): once at recs.ready (outcome='pending') and again when the approval
    resolves. ``TrustService.record_transparency`` UPSERTS on ``rec_id`` (entry_id = rec_id by convention).
    """

    entry_id: str
    user_id: UserId
    trip_id: TripId | None = None
    rec_id: RecId
    sku: Sku
    store_id: StoreId
    kind: RecKind = "detour"
    reason: str
    signals: dict[str, JsonScalar] = Field(default_factory=dict)
    interest: Score01 = 0.0
    sponsored: bool = False
    outcome: Literal["pending", "accepted", "declined", "ignored"] = "pending"
    shown_at: Timestamp = Field(default_factory=utcnow)

    @classmethod
    def from_recommendation(cls, rec: Recommendation, trip_id: TripId | None = None) -> "TransparencyEntry":
        return cls(
            entry_id=rec.rec_id,
            user_id=rec.user_id,
            trip_id=trip_id if trip_id is not None else rec.trip_id,
            rec_id=rec.rec_id,
            sku=rec.sku,
            store_id=rec.store_id,
            kind=rec.kind,
            reason=rec.reason,
            signals=dict(rec.signals),
            interest=rec.interest,
            sponsored=rec.sponsored,
        )


class AuditEntry(Model):
    """Append-only, hash-chained audit record."""

    entry_id: str
    seq: int = Field(ge=0)
    actor: str = Field(description="Arm or principal: 'trust', 'user:maya', 'agent', ...")
    action: str = Field(description="Dotted verb: mandate.evaluate, approval.resolved, checkout.charge, ...")
    trip_id: TripId | None = None
    user_id: UserId | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    hash: str | None = None
    signature: str | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class TrustLedger(Model):
    """Response of TrustService.ledger / GET /api/trust/ledger."""

    user_id: UserId | None = None
    trip_id: TripId | None = None
    mandate: IntentMandate | None = None
    decisions: list[MandateDecision] = Field(default_factory=list)
    transparency: list[TransparencyEntry] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    audit: list[AuditEntry] = Field(default_factory=list)
