"""Planogram + products -> a flat list of primitive specs the Isaac scene builder (``arms/sim/app.py``) consumes.

PURE python (pydantic only): no Isaac, no pxr, no I/O. It is the single translation from the data files to 3D, so it is
unit-tested on a laptop and the GPU box only has to turn each ``PrimSpec`` into one USD prim.

Frame (contracts/planogram.py): sim world == store frame. Z-up, meters, (x, y) = store (x, y), origin at the store's
bottom-left corner. ``PrimSpec.center`` is the CENTER of the primitive; ``size`` is its full extent along its local
x / y / z axes before ``yaw`` (radians about +z) is applied.

Products: ``Product.asset`` gives shape + dims (item-local: x = width, y = depth, z = up; label on the -y face). Each
unit on a shelf is rotated so its label faces out of the pick face (pick-face direction = ``approach_pose.theta + pi``),
i.e. item yaw = pick_dir + pi/2. ``Slot.item_position`` is the front-center of the front item's BASE, so a unit's
center is pushed back into the fixture by depth/2 and up by height/2. Units are laid out ``facings`` wide, then rows
deep, then stacked layers, and assigned to the slot's lots in file order (front units = first lot).

Usage: ``python -m arms.sim.scene_spec [store_id]`` prints a summary of the scene for the data in PICKY_DATA_DIR.
"""

import math
import re
import sys
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.catalog import Product
from contracts.common import ItemId, Point3, Pose2D, Rect2D, Sku
from contracts.planogram import Fixture, Planogram, Slot
from contracts.sim import BASKET_OFFSET_LOCAL, UNIT_FRESHNESS_JITTER, ItemInstance, planogram_hash, unit_freshness
from contracts.sim import BASKET_SIZE_M as _BASKET_SIZE_M
from contracts.sim import instance_id as _instance_id

PrimRole = Literal["floor", "wall", "obstacle", "board", "panel", "base", "product", "marker"]
PrimShape = Literal["box", "cylinder", "sphere"]
Vec3 = tuple[float, float, float]

# Geometry constants (meters)
BOARD_THICKNESS_M = 0.03
PANEL_THICKNESS_M = 0.03
WALL_HEIGHT_M = 2.4
WALL_THICKNESS_M = 0.1
FLOOR_THICKNESS_M = 0.05
OBSTACLE_HEIGHT_M = 3.0
ITEM_GAP_M = 0.01
MAX_UNITS_PER_SLOT = 24
MARKER_SIZE_M = 0.8
FRESHNESS_JITTER = UNIT_FRESHNESS_JITTER
"""Per-unit freshness = lot freshness +- this (``contracts.sim.unit_freshness``, deterministic per instance id and
shared with the robot fake, so a mid-trip fallback keeps the produce top-3 ranking)."""

# Robot basket (robot-local frame: x forward, y left, z up; meters). Fixed drop pose (CLAUDE.md §3 sim), shared with the
# robot arm through contracts.sim.
BASKET_OFFSET_M: Vec3 = BASKET_OFFSET_LOCAL
"""Center of the basket floor relative to the robot base pose (behind the arm, on the deck)."""
BASKET_SIZE_M: Vec3 = _BASKET_SIZE_M
GRIPPER_OFFSET_M: Vec3 = (0.45, 0.0, 1.0)
"""Where a held item sits relative to the robot base (arm extended forward)."""

FIXTURE_COLORS: dict[str, str] = {
    "shelf": "#b8bcc4",
    "fridge": "#dfe9f2",
    "freezer": "#cfe0f5",
    "produce_bin": "#8b6a43",
    "endcap": "#c9b37e",
    "checkout": "#3a3f4b",
}
FLOOR_COLOR = "#e9e4da"
WALL_COLOR = "#f4f1ea"
OBSTACLE_COLOR = "#9a9a9a"
ENTRANCE_COLOR = "#3fb27f"
CHECKOUT_COLOR = "#f2a33a"


class SceneSpecError(ValueError):
    """The planogram cannot be turned into a scene (e.g. empty / inconsistent)."""


