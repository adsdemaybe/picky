"""Schemas of the data files under data/ (the planogram is the single source of truth, CLAUDE.md §4 rule 5).

- data/products.yaml            -> ProductsFile
- data/planograms/<store_id>.yaml -> Planogram
- data/online_store.yaml        -> OnlineCatalog
- data/fixtures/users.yaml      -> FixturesFile (demo users)

Geometry: world frame, meters. x right, y up, origin (0, 0) at the store's bottom-left corner, theta CCW from +x.
Fixture footprints are axis-aligned rectangles; a fixture's ``yaw`` is the direction its pick face points (it does
NOT rotate the rectangle). Load these with utils.data, not by hand.

3D / sim rules (the Isaac scene is generated from these files):
- Sim world == store frame: USD stage Z-up, metersPerUnit = 1, USD (x, y) = store (x, y), origin at the store's
  bottom-left corner. Never re-center the scene; z = height above the floor.
- ``Slot.item_position`` = front-center of the front item's BASE on the shelf board: (x, y) on the pick-face edge of
  the fixture rect, z == ``fixture.level_z_m[slot.level]`` when set. Sim ``ItemInstance.position`` = item CENTER.
- Pick-face direction of a slot = ``slot.approach_pose.theta + pi`` (the robot faces the shelf). This also says which
  side of a ``double_sided`` fixture the slot is on. Items are rotated so their label (-y face) points that way.
- Every approach pose / entrance / checkout keeps ``contracts.common.ROBOT_RADIUS_M`` clearance from fixtures,
  obstacles and the store outline (``python -m utils.data`` checks it).
"""

from typing import Literal

from pydantic import Field, model_validator

from contracts.catalog import DealTerms, Lot, Product
from contracts.common import (
    AisleId,
    Cents,
    FixtureId,
    Model,
    Point3,
    Pose2D,
    Qty,
    Rect2D,
    Sku,
    SlotId,
    StoreId,
    UserId,
    ensure_unique,
)
from contracts.profile import Feedback, PurchaseRecord, UserProfile
from contracts.trust import IntentMandate

FixtureKind = Literal["shelf", "fridge", "freezer", "produce_bin", "endcap", "checkout"]


class Fixture(Model):
    fixture_id: FixtureId
    kind: FixtureKind = "shelf"
    label: str | None = None
    aisle_id: AisleId | None = None
    rect: Rect2D = Field(description="Axis-aligned footprint in the world frame (an obstacle for routing).")
    yaw: float = Field(
        0.0, description="Direction the pick face points, radians CCW from +x (0=+x, pi/2=+y, pi=-x, -pi/2=-y)."
    )
    double_sided: bool = Field(False, description="Products face both yaw and yaw+pi (gondola).")
    height_m: float = Field(1.8, gt=0)
    levels: int = Field(1, ge=1, description="Number of shelf levels; level 0 = bottom.")
    level_z_m: list[float] | None = Field(None, description="Height of each shelf board; len == levels.")

    @model_validator(mode="after")
    def _levels(self) -> "Fixture":
        if self.level_z_m is not None and len(self.level_z_m) != self.levels:
            raise ValueError(
                f"fixture {self.fixture_id}: level_z_m has {len(self.level_z_m)} entries, levels={self.levels}"
            )
        return self


class Aisle(Model):
    aisle_id: AisleId
    label: str = Field(description="Shopper-facing label: 'Aisle 3', 'Produce', 'Dairy wall'.")
    rect: Rect2D = Field(description="Walkable corridor footprint.")
    categories: list[str] = Field(default_factory=list)
    fixture_ids: list[FixtureId] = Field(default_factory=list)


class Slot(Model):
    """A facing on a fixture that holds one SKU."""

    slot_id: SlotId
    fixture_id: FixtureId
    aisle_id: AisleId | None = None
    sku: Sku
    level: int = Field(0, ge=0)
    index: int = Field(0, ge=0, description="Position along the fixture, 0 = left when facing it.")
    item_position: Point3 = Field(
        description="Front-center of the front item's base on the shelf board (world meters; z = level_z_m[level])."
    )
    approach_pose: Pose2D = Field(
        description="Robot base pose in the aisle, facing the slot (theta points at item_position; pick face = "
        "theta + pi). >= ROBOT_RADIUS_M from every fixture/obstacle."
    )
    facings: int = Field(1, ge=1)
    capacity: int = Field(12, ge=0)
    qty: Qty = Field(0, description="Units on the shelf now (0 = out of stock).")
    price_cents: Cents | None = Field(None, description="Store price override; None = product base price.")
    shelf_tag_cents: Cents | None = Field(None, description="What the physical tag shows if it differs (mispricing).")
    lots: list[Lot] = Field(default_factory=list, description="Sum of lot qty should equal qty when present.")
    deal: DealTerms | None = Field(
        None, description="Slot deal. Seed/catalog derive its deal_id as f'{store_id}:{slot_id}'; lot_id in lots."
    )


