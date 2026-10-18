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
