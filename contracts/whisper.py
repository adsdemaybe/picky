"""Cart Whisperer (T-Mobile track "checkout suggestions that actually help"; design: docs/cart-whisperer.md).

0.2.5, additive. Owned by the recommender arm (``RecommenderService.whisper`` / ``whisper_feedback``); the gateway
builds the ``ShopperContext``, serves ``GET /api/users/{user_id}/whisper`` and applies accepts through the normal
cart path (mandate check + approval when needed); trust gates it; the web app and messaging show it.

Pipeline (all deterministic except the wording): candidates (complete / replenish / swap_save / qty / deal / remove /
skip / wait) -> hard gates in code (stock, compatibility, allergens / dislikes, mandate + budget, not declined, max
detour, not already pending) -> deterministic score + ``reason_codes`` -> below the threshold nothing is shown ->
the LLM may only PHRASE ``reason`` from the card's reason codes + signals (template fallback) -> at most
``WHISPER_MAX_CARDS`` cards per phase, SKIP and WAIT included. **Zero cards is a valid answer** (``empty_reason``).

Trust (CLAUDE.md section 8): every card carries ``reason`` + ``reason_codes`` + ``signals``; ``sponsored`` is always
False (a sponsored card would be labeled); an accept is a normal cart change (``TrustService.evaluate``, approval
when needed); a WAIT accept schedules a reminder; a decline feeds the profile (suppressed, kind down-weighted). The
raw ``ShopperContext`` stays on the server: clients see cards and their derived signals only.
"""

from datetime import date
from typing import Literal, get_args

from pydantic import Field, model_validator

from contracts.common import (
    Cents,
    Channel,
    LineId,
    Model,
    Pose2D,
    Score01,
    SignedCents,
    Sku,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)
from contracts.profile import ProfileUpdate
from contracts.routing import Stop
from contracts.shopping import Cart, ShoppingList
from contracts.trust import ApprovalRequest, IntentMandate, MandateDecision, TransparencyEntry

WhisperKind = Literal["complete", "replenish", "swap_save", "qty", "deal", "remove", "skip", "wait"]
"""complete = the stated occasion / setup is missing a part; replenish = you are probably out (cadence); swap_save =
same need, cheaper; qty = quantity does not match servings / pack size; deal = a deal that fits taste + intent;
remove = allergen, shipping waste or over budget (drop a line); skip = don't buy it (already owned, bought recently,
duplicate, incompatible, price above typical); wait = don't buy it now (a promo is coming, installments left)."""
WhisperAction = Literal["add", "swap", "set_qty", "remove", "skip", "remind"]
"""What an accept does: add a line, swap ``line_sku`` for ``sku``, set the line's qty to ``qty``, remove the line,
skip (remove the line if it is in the cart, else just acknowledge), remind (WAIT: drop / don't add the line and
schedule a reminder on ``remind_on``)."""
WhisperPhase = Literal["planning", "in_trip", "checkout"]
WhisperSignalSource = Literal["commerce", "customer", "physical"]
"""The brief's three signal families: commerce (cart, prices, deals, promo calendar, price history), customer
(profile, history + cadence, budget + mandate, owned items / devices, installments), physical (robot pose, the slot
ahead, what was just picked, detour)."""
WhisperReasonCode = Literal[
    "ALREADY_OWNED",
    "BOUGHT_RECENTLY",
    "DUPLICATE",
    "INCOMPATIBLE",
    "PROMO_SOON",
    "PRICE_ABOVE_TYPICAL",
    "INSTALLMENTS_LEFT",
    "OVER_BUDGET",
    "COMPLETES_OCCASION",
    "QTY_MISMATCH",
    "CHEAPER_SAME_NEED",
    "RUNNING_LOW",
    "DEAL_MATCH",
    "ALLERGEN",
    "SHIPPING_WASTE",
    "ON_ROUTE",
]
WhisperOutcome = Literal[
    "applied", "needs_approval", "denied", "reminder_scheduled", "acknowledged", "declined", "stale", "not_applied"
]
"""POST accept / decline result: applied (cart changed), needs_approval (an approval was raised; the change applies
after the shopper approves it), denied (the mandate said no; nothing changed), reminder_scheduled (WAIT),
acknowledged (SKIP of something not in the cart), declined (decline recorded), stale (the card's line is gone),
not_applied (no trip / cart to change: the card was recorded, nothing else)."""

WHISPER_KINDS: tuple[str, ...] = ("complete", "replenish", "swap_save", "qty", "deal", "remove", "skip", "wait")
WHISPER_REASON_CODES: tuple[str, ...] = get_args(WhisperReasonCode)
WHISPER_MAX_CARDS = 3
"""At most this many cards per phase, SKIP and WAIT included (zero is valid)."""
WHISPER_DEFAULT_ACTION: dict[str, str] = {
    "complete": "add",
    "replenish": "add",
    "swap_save": "swap",
    "qty": "set_qty",
    "deal": "add",
    "remove": "remove",
    "skip": "skip",
    "wait": "remind",
}
WHISPER_EMPTY_REASON = "Nothing worth adding today."