class PrimSpec(BaseModel):
    """One primitive in the scene. The builder creates exactly one USD prim per spec."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="USD prim path, e.g. /World/Store/Products/s_salsa_hot/s_salsa_hot_00.")
    role: PrimRole
    shape: PrimShape = "box"
    center: Vec3
    size: Vec3 = Field(description="Full extents along local x, y, z (cylinder/sphere: diameter, diameter, height).")
    yaw: float = 0.0
    color: str = "#cccccc"
    collider: bool = True
    label_text: str | None = None
    sku: Sku | None = None
    barcode: str | None = None
    instance_id: str | None = None
    slot_id: str | None = None
    lot_id: str | None = None
    fixture_id: str | None = None
    freshness: float | None = None
    mass_kg: float | None = None
    price_cents: int | None = None


class SceneSpec(BaseModel):
    """Everything the Isaac builder needs for one store, plus the ground-truth instances the worker reports."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    name: str
    data_hash: str = Field(description="contracts.sim.planogram_hash of the layout (SimHello.data_hash).")
    seed: int = 0
    bounds: Rect2D
    entrance: Pose2D
    checkout: Pose2D
    robot_home: Pose2D
    prims: list[PrimSpec] = Field(default_factory=list)
    instances: list[ItemInstance] = Field(default_factory=list)
    item_dims: dict[str, Vec3] = Field(default_factory=dict, description="instance_id -> item size (x, y, z).")
    item_yaw: dict[str, float] = Field(default_factory=dict, description="instance_id -> item yaw (radians).")
    warnings: list[str] = Field(default_factory=list)

    def by_role(self, role: PrimRole) -> list[PrimSpec]:
        return [p for p in self.prims if p.role == role]

    def product_prims(self) -> list[PrimSpec]:
        return self.by_role("product")

    def prim_for_instance(self, instance_id: str) -> PrimSpec | None:
        return next((p for p in self.prims if p.instance_id == instance_id), None)

    def instances_in_slot(self, slot_id: str) -> list[ItemInstance]:
        return [i for i in self.instances if i.slot_id == slot_id]


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------
_PATH_BAD = re.compile(r"[^A-Za-z0-9_]")


def usd_name(raw: str) -> str:
    """Make a valid USD prim name from an id: non [A-Za-z0-9_] -> '_', prefix '_' if it starts with a digit."""
    name = _PATH_BAD.sub("_", raw) or "_"
    return f"_{name}" if name[0].isdigit() else name


def instance_id_for(slot_id: str, n: int) -> ItemId:
    """Deterministic instance id ``<slot_id>#<nn>`` (n = unit index in the slot, front-left first), from
    ``contracts.sim.instance_id`` so the robot fake uses the same ids."""
    return _instance_id(slot_id, n)


def pick_face_dir(slot: Slot) -> float:
    """Direction (radians) the slot's pick face points: approach_pose.theta + pi (robot faces the shelf)."""
    return _wrap(slot.approach_pose.theta + math.pi)


def item_yaw_for(slot: Slot) -> float:
    """Yaw of a unit in this slot so its label (-y face) points out of the pick face: pick_dir + pi/2."""
    return _wrap(pick_face_dir(slot) + math.pi / 2)


def level_heights(fixture: Fixture) -> list[float]:
    """Shelf board heights (top surface z). ``level_z_m`` when set, else evenly spaced from 0.1 m."""
    if fixture.level_z_m is not None:
        return list(fixture.level_z_m)
    step = (fixture.height_m - 0.1) / fixture.levels
    return [round(0.1 + i * step, 3) for i in range(fixture.levels)]


def to_world(pose: Pose2D, local: Vec3) -> Point3:
    """Robot-local offset (x forward, y left, z up) -> world point."""
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    return Point3(x=pose.x + c * local[0] - s * local[1], y=pose.y + s * local[0] + c * local[1], z=local[2])