class Planogram(Model):
    """One physical store: layout, fixtures, aisles, slots and stock."""

    schema_version: int = 1
    store_id: StoreId
    name: str
    address: str | None = None
    bounds: Rect2D = Field(description="Store outline; normally x=0, y=0, w=width, h=depth.")
    entrance: Pose2D
    checkout: Pose2D = Field(description="Pose where the robot parks for checkout.")
    robot_home: Pose2D | None = Field(None, description="Robot spawn pose; defaults to the entrance.")
    obstacles: list[Rect2D] = Field(default_factory=list, description="Extra non-fixture obstacles (pillars).")
    fixtures: list[Fixture] = Field(default_factory=list)
    aisles: list[Aisle] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_ids(self) -> "Planogram":
        ensure_unique([f.fixture_id for f in self.fixtures], f"{self.store_id} fixture_id")
        ensure_unique([a.aisle_id for a in self.aisles], f"{self.store_id} aisle_id")
        ensure_unique([s.slot_id for s in self.slots], f"{self.store_id} slot_id")
        fixture_ids = {f.fixture_id for f in self.fixtures}
        aisle_ids = {a.aisle_id for a in self.aisles}
        for s in self.slots:
            if s.fixture_id not in fixture_ids:
                raise ValueError(f"slot {s.slot_id}: unknown fixture_id {s.fixture_id}")
            if s.aisle_id is not None and s.aisle_id not in aisle_ids:
                raise ValueError(f"slot {s.slot_id}: unknown aisle_id {s.aisle_id}")
        for f in self.fixtures:
            if f.aisle_id is not None and f.aisle_id not in aisle_ids:
                raise ValueError(f"fixture {f.fixture_id}: unknown aisle_id {f.aisle_id}")
        return self

    @property
    def start_pose(self) -> Pose2D:
        return self.robot_home or self.entrance

    def slot(self, slot_id: SlotId) -> Slot | None:
        return next((s for s in self.slots if s.slot_id == slot_id), None)

    def slots_for(self, sku: Sku) -> list[Slot]:
        return [s for s in self.slots if s.sku == sku]

    def fixture(self, fixture_id: FixtureId) -> Fixture | None:
        return next((f for f in self.fixtures if f.fixture_id == fixture_id), None)

    def aisle(self, aisle_id: AisleId) -> Aisle | None:
        return next((a for a in self.aisles if a.aisle_id == aisle_id), None)


class ProductsFile(Model):
    schema_version: int = 1
    products: list[Product]

    @model_validator(mode="after")
    def _unique(self) -> "ProductsFile":
        ensure_unique([p.sku for p in self.products], "sku")
        ensure_unique([p.barcode for p in self.products], "barcode")
        return self

    def by_sku(self) -> dict[Sku, Product]:
        return {p.sku: p for p in self.products}


class OnlineItem(Model):
    sku: Sku
    price_cents: Cents | None = Field(None, description="None = product base price.")
    qty: Qty = 100
    eta_hours: float | None = Field(None, description="Overrides the catalog default ETA.")
    deal: DealTerms | None = None


class OnlineCatalog(Model):
    """data/online_store.yaml: the mock online store (no planogram)."""

    schema_version: int = 1
    store_id: StoreId = "online"
    name: str = "Picky Online"
    shipping_cents: Cents = 599
    free_shipping_over_cents: Cents | None = 3500
    default_eta_hours: float = 24.0
    items: list[OnlineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "OnlineCatalog":
        ensure_unique([i.sku for i in self.items], "online sku")
        return self

    def item(self, sku: Sku) -> OnlineItem | None:
        return next((i for i in self.items if i.sku == sku), None)


class DemoUser(Model):
    user_id: UserId
    profile: UserProfile
    mandate: IntentMandate
    purchases: list[PurchaseRecord] = Field(default_factory=list)
    feedback: list[Feedback] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "DemoUser":
        owners = {self.profile.user_id, self.mandate.user_id}
        owners |= {p.user_id for p in self.purchases} | {f.user_id for f in self.feedback}
        if owners != {self.user_id}:
            raise ValueError(
                f"demo user {self.user_id}: nested records reference other users {owners - {self.user_id}}"
            )
        return self


class FixturesFile(Model):
    """data/fixtures/users.yaml: fictional demo users with profile, mandate and history."""

    schema_version: int = 1
    users: list[DemoUser]

    @model_validator(mode="after")
    def _unique(self) -> "FixturesFile":
        ensure_unique([u.user_id for u in self.users], "user_id")
        return self

    def user(self, user_id: UserId) -> DemoUser | None:
        return next((u for u in self.users if u.user_id == user_id), None)