class WhisperSignal(Model):
    """One derived fact behind a card ("Why am I seeing this?"). The only facts the reason may cite."""

    name: str = Field(description="Machine name, e.g. 'last_bought_days', 'promo_price', 'detour_m'.")
    value: str | int | float | bool
    source: WhisperSignalSource
    label: str | None = Field(None, description="Plain words for the UI: 'You bought one on Mar 14'.")


class WhisperCard(Model):
    """One Cart Whisperer card. ``card_id`` is stable for the same (user, kind, sku, line_sku) across passes and
    phases (``whisper_card_id``), so a declined card stays suppressed and is never re-asked in the same trip."""

    card_id: str
    kind: WhisperKind
    action: WhisperAction
    phase: WhisperPhase = "planning"
    user_id: UserId
    store_id: StoreId
    trip_id: TripId | None = None
    sku: Sku | None = Field(
        None,
        description="The product the card is about: added / swapped in / skipped / waited on. None only for a SKIP "
        "or WAIT of a cart line whose product is unknown (then ``line_sku`` names it).",
    )
    line_sku: Sku | None = Field(
        None,
        description="The existing list / cart line the card acts on (swap: the line replaced; qty / remove / skip / "
        "wait of a line: that line's sku). None for adds.",
    )
    line_id: LineId | None = Field(None, description="Cart line acted on, when it is already in the cart.")
    name: str = Field(description="Product name of ``sku`` (or of ``line_sku``).")
    title: str = Field("", description="Card headline: 'Skip the charger', 'Add Taco Seasoning Mix'.")
    qty: int = Field(1, ge=0, description="add / swap: units; set_qty: the new qty; remove / skip / wait: 0.")
    price_cents: Cents = Field(description="Unit price of the product at ``store_id`` now (deal applied).")
    price_delta_cents: SignedCents = Field(
        0,
        description="Signed change to the cart total if accepted (negative = cheaper). 0 for SKIP/WAIT of a "
        "product that is not in the cart (that is avoided spend, not a cart change).",
    )
    savings_cents: Cents | None = Field(None, description="Money saved for the same need (swap / qty / remove).")
    avoided_cents: Cents | None = Field(
        None, description="SKIP / WAIT: spend avoided (WAIT: an estimate from the promo calendar, labeled 'expected')."
    )
    reason: str = Field(min_length=1, description="One shopper-facing line; cites only reason codes + signals.")
    reason_codes: list[WhisperReasonCode] = Field(min_length=1, description="Deterministic, set by code.")
    signals: list[WhisperSignal] = Field(default_factory=list)
    score: Score01 = Field(description="Deterministic score; cards below the threshold are never shown.")
    remind_on: date | None = Field(None, description="WAIT: the day the promo starts (the reminder date).")
    promo_price_cents: Cents | None = Field(None, description="WAIT: the expected price on ``remind_on``.")
    needs_approval: bool = Field(
        False, description="Mandate pre-check: accepting will raise an approval (over budget / ask-above threshold)."
    )
    detour_m: float | None = Field(None, ge=0, description="Extra walking for an add / swap (None = no pick needed).")
    aisle_label: str | None = Field(None, description="'Accessory wall · shelf 3' where the product is.")
    image_url: str | None = Field(None, description="Root-relative product thumbnail '/api/products/{sku}/image.jpg'.")
    phrased_by: Literal["template", "llm"] = Field(
        "template", description="'llm' = the LLM phrased ``reason`` (validated against the signals)."
    )
    sponsored: bool = Field(False, description="Always False (no paid placement); a sponsored card would be labeled.")

    @model_validator(mode="after")
    def _shape(self) -> "WhisperCard":
        if self.sku is None and self.line_sku is None:
            raise ValueError(f"card {self.card_id}: sku or line_sku is required")
        if self.action == "remind" and self.remind_on is None:
            raise ValueError(f"card {self.card_id}: a WAIT (remind) card needs remind_on")
        return self

    @property
    def product_sku(self) -> Sku:
        """The SKU the card is about (``sku``, else ``line_sku``)."""
        return self.sku or self.line_sku or ""


class WhisperResponse(Model):
    """``GET /api/users/{user_id}/whisper``: one Cart Whisperer pass. ``cards`` may be empty (then ``empty_reason``)."""

    user_id: UserId
    store_id: StoreId
    phase: WhisperPhase
    trip_id: TripId | None = None
    cards: list[WhisperCard] = Field(default_factory=list, max_length=WHISPER_MAX_CARDS)
    empty_reason: str | None = Field(None, description="Set when ``cards`` is empty: 'Nothing worth adding today.'")
    considered: int = Field(0, ge=0, description="Candidates that passed the hard gates (shown or below threshold).")
    hidden_declined: int = Field(0, ge=0, description="Candidates hidden because the shopper declined them before.")
    threshold: float = Field(0.35, description="Score threshold used (below it nothing is shown).")
    generated_at: Timestamp = Field(default_factory=utcnow)


class WhisperDecisionRequest(Model):
    """Optional body of ``POST /api/users/{user_id}/whisper/{card_id}/accept|decline``."""

    trip_id: TripId | None = Field(None, description="Trip whose cart the accept changes (default: the live trip).")
    channel: Channel = "web"
    note: str | None = None


