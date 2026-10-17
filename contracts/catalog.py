"""Catalog contracts: product master data, store locations, offers, deals, search, merchant dashboard.

Owned by the catalog arm (``CatalogService`` in contracts/services.py). ``data/products.yaml`` rows are ``Product``.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator

from contracts.common import (
    AisleId,
    Cents,
    DealId,
    FixtureId,
    LotId,
    Model,
    Point3,
    Pose2D,
    Qty,
    Rect2D,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    utcnow,
)

ShapeKind = Literal["box", "cylinder", "sphere"]
DealKind = Literal["markdown", "percent_off", "bogo", "near_expiry"]
StoreKind = Literal["physical", "online"]


class SimAsset(Model):
    """Hints the sim arm uses to build a product prop (textured primitive with a readable label).

    Item-local frame: x = width, y = depth, z = up. The label is printed on the item's -y face; on the shelf every item
    is rotated so its label (-y face) points out of the pick face, i.e. towards the slot's approach pose.
    """

    shape: ShapeKind = "box"
    dims_m: tuple[float, float, float] = Field(
        (0.08, 0.08, 0.15),
        description="Size in meters: box (x width, y depth, z height); cylinder/sphere (diameter, diameter, height). "
        "The label is on the -y face.",
    )
    label_color: str = Field("#cccccc", pattern=r"^#[0-9a-fA-F]{6}$", description="Label/body color, #RRGGBB.")
    label_text: str | None = Field(None, description="Text printed on the label; defaults to the product name.")
    mass_kg: float = Field(0.3, gt=0)
    texture: str | None = Field(None, description="Optional texture path relative to the data dir.")


class Product(Model):
    """Product master record (one row of data/products.yaml)."""

    sku: Sku = Field(min_length=1)
    name: str
    brand: str | None = None
    category: str = Field(
        description="Top-level category used by mandates: produce, dairy, meat, bakery, pantry, snacks, "
        "beverages, frozen, deli, household, alcohol, ..."
    )
    subcategory: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list, description="Lowercase tags: 'spicy', 'organic', 'taco', ...")
    dietary: list[str] = Field(default_factory=list, description="vegetarian, vegan, gluten_free, dairy_free, ...")
    allergens: list[str] = Field(default_factory=list)
    spice_level: int | None = Field(None, ge=0, le=5, description="0 = mild .. 5 = very hot; None if n/a.")
    price_cents: Cents = Field(description="Base list price per unit; stores/online may override.")
    unit: str = Field(
        "each",
        description="Selling unit: each | pack | bunch | bag ... qty is an integer count everywhere, so weighted goods "
        "are sold as fixed packs (unit='pack', size='1 lb'), never by weight.",
    )
    size: str | None = Field(None, description="Human size string, e.g. '16 oz'.")
    barcode: str = Field(pattern=r"^\d{8,14}$", description="EAN-13 (13 digits) preferred; UPC/EAN-8 accepted.")
    perishable: bool = False
    shelf_life_days: int | None = Field(None, ge=0)
    online_only: bool = False
    image_url: str | None = None
    asset: SimAsset = Field(default_factory=SimAsset)

    @field_validator("tags", "dietary", "allergens")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v]


def ean13_check_digit(first12: str) -> int:
    """Check digit for the first 12 digits of an EAN-13."""
    if len(first12) != 12 or not first12.isdigit():
        raise ValueError("need exactly 12 digits")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def is_valid_ean13(code: str) -> bool:
    return len(code) == 13 and code.isdigit() and ean13_check_digit(code[:12]) == int(code[12])


class Lot(Model):
    """A batch of units on a slot sharing an expiry date (and, for produce, a freshness estimate).

    Sim ``ItemInstance.lot_id`` points at the lot each unit belongs to, so scans can update freshness per lot.
    """

    lot_id: LotId
    qty: Qty
    expiry: date | None = None
    freshness: Score01 | None = Field(None, description="1.0 = perfectly fresh; produce only.")


class DealTerms(Model):
    """The commercial terms of a deal (embedded in planogram slots / online items without sku+store)."""

    kind: DealKind
    price_cents: Cents = Field(description="Effective unit price after the deal.")
    original_cents: Cents | None = Field(None, description="Unit price before the deal (None = list price).")
    percent_off: int | None = Field(None, ge=0, le=100)
    reason: str = Field("", description="Shopper-facing reason, e.g. 'Near expiry: best by Oct 19'.")
    valid_until: Timestamp | None = None
    lot_id: LotId | None = Field(None, description="Lot the deal applies to (near_expiry markdowns).")


class Deal(DealTerms):
    deal_id: DealId
    sku: Sku
    store_id: StoreId


class ProductLocation(Model):
    """Where a SKU physically sits in a store."""

    store_id: StoreId
    sku: Sku
    slot_id: SlotId
    aisle_id: AisleId | None = None
    aisle_label: str | None = None
    fixture_id: FixtureId
    level: int = Field(ge=0, description="Shelf level, 0 = bottom.")
    index: int = Field(ge=0, description="Position along the fixture, 0 = left when facing it.")
    item_position: Point3 = Field(
        description="Front-center of the front item's base on the shelf board (world meters; z = board height)."
    )
    approach_pose: Pose2D = Field(description="Robot base pose in the aisle, facing the slot.")
    qty: Qty = 0


class StoreOffer(Model):
    """One store's offer for a SKU (physical store or online)."""

    store_id: StoreId
    sku: Sku
    price_cents: Cents = Field(description="Effective unit price now (deal applied).")
    list_price_cents: Cents | None = None
    qty: Qty = 0
    location: ProductLocation | None = None
    lots: list[Lot] = Field(default_factory=list)
    deal: Deal | None = None
    eta_hours: float | None = Field(None, description="Online delivery ETA; None for in-store.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_stock(self) -> bool:
        return self.qty > 0


class StoreInfo(Model):
    store_id: StoreId
    name: str
    kind: StoreKind = "physical"
    address: str | None = None
    width_m: float | None = Field(None, description="Store extent along +x (planogram bounds.w).")
    depth_m: float | None = Field(None, description="Store extent along +y (planogram bounds.h).")
    has_planogram: bool = True
    shipping_cents: Cents | None = Field(None, description="kind='online': flat shipping per order.")
    free_shipping_over_cents: Cents | None = Field(None, description="kind='online': online subtotal for free ship.")
    default_eta_hours: float | None = Field(None, description="kind='online': delivery ETA.")


def online_shipping_cents(online_subtotal_cents: int, shipping_cents: int, free_shipping_over_cents: int | None) -> int:
    """THE shipping rule (deterministic; gateway uses it to set ``Cart.shipping_cents`` whenever the cart changes).

    0 when there are no online lines; free at or above ``free_shipping_over_cents``; else the flat ``shipping_cents``.
    """
    if online_subtotal_cents <= 0:
        return 0
    if free_shipping_over_cents is not None and online_subtotal_cents >= free_shipping_over_cents:
        return 0
    return shipping_cents


class SearchQuery(Model):
    """Catalog search. ``dietary``/``tags`` are AND filters (a product must carry all of them)."""

    text: str = ""
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list)
    store_ids: list[StoreId] | None = Field(None, description="None = all stores including 'online'.")
    in_stock_only: bool = False
    max_price_cents: Cents | None = None
    limit: int = Field(10, ge=1, le=100)


