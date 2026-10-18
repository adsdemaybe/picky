"""Catalog contracts: product master data, store locations, offers, deals, search, merchant dashboard, the category
taxonomy and store STOCK MANAGEMENT ("resource management": levels, low/out alerts, restock, adjustments, movement
history; fed by robot scans and picks).

Owned by the catalog arm (``CatalogService`` in contracts/services.py). ``data/products.yaml`` rows are ``Product``;
``data/categories.yaml`` is ``contracts.planogram.CategoriesFile``.

Stock rules (deterministic; fake and real catalog agree):
- Stock is tracked per slot (``Slot.qty``); ``StockLevel`` aggregates a SKU over all its slots in one store.
- ``stock_status(qty, low_threshold)``: ``out`` when qty <= 0, ``low`` when qty <= low_threshold, else ``ok``.
  ``low_threshold`` = sum over the SKU's slots of ``Slot.low_threshold`` (default ``default_low_threshold(capacity)``).
- Every change appends a ``StockMovement`` (append-only history) with a ``reason`` (pick | scan | restock | damage |
  manual) and an ``actor`` string: ``"robot:<robot_id>"``, ``"staff:<name>"``, ``"user:<user_id>"`` or ``"system"``.
  ``apply_scan`` / ``record_pick`` record movements too (reason 'scan' / 'pick').
- A change that moves a SKU into ``low``/``out`` raises a ``StockAlert`` (one active alert per store+sku); moving back
  to ``ok`` clears it (``active=False``). The catalog publishes ``stock.updated`` / ``stock.alert`` for every change.
- Lots: ``sum(lot.qty) == Slot.qty`` always holds after a change. A pick decrements the picked unit's lot
  (``record_pick(lot_id=...)``, from ``ItemInstance.lot_id``), else the EARLIEST-expiry lot first. An exact scan sets
  each lot to ``ScanReport.lot_counts`` when given, else removes units from the earliest-expiry lots first (or adds
  the difference to the latest lot). Restocks add a new lot.
- Scans: ``count_is_exact`` -> qty = observed_count. Not exact (real CV sees front facings only): observed_count == 0
  -> qty 0; observed_count > 0 -> qty = max(qty, observed_count) (an OOS slot whose units are back is no longer 0).
- Restock: ``qty=None`` fills the SKU's slots to capacity; an explicit ``qty`` fills ``slot_id`` (or the SKU's slots
  in planogram order) up to capacity, and a ``qty`` larger than the total free capacity is REJECTED as a whole
  (``ValueError`` -> HTTP 409, nothing applied).
- Adjust: ``set_qty`` with ``slot_id=None`` is allowed only for a single-slot SKU (else ``ValueError`` -> 409);
  ``delta`` with ``slot_id=None`` hits the SKU's first slot (negative deltas spill to the next slots); qty never < 0
  and never > capacity (excess -> 409). The staff endpoint accepts only ``STAFF_STOCK_REASONS`` (else 422).
- Sim sync: after committing a restock / adjustment the catalog calls ``services.robot.sync_stock(store_id,
  slot_ids)`` best-effort (if wired; failures are logged, never raised) so the Isaac / 2D-fake shelves match and the
  next exact robot scan does not undo the change. Scans and picks never sync (they come from the sim).
"""

import math
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator

from contracts.common import (
    AisleId,
    AlertId,
    CategoryId,
    Cents,
    DealId,
    FixtureId,
    LotId,
    Model,
    MovementId,
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
    category: CategoryId = Field(
        description="Top-level category id from data/categories.yaml (used by mandates and the categories API): "
        "produce, dairy, meat, deli, bakery, pantry, international, snacks, beverages, frozen, household, "
        "personal_care, alcohol (STANDARD_CATEGORY_IDS)."
    )
    subcategory: str | None = Field(
        None, description="Child category id under ``category`` in data/categories.yaml (e.g. 'salsa'), if any."
    )
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


# --------------------------------------------------------------------------------------------------------------------
# Category taxonomy (data/categories.yaml -> contracts.planogram.CategoriesFile)
# --------------------------------------------------------------------------------------------------------------------
STANDARD_CATEGORY_IDS: tuple[CategoryId, ...] = (
    "produce",
    "dairy",
    "meat",
    "deli",
    "bakery",
    "pantry",
    "international",
    "snacks",
    "beverages",
    "frozen",
    "household",
    "personal_care",
    "alcohol",
)
"""Top-level category ids every arm may rely on (mandates block e.g. 'alcohol'; the freshness flow keys off
'produce'). Display names live in data/categories.yaml ('dairy' = 'Dairy & Eggs', 'meat' = 'Meat & Seafood',
'international' = 'International & Mexican', ...)."""


class CategoryInfo(Model):
    """One category with live counts (GET /api/categories[?store_id]). Flat list: top-level categories have
    ``parent_id=None``; subcategories point at their parent. Counts cover ``store_id`` (None = all physical stores)."""

    category_id: CategoryId
    name: str = Field(description="Display name, e.g. 'Dairy & Eggs'.")
    parent_id: CategoryId | None = None
    description: str = ""
    icon: str | None = Field(None, description="Icon name hint for the web app, e.g. 'carrot' (not an emoji).")
    color: str | None = Field(None, description="#RRGGBB accent for the web app.")
    sort: int = 0
    store_id: StoreId | None = Field(None, description="Scope of the counts; None = every physical store.")
    aisle_labels: list[str] = Field(default_factory=list, description="Where it is in that store ('Aisle 3').")
    product_count: int = Field(0, ge=0, description="Products in this category (incl. subcategories).")
    in_stock_count: int = Field(0, ge=0, description="Of those, products with qty > 0 in scope.")
    low_stock_count: int = Field(0, ge=0)
    out_of_stock_count: int = Field(0, ge=0)


# --------------------------------------------------------------------------------------------------------------------
# Stock management
# --------------------------------------------------------------------------------------------------------------------
StockStatus = Literal["ok", "low", "out"]
StockReason = Literal["pick", "scan", "restock", "damage", "manual"]
"""Why stock changed: robot pick, robot/perception scan, staff restock, damage/shrink write-off, manual correction."""
STAFF_STOCK_REASONS: tuple[StockReason, ...] = ("damage", "manual")
"""Reasons POST /api/stores/{id}/stock/adjust accepts (gateway answers 422 for 'pick' / 'scan' / 'restock': those
come from robots and the restock endpoint only)."""

LOW_STOCK_FRACTION = 0.25
"""Default low-stock threshold = this fraction of slot capacity (rounded up, at least 1)."""


def default_low_threshold(capacity: int) -> int:
    """Low-stock threshold for a slot without an explicit ``Slot.low_threshold``: ceil(25% of capacity), >= 1."""
    return max(1, math.ceil(max(capacity, 0) * LOW_STOCK_FRACTION))


def stock_status(qty: int, low_threshold: int) -> StockStatus:
    """THE stock status rule: out (qty <= 0) / low (qty <= low_threshold) / ok."""
    if qty <= 0:
        return "out"
    if qty <= low_threshold:
        return "low"
    return "ok"


class StockLevel(Model):
    """Current stock of one SKU in one store, aggregated over its slots (GET /api/stores/{id}/stock)."""

    store_id: StoreId
    sku: Sku
    name: str
    brand: str | None = None
    category: CategoryId
    subcategory: str | None = None
    unit: str = Field("each", description="Product.unit (display).")
    size: str | None = Field(None, description="Product.size, e.g. '16 oz' (display).")
    image_url: str | None = Field(None, description="Product.image_url (display).")
    qty: int = Field(ge=0, description="Units on the shelf now (sum over slots).")
    capacity: int = Field(ge=0, description="Max units (sum of Slot.capacity).")
    low_threshold: int = Field(ge=0, description="qty <= this -> 'low' (sum of the slots' thresholds).")
    slot_ids: list[SlotId] = Field(default_factory=list)
    aisle_label: str | None = Field(None, description="Where the first slot is, e.g. 'Aisle 3'.")
    price_cents: Cents | None = Field(None, description="Effective unit price in this store (deal applied).")
    near_expiry_qty: int = Field(0, ge=0, description="Units in lots expiring within 3 days.")
    last_scan_at: Timestamp | None = None
    last_movement_at: Timestamp | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> StockStatus:
        return stock_status(self.qty, self.low_threshold)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fill_ratio(self) -> float:
        return round(self.qty / self.capacity, 3) if self.capacity > 0 else 0.0


class StockAlert(Model):
    """A SKU that went low / out in a store (GET /api/stores/{id}/stock/alerts lists the active ones)."""

    alert_id: AlertId = Field(description="Stable per store+sku: f'{store_id}:{sku}'.")
    store_id: StoreId
    sku: Sku
    name: str
    category: CategoryId | None = None
    unit: str = Field("each", description="Product.unit (display).")
    size: str | None = Field(None, description="Product.size (display).")
    image_url: str | None = Field(None, description="Product.image_url (display).")
    status: Literal["low", "out"]
    qty: int = Field(ge=0)
    low_threshold: int = Field(ge=0)
    capacity: int = Field(ge=0)
    slot_ids: list[SlotId] = Field(default_factory=list)
    aisle_label: str | None = None
    suggested_restock_qty: int = Field(0, ge=0, description="capacity - qty.")
    message: str = Field("", description="Staff-facing, e.g. 'Casa Verde Mild Salsa is out of stock (Aisle 3)'.")
    trigger: StockReason | None = Field(None, description="What caused it (a robot scan, a pick, ...).")
    actor: str | None = None
    active: bool = Field(True, description="False once restocked back to 'ok' (sent once as a clear).")
    raised_at: Timestamp = Field(default_factory=utcnow)
    cleared_at: Timestamp | None = None


class RestockRequest(Model):
    """POST /api/stores/{store_id}/stock/restock: staff put units back on the shelf."""

    sku: Sku
    qty: int | None = Field(None, ge=1, description="Units added; None = fill every slot of the SKU to capacity.")
    slot_id: SlotId | None = Field(None, description="Restock this slot only; None = the SKU's slots in order.")
    lot_id: LotId | None = Field(None, description="New lot id; None = catalog mints one.")
    expiry: date | None = Field(None, description="Best-by of the new units (perishables).")
    actor: str = Field("staff", description="'staff:<name>' (web staff view) or 'system'.")
    note: str = ""


class StockAdjustment(Model):
    """POST /api/stores/{store_id}/stock/adjust, and the internal unit of every stock change.

    Exactly one of ``delta`` (signed units) or ``set_qty`` (absolute count, e.g. a scan) is given.
    """

    sku: Sku
    delta: int | None = Field(None, description="Signed change in units (non-zero), e.g. -2 for damage.")
    set_qty: int | None = Field(None, ge=0, description="Absolute count (scans, cycle counts).")
    reason: StockReason = Field("manual", description="Staff endpoint: only STAFF_STOCK_REASONS (else 422).")
    actor: str = Field("staff", description="'robot:<robot_id>' | 'staff:<name>' | 'user:<user_id>' | 'system'.")
    slot_id: SlotId | None = Field(
        None, description="Slot affected; None = the SKU's first slot (delta) / its only slot (set_qty; 409 if many)."
    )
    lot_id: LotId | None = None
    ref_id: str | None = Field(None, description="What caused it: scan_id, goal_id, trip_id, ...")
    note: str = ""

    @model_validator(mode="after")
    def _one_of(self) -> "StockAdjustment":
        if (self.delta is None) == (self.set_qty is None):
            raise ValueError("give exactly one of delta or set_qty")
        if self.delta == 0:
            raise ValueError("delta must be non-zero")
        return self


class StockMovement(Model):
    """One row of the append-only stock history (GET /api/stores/{id}/stock/history[?sku])."""

    movement_id: MovementId
    store_id: StoreId
    sku: Sku
    slot_id: SlotId | None = None
    lot_id: LotId | None = None
    delta: int = Field(description="Signed units actually applied.")
    qty_before: int = Field(ge=0, description="SKU qty in the store before (sum over slots).")
    qty_after: int = Field(ge=0)
    reason: StockReason
    actor: str
    ref_id: str | None = None
    note: str = ""
    ts: Timestamp = Field(default_factory=utcnow)


class StockChangeResult(Model):
    """Response of restock / adjust_stock: what was applied and where the SKU stands now."""

    store_id: StoreId
    sku: Sku
    movements: list[StockMovement] = Field(default_factory=list, description="One per slot touched.")
    level: StockLevel
    alert: StockAlert | None = Field(None, description="The SKU's alert after the change (active or just cleared).")
