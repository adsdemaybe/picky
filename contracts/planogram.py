"""Schemas of the data files under data/ (the planogram is the single source of truth, CLAUDE.md §4 rule 5).

- data/products.yaml              -> ProductsFile
- data/planograms/<store_id>.yaml -> Planogram
- data/online_store.yaml          -> OnlineCatalog
- data/fixtures/users.yaml        -> FixturesFile (demo users)
- data/fixtures/robots.yaml       -> RobotsFile (fleet robots per store + store ceiling cameras)
- data/categories.yaml            -> CategoriesFile (category taxonomy; Product.category / subcategory ids)

Geometry: world frame, meters. x right, y up, origin (0, 0) at the store's bottom-left corner, theta CCW from +x.
Fixture footprints are axis-aligned rectangles; a fixture's ``yaw`` is the direction its pick face points (it does
NOT rotate the rectangle). Load these with utils.data, not by hand.

3D / sim rules (the Isaac scene is generated from these files):
- Stage layout (one Isaac stage, several stores): stage Z-up, metersPerUnit = 1. Each store is a
  ``/World/Stores/<store_id>`` Xform with a TRANSLATION ONLY (no rotation, no scale), stores spaced at least the
  largest store size + 20 m apart so cameras and physics never see a neighbor. That Xform's local frame IS the store
  frame (origin at the store's bottom-left corner, local (x, y) = store (x, y), z = height above the floor); never
  re-center a store inside it. Every pose / position on the wire (waypoints, RobotState.pose, ItemInstance.position,
  CameraMount with frame='world', CameraFrame.camera_pose) is STORE-LOCAL, never stage coordinates.
- Ids: ``slot_id`` is unique across ALL stores (convention: a store prefix, e.g. 'AVE-G1-E1-00' / 'HILL-G1-E1-00'),
  so sim instance ids (``contracts.sim.instance_id(slot_id, n)``) are globally unique too (``python -m utils.data``
  checks it). Docks of robots in one store are >= 2 x ROBOT_RADIUS_M + 0.1 m apart and >= ROBOT_RADIUS_M from every
  slot approach pose.
- ``Slot.item_position`` = front-center of the front item's BASE on the shelf board: (x, y) on the pick-face edge of
  the fixture rect, z == ``fixture.level_z_m[slot.level]`` when set. Sim ``ItemInstance.position`` = item CENTER.
- Pick-face direction of a slot = ``slot.approach_pose.theta + pi`` (the robot faces the shelf). This also says which
  side of a ``double_sided`` fixture the slot is on. Items are rotated so their label (-y face) points that way.
- Every approach pose / dock / entrance / checkout keeps ``contracts.common.ROBOT_RADIUS_M`` (0.3 m) clearance from
  fixtures, obstacles and the store outline (``python -m utils.data`` checks it).
- Reach (SO-101 rover, contracts.common): the horizontal distance approach pose -> ``item_position`` is designed to be
  0.35-0.40 m (``APPROACH_DIST_MIN_M``..``APPROACH_DIST_MAX_M``) and must be <= ``ARM_MOUNT_X_M + ARM_REACH_M``
  (0.47 m); ``item_position.z`` must be within ``REACH_Z_MIN_M``..``REACH_Z_MAX_M`` (0.1-1.5 m).

data/fixtures/robots.yaml (``RobotsFile``)::

    schema_version: 1
    robots:
      - {robot_id: rover-ave-1, name: Pico, kind: rover_so101, store_id: store_ave,
         dock: {x: 1.0, y: 2.2, theta: 0.0}, battery_pct: 95, basket_capacity: 12, color: "#2563eb"}
    store_cameras:
      - {camera_id: store_ave.ceiling-1, store_id: store_ave, name: Ave ceiling 1,
         mount: {frame: world, x: 3.5, y: 5.5, z: 3.2, yaw: 0.785, pitch: -0.9, hfov_deg: 100}}

data/categories.yaml (``CategoriesFile``)::

    schema_version: 1
    categories:
      - {category_id: dairy, name: Dairy & Eggs, icon: milk, color: "#60a5fa", sort: 2}
      - {category_id: salsa, name: Salsa, parent_id: international}
"""