class WhisperReminder(Model):
    """A WAIT accept: 'I'll text you Fri Oct 23'. Sent by the gateway through messaging on ``remind_on``."""

    reminder_id: str
    user_id: UserId
    card_id: str
    sku: Sku
    name: str
    store_id: StoreId
    remind_on: date
    price_cents: Cents | None = Field(None, description="Expected promo price on ``remind_on``.")
    was_cents: Cents | None = Field(None, description="Price today.")
    channel: Channel = "photon"
    message: str = Field(description="Confirmation shown now: 'I'll text you Fri Oct 23 when it drops to $59.99.'")
    status: Literal["scheduled", "sent", "cancelled"] = "scheduled"
    created_at: Timestamp = Field(default_factory=utcnow)


class WhisperDecisionResult(Model):
    """``POST /api/users/{user_id}/whisper/{card_id}/accept|decline`` response."""

    card_id: str
    decision: Literal["accept", "decline"]
    outcome: WhisperOutcome
    message: str = Field("", description="Shopper-facing line: 'Added Taco Seasoning Mix', 'I'll text you Fri Oct 23'.")
    card: WhisperCard | None = None
    cart: Cart | None = Field(None, description="The trip cart after the change (None without a trip).")
    mandate_decision: MandateDecision | None = None
    approval: ApprovalRequest | None = Field(None, description="Set when the accept raised an approval.")
    reminder: WhisperReminder | None = None
    profile_update: ProfileUpdate | None = Field(None, description="Decline / accept feedback applied to the profile.")


class ShopperContext(Model):
    """Everything the gateway knows about this shopper right now, for ``RecommenderService.whisper``. SERVER-SIDE
    ONLY: never serialized to a client or sent to an LLM (the LLM sees derived signals only). The recommender adds
    what it owns itself (profile, purchase history + cadence, owned items / devices, promo calendar, feedback) and
    reads stock / prices / deals from the catalog."""

    user_id: UserId
    store_id: StoreId
    phase: WhisperPhase = "planning"
    trip_id: TripId | None = None
    intent_text: str | None = Field(None, description="Trip.intent.text, e.g. 'tacos for 6, under $40'.")
    shopping_list: ShoppingList | None = None
    cart: Cart | None = Field(None, description="The trip cart now (or the projected cart: cart + unpicked list).")
    budget_cents: Cents | None = None
    mandate: IntentMandate | None = None
    remaining_cents: int | None = Field(None, description="min(cap, budget) - projected total (negative = over).")
    robot_pose: Pose2D | None = None
    remaining_stops: list[Stop] = Field(default_factory=list, description="Route stops not done / declined yet.")
    last_picked_sku: Sku | None = None
    pending_rec_skus: list[Sku] = Field(default_factory=list, description="Detour recs still on the route.")
    conversation: list[str] = Field(default_factory=list, description="Last few shopper lines this trip.")
    answered_card_ids: list[str] = Field(
        default_factory=list, description="Cards accepted or declined this trip (never shown again in this trip)."
    )
    now: Timestamp | None = Field(None, description="Clock override (tests / replays); None = now.")


def whisper_card_id(kind: str, sku: Sku | None, line_sku: Sku | None = None) -> str:
    """Stable card id: 'cw-skip-CHG-20W', 'cw-swap_save-SOYRIZO-for-BEEF-GROUND'."""
    base = f"cw-{kind}-{sku or line_sku}"
    return f"{base}-for-{line_sku}" if line_sku and sku and line_sku != sku else base


def whisper_transparency_entry(card: WhisperCard, outcome: str = "pending") -> TransparencyEntry:
    """The trust ledger row for a shown card (``TrustService.record_transparency`` upserts on ``rec_id``)."""
    rec_id = f"{card.card_id}@{card.trip_id}" if card.trip_id else f"{card.card_id}@{card.user_id}"
    return TransparencyEntry(
        entry_id=rec_id,
        user_id=card.user_id,
        trip_id=card.trip_id,
        rec_id=rec_id,
        sku=card.product_sku,
        store_id=card.store_id,
        kind="whisper",
        reason=card.reason,
        signals={
            "kind": card.kind,
            "action": card.action,
            "reason_codes": ",".join(card.reason_codes),
            **{s.name: s.value for s in card.signals},
        },
        interest=card.score,
        sponsored=card.sponsored,
        outcome=outcome,  # type: ignore[arg-type]
    )


__all__ = [
    "WHISPER_DEFAULT_ACTION",
    "WHISPER_EMPTY_REASON",
    "WHISPER_KINDS",
    "WHISPER_MAX_CARDS",
    "WHISPER_REASON_CODES",
    "ShopperContext",
    "WhisperAction",
    "WhisperCard",
    "WhisperDecisionRequest",
    "WhisperDecisionResult",
    "WhisperKind",
    "WhisperOutcome",
    "WhisperPhase",
    "WhisperReasonCode",
    "WhisperReminder",
    "WhisperResponse",
    "WhisperSignal",
    "WhisperSignalSource",
    "whisper_card_id",
    "whisper_transparency_entry",
]