def basket_drop_position(pose: Pose2D, index: int, item_dims: Vec3 = (0.08, 0.08, 0.15)) -> Point3:
    """Center of the ``index``-th item dropped in the basket (2 x 2 grid per layer, then stacked)."""
    bx, by, _ = BASKET_SIZE_M
    col, row, layer = index % 2, (index // 2) % 2, index // 4
    lx = BASKET_OFFSET_M[0] + (col - 0.5) * bx / 2
    ly = BASKET_OFFSET_M[1] + (row - 0.5) * by / 2
    lz = BASKET_OFFSET_M[2] + item_dims[2] / 2 + layer * item_dims[2]
    return to_world(pose, (lx, ly, lz))


def gripper_position(pose: Pose2D) -> Point3:
    return to_world(pose, GRIPPER_OFFSET_M)


def _wrap(theta: float) -> float:
    return math.atan2(math.sin(theta), math.cos(theta))


def _axis(theta: float) -> tuple[int, int]:
    """Snap a direction to the nearest world axis: (+1,0) / (-1,0) / (0,+1) / (0,-1)."""
    c, s = math.cos(theta), math.sin(theta)
    if abs(c) >= abs(s):
        return (1 if c > 0 else -1, 0)
    return (0, 1 if s > 0 else -1)


# --------------------------------------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------------------------------------
def _store_shell(plan: Planogram) -> list[PrimSpec]:
    b = plan.bounds
    cx, cy = b.x + b.w / 2, b.y + b.h / 2
    t, hh = WALL_THICKNESS_M, WALL_HEIGHT_M
    prims = [
        PrimSpec(
            path="/World/Store/Floor",
            role="floor",
            center=(cx, cy, -FLOOR_THICKNESS_M / 2),
            size=(b.w + 2 * t, b.h + 2 * t, FLOOR_THICKNESS_M),
            color=FLOOR_COLOR,
        )
    ]
    walls = {
        "south": ((cx, b.y - t / 2, hh / 2), (b.w + 2 * t, t, hh)),
        "north": ((cx, b.y_max + t / 2, hh / 2), (b.w + 2 * t, t, hh)),
        "west": ((b.x - t / 2, cy, hh / 2), (t, b.h, hh)),
        "east": ((b.x_max + t / 2, cy, hh / 2), (t, b.h, hh)),
    }
    for name, (center, size) in walls.items():
        prims.append(
            PrimSpec(path=f"/World/Store/Walls/{name}", role="wall", center=center, size=size, color=WALL_COLOR)
        )
    for i, ob in enumerate(plan.obstacles):
        prims.append(
            PrimSpec(
                path=f"/World/Store/Obstacles/obstacle_{i}",
                role="obstacle",
                center=(ob.x + ob.w / 2, ob.y + ob.h / 2, OBSTACLE_HEIGHT_M / 2),
                size=(ob.w, ob.h, OBSTACLE_HEIGHT_M),
                color=OBSTACLE_COLOR,
            )
        )
    for name, pose, color in (("entrance", plan.entrance, ENTRANCE_COLOR), ("checkout", plan.checkout, CHECKOUT_COLOR)):
        prims.append(
            PrimSpec(
                path=f"/World/Store/Markers/{name}",
                role="marker",
                center=(pose.x, pose.y, 0.002),
                size=(MARKER_SIZE_M, MARKER_SIZE_M, 0.004),
                yaw=pose.theta,
                color=color,
                collider=False,
                label_text=name.upper(),
            )
        )
    return prims


def _fixture_prims(fx: Fixture) -> list[PrimSpec]:
    r = fx.rect
    root = f"/World/Store/Fixtures/{usd_name(fx.fixture_id)}"
    color = FIXTURE_COLORS.get(fx.kind, "#b8bcc4")
    cx, cy = r.x + r.w / 2, r.y + r.h / 2
    common = {"fixture_id": fx.fixture_id, "color": color}
    if fx.kind == "checkout":
        return [
            PrimSpec(
                path=f"{root}/counter",
                role="base",
                center=(cx, cy, fx.height_m / 2),
                size=(r.w, r.h, fx.height_m),
                **common,
            )
        ]

    prims: list[PrimSpec] = []
    heights = level_heights(fx)
    for i, z in enumerate(heights):
        if fx.kind == "produce_bin" and i == 0 and z > BOARD_THICKNESS_M:
            # produce table: solid base from the floor up to the first level
            prims.append(
                PrimSpec(path=f"{root}/base", role="base", center=(cx, cy, z / 2), size=(r.w, r.h, z), **common)
            )
            continue
        if z < BOARD_THICKNESS_M:
            continue  # items stand on the floor
        prims.append(
            PrimSpec(
                path=f"{root}/board_{i}",
                role="board",
                center=(cx, cy, z - BOARD_THICKNESS_M / 2),
                size=(r.w, r.h, BOARD_THICKNESS_M),
                **common,
            )
        )
    if fx.kind == "produce_bin":
        return prims

    # Back panel (single-sided: on the edge opposite the pick face) or center divider (double-sided gondola).
    ax, ay = _axis(fx.yaw)
    t = PANEL_THICKNESS_M
    if ax != 0:  # pick face points +-x -> panel is a y-z plane
        px = cx if fx.double_sided else (r.x + t / 2 if ax > 0 else r.x_max - t / 2)
        center, size = (px, cy, fx.height_m / 2), (t, r.h, fx.height_m)
    else:
        py = cy if fx.double_sided else (r.y + t / 2 if ay > 0 else r.y_max - t / 2)
        center, size = (cx, py, fx.height_m / 2), (r.w, t, fx.height_m)
    prims.append(PrimSpec(path=f"{root}/panel", role="panel", center=center, size=size, **common))
    return prims


def _available_depth(fx: Fixture, pick_dir: float) -> float:
    ax, _ = _axis(pick_dir)
    depth = fx.rect.w if ax != 0 else fx.rect.h
    if fx.kind == "produce_bin":
        return depth
    return depth / 2 - PANEL_THICKNESS_M / 2 if fx.double_sided else depth - PANEL_THICKNESS_M


def _vertical_room(fx: Fixture, level: int) -> float:
    heights = level_heights(fx)
    top = heights[level + 1] - BOARD_THICKNESS_M if level + 1 < len(heights) else fx.height_m
    return max(0.0, top - heights[level] - ITEM_GAP_M) if level < len(heights) else 0.0


def _unit_lots(slot: Slot, n: int) -> list[tuple[str | None, float | None]]:
    """(lot_id, lot freshness) per unit, front units first, following the slot's lots in file order."""
    out: list[tuple[str | None, float | None]] = []
    for lot in slot.lots:
        out += [(lot.lot_id, lot.freshness)] * lot.qty
    out += [(None, None)] * max(0, n - len(out))
    return out[:n]


def _slot_units(
    slot: Slot, fx: Fixture, product: Product, seed: int, warnings: list[str]
) -> tuple[list[PrimSpec], list[ItemInstance], dict[str, Vec3], dict[str, float]]:
    asset = product.asset
    w, d, h = asset.dims_m
    pick_dir = pick_face_dir(slot)
    yaw = item_yaw_for(slot)
    inward = (-math.cos(pick_dir), -math.sin(pick_dir))  # into the fixture
    lateral = (math.cos(yaw), math.sin(yaw))  # item-local +x in world (left -> right when facing the shelf)

    cols = slot.facings
    if fx.kind == "produce_bin":
        cols = max(cols, min(slot.qty, 4))  # spread produce so a scan sees several units side by side
    rows = max(1, int(_available_depth(fx, pick_dir) // (d + ITEM_GAP_M)))
    room = _vertical_room(fx, slot.level)
    layers = max(1, int(room // h)) if room > 0 else 1
    fit = min(cols * rows * layers, MAX_UNITS_PER_SLOT)
    n = min(slot.qty, fit)
    if slot.qty > n:
        warnings.append(f"{slot.slot_id}: qty {slot.qty} > {n} units that fit; sim shows {n}")

    prims: list[PrimSpec] = []
    instances: list[ItemInstance] = []
    dims: dict[str, Vec3] = {}
    yaws: dict[str, float] = {}
    base = slot.item_position
    slot_root = f"/World/Store/Products/{usd_name(slot.slot_id)}"
    label = asset.label_text or product.name
    price = slot.price_cents if slot.price_cents is not None else product.price_cents
    for k, (lot_id, lot_fresh) in enumerate(_unit_lots(slot, n)):
        col = k % cols
        row = (k // cols) % rows
        layer = k // (cols * rows)
        lat = (col - (cols - 1) / 2) * (w + ITEM_GAP_M)
        dep = d / 2 + row * (d + ITEM_GAP_M)
        x = base.x + lateral[0] * lat + inward[0] * dep
        y = base.y + lateral[1] * lat + inward[1] * dep
        z = base.z + h / 2 + layer * h
        iid = instance_id_for(slot.slot_id, k)
        fresh = unit_freshness(lot_fresh, iid)
        prims.append(
            PrimSpec(
                path=f"{slot_root}/{usd_name(iid)}",
                role="product",
                shape=asset.shape,
                center=(round(x, 4), round(y, 4), round(z, 4)),
                size=(w, d, h),
                yaw=round(yaw, 6),
                color=asset.label_color,
                label_text=label,
                sku=product.sku,
                barcode=product.barcode,
                instance_id=iid,
                slot_id=slot.slot_id,
                lot_id=lot_id,
                fixture_id=fx.fixture_id,
                freshness=fresh,
                mass_kg=asset.mass_kg,
                price_cents=price,
            )
        )
        instances.append(
            ItemInstance(
                instance_id=iid,
                sku=product.sku,
                slot_id=slot.slot_id,
                position=Point3(x=round(x, 4), y=round(y, 4), z=round(z, 4)),
                freshness=fresh,
                lot_id=lot_id,
            )
        )
        dims[iid] = (w, d, h)
        yaws[iid] = round(yaw, 6)
    return prims, instances, dims, yaws


def build_scene_spec(
    planogram: Planogram, products: dict[Sku, Product] | Iterable[Product], seed: int = 0
) -> SceneSpec:
    """Planogram + products -> SceneSpec (fixtures as boxes, one product prim + ItemInstance per stocked unit).

    Deterministic for the same inputs + seed. Problems that should not kill a demo (unknown sku, more stock than fits)
    become ``SceneSpec.warnings`` instead of exceptions.
    """
    by_sku = products if isinstance(products, dict) else {p.sku: p for p in products}
    warnings: list[str] = []
    prims = _store_shell(planogram)
    for fx in planogram.fixtures:
        prims += _fixture_prims(fx)

    instances: list[ItemInstance] = []
    dims: dict[str, Vec3] = {}
    yaws: dict[str, float] = {}
    for slot in planogram.slots:
        product = by_sku.get(slot.sku)
        fx = planogram.fixture(slot.fixture_id)
        if product is None:
            warnings.append(f"{slot.slot_id}: unknown sku {slot.sku!r}; slot left empty")
            continue
        if fx is None:  # unreachable with a validated Planogram, kept for safety
            warnings.append(f"{slot.slot_id}: unknown fixture {slot.fixture_id!r}")
            continue
        if slot.qty <= 0:
            continue
        p, inst, d, y = _slot_units(slot, fx, product, seed, warnings)
        prims += p
        instances += inst
        dims.update(d)
        yaws.update(y)

    return SceneSpec(
        store_id=planogram.store_id,
        name=planogram.name,
        data_hash=planogram_hash(planogram),
        seed=seed,
        bounds=planogram.bounds,
        entrance=planogram.entrance,
        checkout=planogram.checkout,
        robot_home=planogram.start_pose,
        prims=prims,
        instances=instances,
        item_dims=dims,
        item_yaw=yaws,
        warnings=warnings,
    )


def load_scene_spec(store_id: str, seed: int = 0, data_root: str | None = None) -> SceneSpec:
    """Build the scene for ``store_id`` from data/ (via utils.data)."""
    from utils.data import load_planogram, load_products

    return build_scene_spec(load_planogram(store_id, data_root), load_products(data_root), seed=seed)


def main(argv: list[str] | None = None) -> int:
    from utils.config import get_settings

    args = sys.argv[1:] if argv is None else argv
    store_id = args[0] if args else get_settings().picky_default_store
    spec = load_scene_spec(store_id)
    roles: dict[str, int] = {}
    for p in spec.prims:
        roles[p.role] = roles.get(p.role, 0) + 1
    print(f"{spec.store_id} ({spec.name}) hash={spec.data_hash[:12]} bounds={spec.bounds.w}x{spec.bounds.h} m")
    print("prims: " + ", ".join(f"{k}={v}" for k, v in sorted(roles.items())))
    print(f"instances: {len(spec.instances)} across {len({i.slot_id for i in spec.instances})} slots")
    for w in spec.warnings:
        print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