import math
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from contracts.cameras import ROBOT_CAMERA_KINDS, CameraMount, RobotCameraKind
from contracts.catalog import DealTerms, Lot, Product
from contracts.common import (
    ROBOT_KIND,
    ROBOT_MAX_SPEED_MPS,
    AisleId,
    CameraId,
    CategoryId,
    Cents,
    FixtureId,
    Model,
    Point3,
    Pose2D,
    Qty,
    Rect2D,
    RobotId,
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
    low_threshold: int | None = Field(
        None, ge=0, description="qty <= this -> low stock; None = catalog.default_low_threshold(capacity)."
    )
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


# --------------------------------------------------------------------------------------------------------------------
# Checkout counter spots (0.2.4): where the arm puts each unit it unloads from the basket
# --------------------------------------------------------------------------------------------------------------------
COUNTER_SPOTS_PER_BAY = 3
"""Spots the arm reaches from one counter approach pose (left, center, right as seen from the robot)."""
COUNTER_SPOT_PITCH_M = 0.12
"""Lateral distance between two spots of one bay (the 0.47 m reach still covers the outer ones)."""
COUNTER_BAY_PITCH_M = 0.45
COUNTER_SPOT_DEPTH_M = 0.06
"""Spot center this far inside the counter's face edge (units stand on the counter, not on its edge)."""
COUNTER_APPROACH_M = 0.37
"""Robot base this far off the counter face (the shelf approach distance: >= ROBOT_RADIUS_M, reach <= 0.47 m)."""
COUNTER_EDGE_MARGIN_M = 0.2


class CounterSpot(Model):
    """One place on the checkout counter for an unloaded unit (0.2.4), derived from the planogram's checkout fixture by
    ``counter_spots`` (generated, never hand-written: CLAUDE.md section 4 rule 5)."""

    index: int = Field(ge=0, description="0 = first spot used (bay 0 at the checkout pose, left to right).")
    bay: int = Field(0, ge=0, description="Approach pose group: the robot shifts along the counter between bays.")
    fixture_id: FixtureId | None = Field(None, description="The checkout fixture (None = virtual counter).")
    position: Point3 = Field(description="Where the unit's BASE center lands (store frame; z = counter top).")
    approach_pose: Pose2D = Field(description="Robot base pose facing the counter for this spot (slot_reachable).")
    yaw: float = Field(0.0, description="Unit yaw on the counter (label toward the shopper side).")
    label: str = Field("", description="'Counter spot 3'.")


def _checkout_fixture(plan: "Planogram") -> Fixture | None:
    best: tuple[float, Fixture] | None = None
    for f in plan.fixtures:
        if f.kind != "checkout":
            continue
        cx, cy = f.rect.x + f.rect.w / 2, f.rect.y + f.rect.h / 2
        d = math.hypot(cx - plan.checkout.x, cy - plan.checkout.y)
        if best is None or d < best[0]:
            best = (d, f)
    return best[1] if best else None


def counter_spots(plan: "Planogram", count: int | None = None) -> list[CounterSpot]:
    """Deterministic counter spots for unloading at checkout: bays of ``COUNTER_SPOTS_PER_BAY`` along the checkout
    fixture's pick face, bay 0 straight ahead of ``plan.checkout``, then alternating right / left of it while they fit
    (``COUNTER_EDGE_MARGIN_M`` from the ends); spots of a bay left -> right as seen by the robot. Without a checkout
    fixture a virtual 2 m counter 0.43 m ahead of the checkout pose is used (height 0.9 m). ``count`` caps the list
    (None = every spot)."""
    f = _checkout_fixture(plan)
    co = plan.checkout
    if f is not None:
        face = f.yaw  # the pick face points toward the shopper / robot side
        fx, fy = math.cos(face), math.sin(face)
        r = f.rect
        if abs(fx) >= abs(fy):  # face along +-x: the edge is a vertical line, the counter runs along y
            edge = r.x + r.w if fx > 0 else r.x
            along = (0.0, 1.0)
            lo, hi = r.y, r.y + r.h
            origin_along = min(max(co.y, lo), hi)
            edge_pt = (edge, origin_along)
        else:
            edge = r.y + r.h if fy > 0 else r.y
            along = (1.0, 0.0)
            lo, hi = r.x, r.x + r.w
            origin_along = min(max(co.x, lo), hi)
            edge_pt = (origin_along, edge)
        top = f.height_m  # the counter is a solid block (scene_spec): units stand on its top
        fid: str | None = f.fixture_id
    else:  # virtual counter in front of the checkout pose
        face = co.theta + math.pi
        fx, fy = math.cos(face), math.sin(face)
        along = (-fy, fx)
        edge_pt = (co.x - fx * COUNTER_APPROACH_M, co.y - fy * COUNTER_APPROACH_M)
        lo, hi = -1.0, 1.0
        origin_along = 0.0
        top, fid = 0.9, None
    robot_theta = math.atan2(-fy, -fx)
    left = (math.cos(robot_theta + math.pi / 2), math.sin(robot_theta + math.pi / 2))
    # bay centers along the counter (in the 'along' coordinate), bay 0 at the checkout pose
    half = COUNTER_SPOT_PITCH_M * (COUNTER_SPOTS_PER_BAY - 1) / 2
    lo_ok, hi_ok = lo + COUNTER_EDGE_MARGIN_M + half, hi - COUNTER_EDGE_MARGIN_M - half
    if f is None:
        lo_ok, hi_ok = -0.9 + half, 0.9 - half
    start = min(max(origin_along, lo_ok), hi_ok) if lo_ok <= hi_ok else (lo + hi) / 2
    centers = [start]
    k = 1
    while k < 12:
        added = False
        for sign in (1, -1):
            c = start + sign * k * COUNTER_BAY_PITCH_M
            if lo_ok - 1e-9 <= c <= hi_ok + 1e-9:
                centers.append(c)
                added = True
        if not added:
            break
        k += 1
    out: list[CounterSpot] = []
    for bay, c in enumerate(centers):
        base = (
            (edge_pt[0] + along[0] * (c - origin_along), edge_pt[1] + along[1] * (c - origin_along))
            if f is not None
            else (edge_pt[0] + along[0] * c, edge_pt[1] + along[1] * c)
        )
        approach = Pose2D(
            x=round(base[0] + fx * COUNTER_APPROACH_M, 4),
            y=round(base[1] + fy * COUNTER_APPROACH_M, 4),
            theta=robot_theta,
        )
        offs = [half - i * COUNTER_SPOT_PITCH_M for i in range(COUNTER_SPOTS_PER_BAY)]  # +left first = left -> right
        for off in offs:
            px = base[0] - fx * COUNTER_SPOT_DEPTH_M + left[0] * off
            py = base[1] - fy * COUNTER_SPOT_DEPTH_M + left[1] * off
            n = len(out)
            out.append(
                CounterSpot(
                    index=n,
                    bay=bay,
                    fixture_id=fid,
                    position=Point3(x=round(px, 4), y=round(py, 4), z=round(top, 4)),
                    approach_pose=approach,
                    yaw=robot_theta + math.pi / 2,
                    label=f"Counter spot {n + 1}",
                )
            )
            if count is not None and len(out) >= count:
                return out
    return out


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


# --------------------------------------------------------------------------------------------------------------------
# Fleet robots + store cameras (data/fixtures/robots.yaml)
# --------------------------------------------------------------------------------------------------------------------
RobotKind = Literal["rover_so101"]
_ROBOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RobotSpec(Model):
    """One fleet robot (static config). Live state is ``contracts.sim.RobotState`` / ``contracts.fleet.RobotInfo``."""

    robot_id: RobotId = Field(description="Lowercase + hyphens, e.g. 'rover-ave-1' (no dots: camera ids use '.').")
    name: str = Field(description="Shopper-facing name, e.g. 'Pico'.")
    kind: RobotKind = ROBOT_KIND  # type: ignore[assignment]
    store_id: StoreId = Field(description="Physical store the robot works in.")
    dock: Pose2D = Field(description="Charging dock pose (spawn pose on reset; return_to_dock goal target).")
    battery_pct: float = Field(100.0, ge=0, le=100, description="Battery at boot / reset.")
    basket_capacity: int = Field(12, ge=1, description="Max units in the basket.")
    max_speed_mps: float = Field(ROBOT_MAX_SPEED_MPS, gt=0)
    cameras: list[RobotCameraKind] = Field(default_factory=lambda: list(ROBOT_CAMERA_KINDS))
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$", description="UI accent color.")
    notes: str = ""

    @field_validator("robot_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _ROBOT_ID_RE.match(v):
            raise ValueError(f"robot_id {v!r} must be lowercase letters/digits/hyphens (no dots)")
        return v


class StoreCameraSpec(Model):
    """A fixed store camera (ceiling). ``camera_id`` must be '<store_id>.ceiling-<n>'."""

    camera_id: CameraId
    store_id: StoreId
    name: str
    mount: CameraMount = Field(description="frame='world' (store frame, meters; z = height above the floor).")

    @model_validator(mode="after")
    def _id_matches(self) -> "StoreCameraSpec":
        if not re.fullmatch(rf"{re.escape(self.store_id)}\.ceiling-[1-9][0-9]*", self.camera_id):
            raise ValueError(f"store camera id {self.camera_id!r} must be '{self.store_id}.ceiling-<n>'")
        if self.mount.frame != "world":
            raise ValueError(f"store camera {self.camera_id}: mount.frame must be 'world'")
        return self


class RobotsFile(Model):
    """data/fixtures/robots.yaml: every fleet robot in every physical store, plus the stores' ceiling cameras."""

    schema_version: int = 1
    robots: list[RobotSpec] = Field(default_factory=list)
    store_cameras: list[StoreCameraSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "RobotsFile":
        ensure_unique([r.robot_id for r in self.robots], "robot_id")
        ensure_unique([c.camera_id for c in self.store_cameras], "store camera_id")
        return self

    def robot(self, robot_id: RobotId) -> RobotSpec | None:
        return next((r for r in self.robots if r.robot_id == robot_id), None)

    def robots_in(self, store_id: StoreId) -> list[RobotSpec]:
        return [r for r in self.robots if r.store_id == store_id]

    def cameras_in(self, store_id: StoreId) -> list[StoreCameraSpec]:
        return [c for c in self.store_cameras if c.store_id == store_id]


# --------------------------------------------------------------------------------------------------------------------
# Category taxonomy (data/categories.yaml)
# --------------------------------------------------------------------------------------------------------------------
_CATEGORY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class CategoryDef(Model):
    """One node of the taxonomy. Top-level nodes (parent_id None) are what ``Product.category`` holds; children are
    what ``Product.subcategory`` holds. Max depth 2 (category -> subcategory)."""

    category_id: CategoryId
    name: str
    parent_id: CategoryId | None = None
    description: str = ""
    icon: str | None = Field(None, description="Icon name hint for the web app, e.g. 'carrot'.")
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    aliases: list[str] = Field(default_factory=list, description="Words shoppers use ('mexican', 'dairy aisle').")
    sort: int = 0

    @field_validator("category_id", "parent_id")
    @classmethod
    def _id_shape(cls, v: str | None) -> str | None:
        if v is not None and not _CATEGORY_ID_RE.match(v):
            raise ValueError(f"category id {v!r} must be lowercase snake_case")
        return v

    @field_validator("aliases")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [a.strip().lower() for a in v]


class CategoriesFile(Model):
    schema_version: int = 1
    categories: list[CategoryDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tree(self) -> "CategoriesFile":
        ensure_unique([c.category_id for c in self.categories], "category_id")
        by_id = {c.category_id: c for c in self.categories}
        for c in self.categories:
            if c.parent_id is None:
                continue
            parent = by_id.get(c.parent_id)
            if parent is None:
                raise ValueError(f"category {c.category_id}: unknown parent_id {c.parent_id}")
            if parent.parent_id is not None:
                raise ValueError(f"category {c.category_id}: max depth is 2 (parent {c.parent_id} has a parent)")
        return self

    def top_level(self) -> list[CategoryDef]:
        return sorted((c for c in self.categories if c.parent_id is None), key=lambda c: (c.sort, c.category_id))

    def children(self, category_id: CategoryId) -> list[CategoryDef]:
        return sorted((c for c in self.categories if c.parent_id == category_id), key=lambda c: (c.sort, c.name))

    def get(self, category_id: CategoryId) -> CategoryDef | None:
        return next((c for c in self.categories if c.category_id == category_id), None)
