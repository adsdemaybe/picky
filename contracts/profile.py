"""Shopper profile contracts: preferences, purchase history, feedback, profile updates. Owned by the recommender."""

from typing import Literal

from pydantic import Field, field_validator

from contracts.common import (
    EMBED_DIM,
    Cents,
    Channel,
    FeedbackId,
    Model,
    PositiveQty,
    Rating,
    Sku,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)


class UserProfile(Model):
    """Everything the recommender knows about a shopper. Demo users are fictional (CLAUDE.md §8)."""

    user_id: UserId
    display_name: str
    phone: str | None = Field(None, description="E.164 phone / iMessage handle used by Photon messaging.")
    email: str | None = None
    home_store_id: StoreId = "store_ave"
    household_size: int = Field(1, ge=1)
    prefs: dict[str, float] = Field(
        default_factory=dict,
        description="Weighted likes keyed by tag/category/attribute (e.g. {'spicy': 0.8, 'organic': 0.3}); "
        "weights in [-1, 1], negative = dislike.",
    )
    dislikes: list[str] = Field(default_factory=list, description="Tags/categories to avoid.")
    disliked_skus: list[Sku] = Field(default_factory=list)
    favorite_skus: list[Sku] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list, description="The shopper's own diet: vegetarian, ...")
    allergies: list[str] = Field(default_factory=list)
    default_budget_cents: Cents | None = None
    taste_vector: list[float] | None = Field(None, description=f"Profile embedding, length {EMBED_DIM}.")
    version: int = Field(1, ge=1, description="Incremented on every ProfileUpdate.")
    updated_at: Timestamp = Field(default_factory=utcnow)

    @field_validator("prefs")
    @classmethod
    def _weights_in_range(cls, v: dict[str, float]) -> dict[str, float]:
        for key, w in v.items():
            if not -1.0 <= w <= 1.0:
                raise ValueError(f"pref weight for {key!r} must be in [-1, 1], got {w}")
        return {k.strip().lower(): w for k, w in v.items()}

    @field_validator("taste_vector")
    @classmethod
    def _dim(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != EMBED_DIM:
            raise ValueError(f"taste_vector must have length {EMBED_DIM}, got {len(v)}")
        return v


class PurchaseRecord(Model):
    purchase_id: str
    user_id: UserId
    sku: Sku
    qty: PositiveQty = 1
    unit_price_cents: Cents
    store_id: StoreId
    trip_id: TripId | None = None
    ts: Timestamp = Field(default_factory=utcnow)
    rating: Rating | None = None


class Feedback(Model):
    """Post-purchase feedback ('how was the salsa?' -> 'too mild, 3/5')."""

    feedback_id: FeedbackId
    user_id: UserId
    sku: Sku | None = None
    trip_id: TripId | None = None
    rating: Rating | None = None
    text: str = ""
    channel: Channel = "web"
    tags: list[str] = Field(default_factory=list, description="Extracted tags, e.g. ['too_mild', 'want_spicier'].")
    ts: Timestamp = Field(default_factory=utcnow)


class ProfileChange(Model):
    field: str = Field(description="UserProfile field changed: prefs, favorite_skus, dislikes, ...")
    key: str | None = Field(None, description="Pref key or sku the change applies to.")
    old: float | str | None = None
    new: float | str | None = None
    reason: str = ""


class ProfileUpdate(Model):
    """Diff applied to a profile; drives the 'what changed since last time' panel."""

    user_id: UserId
    trip_id: TripId | None = None
    source: Literal["feedback", "purchase", "explicit", "inferred"] = "feedback"
    changes: list[ProfileChange] = Field(default_factory=list)
    summary: str = Field("", description="One shopper-facing sentence, e.g. 'You like it hotter: spicier picks next.'")
    version_before: int = 1
    version_after: int = 1
    ts: Timestamp = Field(default_factory=utcnow)


# --------------------------------------------------------------------------------------------------------------------
# Replenish suggestions (0.2.3): "you bought this 12 days ago" cards for the web app
# --------------------------------------------------------------------------------------------------------------------
class ReplenishSuggestion(Model):
    """One card of GET /api/users/{user_id}/suggestions (0.2.3): an item the shopper probably needs again, from their
    purchase cadence (the Cart Whisperer "replenish" generator, recommender arm). Deterministic: the list is sorted by
    ``overdue_days`` (most overdue first), then SKU. Clicking cards and ordering them = POST /api/requests with
    ``items`` (``fleet.RequestItem``). Never sponsored (CLAUDE.md section 8)."""

    suggestion_id: str = Field(description="Stable id: 'rep-<user_id>-<sku>'.")
    kind: Literal["replenish"] = "replenish"
    user_id: UserId
    sku: Sku
    name: str
    brand: str | None = None
    size: str | None = None
    category: str | None = None
    image_url: str = Field(description="Root-relative product thumbnail: '/api/products/{sku}/image.jpg'.")
    label_color: str | None = Field(None, description="#RRGGBB of the sim prop's label (card accent).")
    price_cents: Cents = Field(description="Unit price at ``store_id`` now (deal applied).")
    store_id: StoreId
    in_stock: bool
    stock_qty: int = Field(0, ge=0)
    aisle_label: str | None = Field(None, description="'Aisle 3 · shelf 2' (None when not stocked here).")
    slot_id: str | None = None
    shelf_level: int | None = Field(None, ge=0, description="0 = bottom shelf.")
    last_bought_at: Timestamp
    last_bought_days: int = Field(ge=0, description="Whole days since the last purchase.")
    cadence_days: int = Field(ge=1, description="Typical days between purchases (own history).")
    overdue_days: int = Field(description="last_bought_days - cadence_days (negative = due in that many days).")
    times_bought: int = Field(ge=1)
    suggested_qty: int = Field(1, ge=1, description="Usual quantity per purchase.")
    reason: str = Field(description="'You bought this 12 days ago — you usually buy it every 10 days.'")
    signals: dict[str, float | int | str] = Field(default_factory=dict, description="Why, as numbers (transparency).")
    sponsored: bool = False