class SearchHit(Model):
    product: Product
    offers: list[StoreOffer] = Field(default_factory=list, description="Offers across ALL requested stores.")
    score: float = 0.0

    def offer_for(self, store_id: StoreId) -> StoreOffer | None:
        return next((o for o in self.offers if o.store_id == store_id), None)

    def cheapest_in_stock(self) -> StoreOffer | None:
        stocked = [o for o in self.offers if o.in_stock]
        return min(stocked, key=lambda o: o.price_cents) if stocked else None


class NearExpiryItem(Model):
    store_id: StoreId
    sku: Sku
    slot_id: SlotId | None = None
    lot: Lot
    days_left: int
    price_cents: Cents = Field(description="Current (possibly marked-down) unit price.")
    original_cents: Cents
    deal: Deal | None = None


class ScanApplyResult(Model):
    """What the catalog changed after applying a perception ScanReport."""

    store_id: StoreId
    slot_id: SlotId | None = None
    applied: bool = True
    qty_before: int | None = None
    qty_after: int | None = None
    out_of_stock: bool = False
    mispricing: bool = False
    misplaced_skus: list[Sku] = Field(default_factory=list)
    notes: str = ""


class OutOfStockItem(Model):
    sku: Sku
    name: str
    slot_id: SlotId | None = None
    aisle_label: str | None = None
    last_qty: int = 0
    last_scan_at: Timestamp | None = None


class Mispricing(Model):
    sku: Sku
    name: str
    slot_id: SlotId
    system_price_cents: Cents = Field(description="Price in the catalog/planogram (system of record).")
    shelf_tag_cents: Cents = Field(description="Price read off the shelf tag.")
    detected_at: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def delta_cents(self) -> int:
        return self.shelf_tag_cents - self.system_price_cents


class HeatmapCell(Model):
    """One cell of the merchant shelf heatmap (usually one per slot)."""

    cell_id: str
    rect: Rect2D
    slot_id: SlotId | None = None
    aisle_id: AisleId | None = None
    scans: int = 0
    picks: int = Field(0, description="Robot picks from this slot (CatalogService.record_pick).")
    oos_events: int = 0
    value: Annotated[float, Field(ge=0.0, le=1.0)] = Field(0.0, description="Normalized intensity for coloring.")


class MerchantDashboard(Model):
    store_id: StoreId
    generated_at: Timestamp = Field(default_factory=utcnow)
    scans_total: int = 0
    out_of_stock: list[OutOfStockItem] = Field(default_factory=list)
    mispricings: list[Mispricing] = Field(default_factory=list)
    near_expiry: list[NearExpiryItem] = Field(default_factory=list)
    heatmap: list[HeatmapCell] = Field(default_factory=list)
    kpis: dict[str, float] = Field(default_factory=dict, description="e.g. {'on_shelf_availability': 0.94}")
