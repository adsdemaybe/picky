"""Shopping contracts: intent, shopping list, recommendations, cart, trip state.

``Trip`` lives in contracts/trip.py (it aggregates routing + trust models); it is also importable from here:
``from contracts.shopping import Trip`` works via a lazy module attribute.

Money naming: ``*_price_cents`` / ``price_cents`` = UNIT price; ``*_line_cents`` / ``total_cents`` = qty x unit price.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, computed_field, model_validator

from contracts.catalog import Deal
from contracts.common import (
    ONLINE_STORE_ID,
    Cents,
    Channel,
    InstanceId,
    ItemId,
    JsonScalar,
    LineId,
    ListId,
    Model,
    PositiveQty,
    RecId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)

Sourcing = Literal["in_store", "online", "unresolved"]
LineSource = Literal["in_store", "online"]
LineStatus = Literal["planned", "picked", "verified", "declined"]
RecKind = Literal["detour", "checkout_batch", "near_expiry_offer"]


class TripState(StrEnum):
    INTENT = "intent"
    LIST_READY = "list_ready"
    RECS_READY = "recs_ready"
    ROUTE_PLANNED = "route_planned"
    AWAITING_LOGIN = "awaiting_login"
    EXECUTING = "executing"
    AWAITING_APPROVAL = "awaiting_approval"
    CHECKOUT = "checkout"
    PAID = "paid"
    FOLLOWUP = "followup"
    DONE = "done"
    FAILED = "failed"


_S = TripState
TRIP_TRANSITIONS: dict[TripState, frozenset[TripState]] = {
    _S.INTENT: frozenset({_S.LIST_READY}),
    # pre-trip edits ("add limes" by text before login) go back to LIST_READY and re-run recs + routing
    _S.LIST_READY: frozenset({_S.LIST_READY, _S.RECS_READY, _S.ROUTE_PLANNED}),
    _S.RECS_READY: frozenset({_S.LIST_READY, _S.RECS_READY, _S.ROUTE_PLANNED}),
    _S.ROUTE_PLANNED: frozenset({_S.LIST_READY, _S.AWAITING_LOGIN, _S.EXECUTING, _S.ROUTE_PLANNED}),
    _S.AWAITING_LOGIN: frozenset({_S.LIST_READY, _S.ROUTE_PLANNED, _S.EXECUTING}),
    _S.EXECUTING: frozenset({_S.AWAITING_APPROVAL, _S.CHECKOUT, _S.ROUTE_PLANNED}),
    _S.AWAITING_APPROVAL: frozenset({_S.EXECUTING, _S.CHECKOUT}),
    _S.CHECKOUT: frozenset({_S.AWAITING_APPROVAL, _S.PAID, _S.EXECUTING}),
    _S.PAID: frozenset({_S.FOLLOWUP, _S.DONE}),
    _S.FOLLOWUP: frozenset({_S.DONE}),
    _S.DONE: frozenset(),
    _S.FAILED: frozenset(),
}
"""Allowed trip transitions. Any non-terminal state may also go to FAILED."""


def can_transition(src: TripState, dst: TripState) -> bool:
    if dst == TripState.FAILED:
        return src not in (TripState.DONE, TripState.FAILED)
    return dst in TRIP_TRANSITIONS[src]


class Intent(Model):
    """Raw shopper intent, e.g. 'tacos for 6, under $40, one vegetarian'."""

    intent_id: str
    user_id: UserId
    text: str
    channel: Channel = "web"
    store_id: StoreId | None = Field(None, description="None = user's home store / PICKY_DEFAULT_STORE.")
    trip_id: TripId | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class ListItem(Model):
    item_id: ItemId
    query: str = Field(description="What the shopper needs, e.g. 'tortillas'.")
    sku: Sku | None = Field(None, description="Resolved SKU (set by AgentService.source_list); None = unresolved.")
    qty: PositiveQty = Field(1, description="Integer count of selling units (Product.unit), never a weight.")
    substitutes: list[Sku] = Field(default_factory=list, description="Acceptable substitutes in preference order.")
    required: bool = True
    sourcing: Sourcing = "unresolved"
    store_id: StoreId | None = Field(None, description="Store it will come from ('online' for online orders).")
    category: str | None = None
    est_line_cents: Cents | None = Field(None, description="Estimated LINE total (qty x unit price) at store_id.")
    for_user_id: UserId | None = Field(None, description="Batched trips: whose list this item belongs to.")
    dietary: list[str] = Field(default_factory=list, description="Constraints this item must satisfy.")
    notes: str = Field("", description="Why: 'OOS at store_ave -> online', 'vegetarian option', ...")


class ShoppingList(Model):
    list_id: ListId
    user_id: UserId
    trip_id: TripId | None = None
    store_id: StoreId | None = Field(None, description="Store the list was sourced against (None = not sourced).")
    title: str | None = Field(None, description="e.g. 'Tacos for 6'.")
    servings: int | None = Field(None, ge=1)
    budget_cents: Cents | None = Field(None, description="Stated trip budget ('under $40'); trust enforces it.")
    dietary_notes: list[str] = Field(default_factory=list, description="e.g. ['1 vegetarian guest'].")
    items: list[ListItem] = Field(default_factory=list)
    intent_id: str | None = None

    def by_sourcing(self, sourcing: Sourcing) -> list[ListItem]:
        return [i for i in self.items if i.sourcing == sourcing]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def est_total_cents(self) -> int:
        return sum(i.est_line_cents or 0 for i in self.items)


class Recommendation(Model):
    """A suggested extra item. Always explainable; sponsored placement must be labeled (CLAUDE.md §8)."""

    rec_id: RecId
    user_id: UserId
    trip_id: TripId | None = Field(None, description="Copied from shopping_list.trip_id / cart.trip_id.")
    sku: Sku
    store_id: StoreId
    product_name: str | None = None
    interest: Score01 = Field(description="Predicted interest; also the routing skip penalty weight.")
    reason: str = Field(min_length=1, description="Shopper-facing 'why you'd like this'.")
    signals: dict[str, JsonScalar] = Field(
        default_factory=dict,
        description="Inputs behind the score, e.g. {'similarity': 0.82, 'past_rating': 5, 'discount_pct': 30}.",
    )
    kind: RecKind = "detour"
    sponsored: bool = False
    slot_id: SlotId | None = None
    price_cents: Cents | None = Field(None, description="UNIT price now (deal applied).")
    deal: Deal | None = None
    extra_distance_m: float | None = Field(
        None, description="Detour cost; the gateway copies it from the routed Stop.detour_m."
    )
    extra_time_s: float | None = Field(None, description="Detour time; copied from Stop.detour_s.")


class CartLine(Model):
    line_id: LineId
    sku: Sku
    name: str | None = None
    category: str = Field("uncategorized", description="Product category; mandates check it.")
    qty: PositiveQty = 1
    unit_price_cents: Cents
    source: LineSource = "in_store"
    store_id: StoreId = Field(description="'online' iff source='online'.")
    slot_id: SlotId | None = Field(None, description="In-store: slot it was (or will be) picked from.")
    status: LineStatus = "planned"
    rec_id: RecId | None = None
    deal_id: str | None = None
    list_item_id: ItemId | None = None
    user_id: UserId | None = Field(None, description="Batched trips: whose item this is.")
    instance_ids: list[InstanceId] = Field(default_factory=list, description="Sim instances picked for this line.")

    @model_validator(mode="after")
    def _source_matches_store(self) -> "CartLine":
        if (self.source == "online") != (self.store_id == ONLINE_STORE_ID):
            raise ValueError(f"line {self.line_id}: source {self.source!r} does not match store_id {self.store_id!r}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cents(self) -> int:
        return self.qty * self.unit_price_cents

    @property
    def active(self) -> bool:
        return self.status != "declined"


class Cart(Model):
    """The trip cart (basket + online order). Totals are computed fields (present in JSON, ignored on input).

    ``shipping_cents`` is set by the gateway with ``contracts.catalog.online_shipping_cents`` on every change.
    """

    cart_id: str
    trip_id: TripId | None = None
    user_id: UserId | None = None
    currency: Literal["USD"] = "USD"
    lines: list[CartLine] = Field(default_factory=list)
    shipping_cents: Cents = Field(0, description="Online-order shipping, included in total_cents.")

    def active_lines(self) -> list[CartLine]:
        return [line for line in self.lines if line.active]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def subtotal_cents(self) -> int:
        return sum(line.total_cents for line in self.active_lines())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_store_cents(self) -> int:
        return sum(line.total_cents for line in self.active_lines() if line.source == "in_store")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def online_cents(self) -> int:
        return sum(line.total_cents for line in self.active_lines() if line.source == "online")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + (self.shipping_cents if self.online_cents else 0)

    def line(self, line_id: LineId) -> CartLine | None:
        return next((line for line in self.lines if line.line_id == line_id), None)


def __getattr__(name: str) -> Any:  # lazy re-export to avoid an import cycle (trip -> trust -> shopping)
    if name == "Trip":
        from contracts.trip import Trip

        return Trip
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
