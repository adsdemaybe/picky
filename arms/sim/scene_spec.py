"""Planogram(s) + products + robots -> flat primitive specs the scene builders consume (Isaac in app.py, the Pillow
sketch renderer in sketch.py).

PURE python (pydantic only): no Isaac, no pxr, no I/O except ``load_*``. It is the single translation from the data
files to 3D, so it is unit-tested on a laptop and the GPU box only has to turn each ``PrimSpec`` into a USD prim.

Stage layout (contracts/planogram.py): ONE stage, several stores. Each store is a translation-only Xform
``/World/Stores/<store>`` placed at ``store_offsets()`` (stores side by side along +x, ``store_pitch_m`` apart =
max(``STORE_PITCH_MIN_M``, largest store side + ``STORE_GAP_M``)). Everything under that Xform, and every coordinate in
this module, is STORE-LOCAL: (x, y) = store (x, y), origin at the store's bottom-left corner, z = height above the
floor. Wire poses are store-local too, so offsets matter only to the renderer.

``PrimSpec.center`` is the CENTER of the primitive; ``size`` its full extent along local x / y / z before ``yaw``.

Products: ``Product.asset`` gives shape + dims (item-local: x = width, y = depth, z = up; label on the -y face). Each
unit is rotated so its label faces out of the pick face (pick-face direction = ``approach_pose.theta + pi``), i.e. item
yaw = pick_dir + pi/2. ``Slot.item_position`` is the front-center of the front item's BASE, so a unit's center is pushed
back into the fixture by depth/2 and up by height/2. Positions come from a per-slot ``SlotLayout`` (``facings`` wide,
rows deep, stacked layers; produce bins pile 4+ wide and 2 high); unit n of a slot (``contracts.sim.instance_id``) sits
at the layout position of its rank, front units first, and units are assigned to the slot's lots in file order. EVERY
stocked unit is spawned (the scan ground truth must equal the catalog qty): units beyond what fits share the back-row
positions (hidden behind the front rows) and a warning is recorded.

Per-item models (``arms.sim.assets.SkuAssets``, optional): a SKU that resolves to a model uses the model's
``target_dims_m`` as its unit size (``SlotLayout.dims``) and its prims carry ``render='asset'|'parts'``, so the Isaac
backend references the model instead of drawing a box. Unit counts, instance ids and lots never change; without assets
(the default) everything is exactly the primitive scene.

Rovers (``RoverSpec``): a small diff-drive base + vertical lift mast + an SO-101 approximated with primitives (links
sized from the SO-ARM100 repo's SO-101 URDF: base, shoulder, upper arm 0.116 m, forearm 0.135 m, wrist + gripper),
a basket on the back half at ``contracts.sim.BASKET_OFFSET_LOCAL`` and the pan-tilt front camera head on the mast.
Prims are robot-local and grouped: ``base`` (rides the base), ``lift`` (rides the lift carriage, z relative to
``LIFT_REF_M``), ``arm`` (rides the carriage and extends along +x). The Isaac backend may swap the arm primitives for
the real URDF (``PICKY_SO101_URDF``, see app.py). With a rover USD (``PICKY_ROVER_USD`` -> ``picky_rover.usda``) the
spec also carries ``model_usd`` / ``model_path`` and ``puppet_prims``: the LeKiwi base + SO-101 come from the USD
(posed kinematically, ``arms.sim.rover_puppet``) and only the mast, camera head, carriage bracket and basket stay
primitives; ``prims`` remains the full primitive rover (fallback, Pillow sketch).

Usage: ``python -m arms.sim.scene_spec [store_id ...]`` prints a summary of the scene for the data in PICKY_DATA_DIR.
"""

import math
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arms.sim import rover_puppet as puppet
from arms.sim.assets import SkuAssets
from contracts.cameras import DEFAULT_ROBOT_CAMERA_MOUNTS, CameraInfo, CameraMount, robot_camera_id, store_camera_id
from contracts.catalog import Lot, Product
from contracts.common import ARM_MOUNT_X_M, ItemId, Point3, Pose2D, Rect2D, Sku
from contracts.planogram import Fixture, Planogram, RobotSpec, Slot, StoreCameraSpec
from contracts.sim import BASKET_OFFSET_LOCAL, UNIT_FRESHNESS_JITTER, ItemInstance, planogram_hash, unit_freshness
from contracts.sim import BASKET_SIZE_M as _BASKET_SIZE_M
from contracts.sim import instance_id as _instance_id

PrimRole = Literal[
    "floor",
    "wall",
    "ceiling",
    "obstacle",
    "board",
    "panel",
    "base",
    "product",
    "marker",
    "light",
    "rover",
]
PrimShape = Literal["box", "cylinder", "sphere"]
RoverGroup = Literal["base", "lift", "arm"]
UnitRender = Literal["primitive", "asset", "parts"]
Vec3 = tuple[float, float, float]

# ---- store geometry (meters)
BOARD_THICKNESS_M = 0.03
PANEL_THICKNESS_M = 0.03
WALL_HEIGHT_M = 3.0
WALL_THICKNESS_M = 0.1
FLOOR_THICKNESS_M = 0.05
CEILING_HEIGHT_M = 3.4
CEILING_SLAB_GAP_M = 0.08
"""The ceiling slab sits this far above CEILING_HEIGHT_M (lights, camera mounts): ceiling cameras mounted AT the
ceiling height (robots.yaml) otherwise start inside the slab and render its unlit inside (black band in Isaac)."""
OBSTACLE_HEIGHT_M = 3.0
ITEM_GAP_M = 0.01
SLOT_ROOM_FILL = 0.9
"""Share of a slot's lateral room (``slot_lateral_room``) its front row may use (gap to the neighbouring SKU)."""
SLOT_ROOM_DEFAULT_M = 0.6
"""Lateral room of a slot with no neighbour on its fixture level (still capped by the fixture's ends)."""
MAX_UNITS_PER_SLOT = 120
"""Hard cap on spawned units per slot (catalog capacities are <= 60)."""
MARKER_SIZE_M = 0.8
STORE_GAP_M = 20.0
STORE_PITCH_MIN_M = 60.0
FRESHNESS_JITTER = UNIT_FRESHNESS_JITTER
"""Per-unit freshness = lot freshness +- this (``contracts.sim.unit_freshness``, deterministic per instance id and
shared with the robot fake, so a mid-trip fallback keeps the produce top-3 ranking)."""

# ---- rover (robot-local: x forward, y left, z up). Shared ground truth with the robot fake via contracts.sim.
BASKET_OFFSET_M: Vec3 = BASKET_OFFSET_LOCAL
"""Center of the basket floor relative to the robot base pose (behind the mast, on the deck)."""
BASKET_SIZE_M: Vec3 = _BASKET_SIZE_M
BASE_SIZE_M: Vec3 = (0.46, 0.36, 0.16)
BASE_CLEARANCE_M = 0.05
WHEEL_RADIUS_M = 0.06
MAST_HEIGHT_M = 1.50
MAST_CAMERA_CLEARANCE_M = 0.15
"""The mast tops out this far below the front camera eye (> the renderers' 0.05 m near clip; a thin neck BEHIND the
eye carries the camera head): with the eye inside the mast column, Isaac (RTX renders back faces) showed the mast's
unlit interior across the bottom of the front view, and a mast top just below the lens filled a quarter of it."""
LIFT_REF_M = 0.55
"""Lift-group prims are authored for this lift height; the builder translates the group by (lift - LIFT_REF_M)."""
SO101_LINKS_M: tuple[tuple[str, float], ...] = (("upper_arm", 0.116), ("forearm", 0.135), ("wrist_gripper", 0.10))
"""SO-101 link lengths (approx., from the SO-ARM100 URDF) used for the primitive arm."""

FIXTURE_COLORS: dict[str, str] = {
    "shelf": "#d3d6db",
    "fridge": "#dfe9f2",
    "freezer": "#cfe0f5",
    "produce_bin": "#8b6a43",
    "endcap": "#c9b37e",
    "checkout": "#3a3f4b",
}
FLOOR_COLOR = "#e9e4da"
WALL_COLOR = "#f4f1ea"
CEILING_COLOR = "#fbfbf8"
OBSTACLE_COLOR = "#9a9a9a"
ENTRANCE_COLOR = "#3fb27f"
CHECKOUT_COLOR = "#f2a33a"
DOCK_COLOR = "#2bb3c0"
ROVER_DEFAULT_COLOR = "#2563eb"


class SceneSpecError(ValueError):
    """The planogram cannot be turned into a scene (e.g. empty / inconsistent)."""


class PrimSpec(BaseModel):
    """One primitive. The builder creates one USD prim (product units: an Xform with a body + a label quad)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="USD prim path, e.g. /World/Stores/store_ave/Products/AVE_G2_E1_03/AVE_G2_E1_03_00.")
    role: PrimRole
    shape: PrimShape = "box"
    center: Vec3
    size: Vec3 = Field(description="Full extents along local x, y, z (cylinder/sphere: diameter, diameter, height).")
    yaw: float = 0.0
    pitch: float = Field(0.0, description="Rotation about local +y (radians), rover arm links only.")
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
    store_id: str | None = None
    group: RoverGroup | None = Field(None, description="Rover prims: which moving part it rides.")
    render: UnitRender = Field(
        "primitive", description="Product units: 'asset' / 'parts' = draw the SKU's model (arms.sim.assets)."
    )


class SlotLayout(BaseModel):
    """Where the units of one slot go (store-local), front units first. Used at build time and by set_slot_stock."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str
    sku: Sku
    fixture_id: str
    shape: PrimShape = "box"
    dims: Vec3
    yaw: float
    color: str
    label_text: str
    barcode: str | None = None
    price_cents: int | None = None
    mass_kg: float | None = None
    render: UnitRender = "primitive"
    positions: list[Vec3] = Field(description="Grid positions (unit centers) that fit, front row first.")

    def position(self, rank: int) -> Vec3:
        """Center of the unit with this rank (0 = front). Overflow shares the back-row positions, nudged so the
        prims don't coincide exactly (they stay hidden behind the front rows)."""
        if rank < len(self.positions):
            return self.positions[rank]
        back = self.positions[-max(1, min(len(self.positions), 4)) :]
        k = rank - len(self.positions)
        x, y, z = back[k % len(back)]
        bump = 0.002 * (1 + k // len(back))
        return (round(x, 4), round(y, 4), round(z + bump, 4))


class SceneSpec(BaseModel):
    """Everything a builder needs for one store (store-local), plus the ground-truth instances the worker reports."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    name: str
    root_path: str = Field(description="'/World/Stores/<store>': translation-only Xform (store_offsets).")
    data_hash: str = Field(description="contracts.sim.planogram_hash of the layout (SimStoreInfo.data_hash).")
    seed: int = 0
    bounds: Rect2D
    entrance: Pose2D
    checkout: Pose2D
    robot_home: Pose2D
    prims: list[PrimSpec] = Field(default_factory=list)
    instances: list[ItemInstance] = Field(default_factory=list)
    item_dims: dict[str, Vec3] = Field(default_factory=dict, description="instance_id -> item size (x, y, z).")
    item_yaw: dict[str, float] = Field(default_factory=dict, description="instance_id -> item yaw (radians).")
    slot_layouts: dict[str, SlotLayout] = Field(default_factory=dict)
    products: dict[Sku, Product] = Field(default_factory=dict, description="Products stocked here (label textures).")
    warnings: list[str] = Field(default_factory=list)

    def by_role(self, role: PrimRole) -> list[PrimSpec]:
        return [p for p in self.prims if p.role == role]

    def product_prims(self) -> list[PrimSpec]:
        return self.by_role("product")

    def prim_for_instance(self, instance_id: str) -> PrimSpec | None:
        return next((p for p in self.prims if p.instance_id == instance_id), None)

    def instances_in_slot(self, slot_id: str) -> list[ItemInstance]:
        return [i for i in self.instances if i.slot_id == slot_id]


class RoverSpec(BaseModel):
    """One rover: robot-local primitives (grouped by moving part) + its config."""

    model_config = ConfigDict(extra="forbid")

    robot_id: str
    store_id: str
    name: str
    color: str
    root_path: str = Field(description="'/World/Stores/<store>/Robots/<robot>' (pose = base pose in the store frame).")
    dock: Pose2D
    battery_pct: float = 100.0
    basket_capacity: int = 12
    max_speed_mps: float = 0.8
    camera_ids: list[str] = Field(default_factory=list)
    prims: list[PrimSpec] = Field(default_factory=list)
    model_usd: str | None = Field(None, description="Rover USD referenced as a visual puppet (None = primitives).")
    model_path: str | None = Field(None, description="'<root>/Model/LeKiwi': the prim that references model_usd.")
    puppet_prims: list[PrimSpec] = Field(
        default_factory=list, description="With model_usd: the primitives kept next to the USD (mast, basket, ...)."
    )


class WorldSpec(BaseModel):
    """Every store + rover + camera the worker hosts."""

    model_config = ConfigDict(extra="forbid")

    stores: dict[str, SceneSpec] = Field(default_factory=dict)
    offsets: dict[str, Vec3] = Field(default_factory=dict, description="store_id -> /World/Stores/<store> translate.")
    rovers: dict[str, RoverSpec] = Field(default_factory=dict)
    cameras: list[CameraInfo] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------
_PATH_BAD = re.compile(r"[^A-Za-z0-9_]")


def usd_name(raw: str) -> str:
    """Make a valid USD prim name from an id: non [A-Za-z0-9_] -> '_', prefix '_' if it starts with a digit."""
    name = _PATH_BAD.sub("_", raw) or "_"
    return f"_{name}" if name[0].isdigit() else name


def store_root(store_id: str) -> str:
    return f"/World/Stores/{usd_name(store_id)}"


def rover_root(store_id: str, robot_id: str) -> str:
    return f"{store_root(store_id)}/Robots/{usd_name(robot_id)}"


def camera_prim_path(store_id: str, camera_id: str) -> str:
    return f"{store_root(store_id)}/Cameras/{usd_name(camera_id)}"


def instance_id_for(slot_id: str, n: int) -> ItemId:
    """Deterministic instance id ``<slot_id>#<nn>`` (``contracts.sim.instance_id``, shared with the robot fake)."""
    return _instance_id(slot_id, n)


def instance_index(instance_id: str) -> int:
    """'AVE-G1-E1-00#07' -> 7 (-1 when the id has no '#nn' suffix)."""
    _, sep, n = instance_id.rpartition("#")
    return int(n) if sep and n.isdigit() else -1


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
    """Robot-local offset (x forward, y left, z up) -> store-frame point."""
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    return Point3(x=pose.x + c * local[0] - s * local[1], y=pose.y + s * local[0] + c * local[1], z=local[2])


def basket_drop_position(pose: Pose2D, index: int, item_dims: Vec3 = (0.08, 0.08, 0.15)) -> Point3:
    """Center of the ``index``-th item dropped in the basket (2 x 3 grid per layer, then stacked). Basket units are
    yawed pose.theta + pi/2 (width along the robot's y, depth along its x); a big unit is pulled in so its footprint
    stays inside the basket walls instead of hanging over them."""
    bx, by, _ = BASKET_SIZE_M
    col, row, layer = index % 2, (index // 2) % 3, index // 6
    w, d = item_dims[0], item_dims[1]
    ox, oy = BASKET_OFFSET_M[0], BASKET_OFFSET_M[1]
    lx = ox + (col - 0.5) * bx / 2
    ly = oy + (row - 1) * by / 3
    hx, hy = max(0.0, (bx - d) / 2), max(0.0, (by - w) / 2)
    lx = min(ox + hx, max(ox - hx, lx))
    ly = min(oy + hy, max(oy - hy, ly))
    lz = BASKET_OFFSET_M[2] + item_dims[2] / 2 + layer * min(item_dims[2], 0.08)
    return to_world(pose, (lx, ly, lz))


def _wrap(theta: float) -> float:
    return math.atan2(math.sin(theta), math.cos(theta))


def _axis(theta: float) -> tuple[int, int]:
    """Snap a direction to the nearest world axis: (+1,0) / (-1,0) / (0,+1) / (0,-1)."""
    c, s = math.cos(theta), math.sin(theta)
    if abs(c) >= abs(s):
        return (1 if c > 0 else -1, 0)
    return (0, 1 if s > 0 else -1)


def store_pitch_m(sizes: Iterable[tuple[float, float]]) -> float:
    """Distance between neighboring store origins: >= the largest store side + STORE_GAP_M (contracts rule)."""
    largest = max((max(w, h) for w, h in sizes), default=0.0)
    return max(STORE_PITCH_MIN_M, math.ceil((largest + STORE_GAP_M) / 10.0) * 10.0)


def store_offsets(order: Sequence[str], sizes: dict[str, tuple[float, float]]) -> dict[str, Vec3]:
    """Translation of each ``/World/Stores/<store>`` Xform: stores side by side along +x in ``order``."""
    pitch = store_pitch_m(sizes.values())
    return {sid: (round(i * pitch, 3), 0.0, 0.0) for i, sid in enumerate(order)}


# --------------------------------------------------------------------------------------------------------------------
# Store builders
# --------------------------------------------------------------------------------------------------------------------
def _store_shell(plan: Planogram, root: str) -> list[PrimSpec]:
    b = plan.bounds
    sid = plan.store_id
    cx, cy = b.x + b.w / 2, b.y + b.h / 2
    t, hh = WALL_THICKNESS_M, WALL_HEIGHT_M
    prims = [
        PrimSpec(
            path=f"{root}/Floor",
            role="floor",
            center=(cx, cy, -FLOOR_THICKNESS_M / 2),
            size=(b.w + 2 * t, b.h + 2 * t, FLOOR_THICKNESS_M),
            color=FLOOR_COLOR,
            store_id=sid,
        ),
        PrimSpec(
            path=f"{root}/Ceiling",
            role="ceiling",
            center=(cx, cy, CEILING_HEIGHT_M + CEILING_SLAB_GAP_M + 0.025),
            size=(b.w + 2 * t, b.h + 2 * t, 0.05),
            color=CEILING_COLOR,
            collider=False,
            store_id=sid,
        ),
    ]
    walls = {
        "south": ((cx, b.y - t / 2, hh / 2), (b.w + 2 * t, t, hh)),
        "north": ((cx, b.y_max + t / 2, hh / 2), (b.w + 2 * t, t, hh)),
        "west": ((b.x - t / 2, cy, hh / 2), (t, b.h, hh)),
        "east": ((b.x_max + t / 2, cy, hh / 2), (t, b.h, hh)),
    }
    for name, (center, size) in walls.items():
        prims.append(
            PrimSpec(path=f"{root}/Walls/{name}", role="wall", center=center, size=size, color=WALL_COLOR, store_id=sid)
        )
    for i, ob in enumerate(plan.obstacles):
        prims.append(
            PrimSpec(
                path=f"{root}/Obstacles/obstacle_{i}",
                role="obstacle",
                center=(ob.x + ob.w / 2, ob.y + ob.h / 2, OBSTACLE_HEIGHT_M / 2),
                size=(ob.w, ob.h, OBSTACLE_HEIGHT_M),
                color=OBSTACLE_COLOR,
                store_id=sid,
            )
        )
    for name, pose, color in (("entrance", plan.entrance, ENTRANCE_COLOR), ("checkout", plan.checkout, CHECKOUT_COLOR)):
        prims.append(
            PrimSpec(
                path=f"{root}/Markers/{name}",
                role="marker",
                center=(pose.x, pose.y, 0.002),
                size=(MARKER_SIZE_M, MARKER_SIZE_M, 0.004),
                yaw=pose.theta,
                color=color,
                collider=False,
                label_text=name.upper(),
                store_id=sid,
            )
        )
    # ceiling light panels on a ~4 m grid (the Isaac backend adds a RectLight per panel)
    nx, ny = max(1, round(b.w / 4)), max(1, round(b.h / 4))
    for i in range(nx):
        for j in range(ny):
            prims.append(
                PrimSpec(
                    path=f"{root}/Lights/panel_{i}_{j}",
                    role="light",
                    center=(b.x + (i + 0.5) * b.w / nx, b.y + (j + 0.5) * b.h / ny, CEILING_HEIGHT_M - 0.01),
                    size=(1.2, 0.3, 0.02),
                    color="#ffffff",
                    collider=False,
                    store_id=sid,
                )
            )
    return prims


def _fixture_prims(fx: Fixture, root: str, store_id: str) -> list[PrimSpec]:
    r = fx.rect
    froot = f"{root}/Fixtures/{usd_name(fx.fixture_id)}"
    color = FIXTURE_COLORS.get(fx.kind, "#b8bcc4")
    cx, cy = r.x + r.w / 2, r.y + r.h / 2
    common = {"fixture_id": fx.fixture_id, "color": color, "store_id": store_id}
    if fx.kind == "checkout":
        return [
            PrimSpec(
                path=f"{froot}/counter",
                role="base",
                center=(cx, cy, fx.height_m / 2),
                size=(r.w, r.h, fx.height_m),
                label_text=fx.label,
                **common,
            )
        ]

    prims: list[PrimSpec] = []
    heights = level_heights(fx)
    for i, z in enumerate(heights):
        if fx.kind == "produce_bin" and i == 0 and z > BOARD_THICKNESS_M:
            prims.append(
                PrimSpec(path=f"{froot}/base", role="base", center=(cx, cy, z / 2), size=(r.w, r.h, z), **common)
            )
            continue
        if z < BOARD_THICKNESS_M:
            continue  # items stand on the floor
        prims.append(
            PrimSpec(
                path=f"{froot}/board_{i}",
                role="board",
                center=(cx, cy, z - BOARD_THICKNESS_M / 2),
                size=(r.w, r.h, BOARD_THICKNESS_M),
                **common,
            )
        )
    if fx.kind == "produce_bin":
        return prims

    # Back panel (single-sided: on the edge opposite the pick face) or center divider (double-sided gondola), plus a
    # top board so fridges / shelves read as closed units.
    ax, ay = _axis(fx.yaw)
    t = PANEL_THICKNESS_M
    if ax != 0:
        px = cx if fx.double_sided else (r.x + t / 2 if ax > 0 else r.x_max - t / 2)
        center, size = (px, cy, fx.height_m / 2), (t, r.h, fx.height_m)
    else:
        py = cy if fx.double_sided else (r.y + t / 2 if ay > 0 else r.y_max - t / 2)
        center, size = (cx, py, fx.height_m / 2), (r.w, t, fx.height_m)
    prims.append(PrimSpec(path=f"{froot}/panel", role="panel", center=center, size=size, label_text=fx.label, **common))
    prims.append(
        PrimSpec(
            path=f"{froot}/top",
            role="board",
            center=(cx, cy, fx.height_m - BOARD_THICKNESS_M / 2),
            size=(r.w, r.h, BOARD_THICKNESS_M),
            **common,
        )
    )
    return prims


def _available_depth(fx: Fixture, pick_dir: float) -> float:
    ax, _ = _axis(pick_dir)
    depth = fx.rect.w if ax != 0 else fx.rect.h
    if fx.kind == "produce_bin":
        return depth / 2 if fx.double_sided else depth
    return depth / 2 - PANEL_THICKNESS_M / 2 if fx.double_sided else depth - PANEL_THICKNESS_M


def _vertical_room(fx: Fixture, level: int) -> float:
    heights = level_heights(fx)
    if level >= len(heights):
        return 0.0
    top = heights[level + 1] - BOARD_THICKNESS_M if level + 1 < len(heights) else fx.height_m - BOARD_THICKNESS_M
    return max(0.0, top - heights[level] - ITEM_GAP_M)


def unit_lots(lots: Sequence[Lot], n: int) -> list[tuple[str | None, float | None]]:
    """(lot_id, lot freshness) per unit rank, front units first, following ``lots`` in file order."""
    out: list[tuple[str | None, float | None]] = []
    for lot in lots:
        out += [(lot.lot_id, lot.freshness)] * lot.qty
    out += [(None, None)] * max(0, n - len(out))
    return out[:n]


def slot_lateral_room(planogram: Planogram, slot: Slot, fx: Fixture) -> float:
    """Width (m) along the shelf the units of ``slot`` may spread over: the distance to the nearest other slot on the
    same fixture, level and pick face (``SLOT_ROOM_DEFAULT_M`` without one), capped by the fixture's ends."""
    pick = pick_face_dir(slot)
    yaw = item_yaw_for(slot)
    lat = (math.cos(yaw), math.sin(yaw))
    p = slot.item_position
    gaps = [
        abs((o.item_position.x - p.x) * lat[0] + (o.item_position.y - p.y) * lat[1])
        for o in planogram.slots
        if o.slot_id != slot.slot_id
        and o.fixture_id == slot.fixture_id
        and o.level == slot.level
        and abs(_wrap(pick_face_dir(o) - pick)) <= 0.3  # not the other face of a double-sided gondola
    ]
    gaps = [g for g in gaps if g > 1e-3]
    room = min(gaps) if gaps else SLOT_ROOM_DEFAULT_M
    r = fx.rect
    ax, _ = _axis(yaw)
    lo, hi, c = (r.x, r.x_max, p.x) if ax != 0 else (r.y, r.y_max, p.y)
    to_end = min(c - lo, hi - c)
    if to_end > 0:
        room = min(room, 2 * to_end)
    return max(0.0, room)


def slot_layout(
    slot: Slot,
    fx: Fixture,
    product: Product,
    dims: Vec3 | None = None,
    render: UnitRender = "primitive",
    lateral_room_m: float | None = None,
) -> SlotLayout:
    """Grid positions for the units of ``slot`` (store-local centers), front row first. ``dims`` overrides the
    products.yaml unit size (a per-item model's ``target_dims_m``). ``lateral_room_m`` (``slot_lateral_room``): shelf
    slots then fill WIDE first, like a stocked store: as many front-row facings as fit in that room (at least
    ``slot.facings``, at most what the slot's capacity / qty needs), each row ordered from the center out, so rank 0
    stays on the slot's tag (what a default pick takes) and a partly sold-out slot keeps a centered block."""
    asset = product.asset
    w, d, h = dims or asset.dims_m
    pick_dir = pick_face_dir(slot)
    yaw = item_yaw_for(slot)
    inward = (-math.cos(pick_dir), -math.sin(pick_dir))  # into the fixture
    lateral = (math.cos(yaw), math.sin(yaw))  # item-local +x in world (left -> right when facing the shelf)

    cols = max(slot.facings, 4) if fx.kind == "produce_bin" else slot.facings
    if lateral_room_m is not None and fx.kind != "produce_bin":
        fit = int((lateral_room_m * SLOT_ROOM_FILL + ITEM_GAP_M) // (w + ITEM_GAP_M))
        cols = max(slot.facings, min(fit, max(slot.capacity, slot.qty, 1)))
    rows = max(1, int(_available_depth(fx, pick_dir) // (d + ITEM_GAP_M)))
    room = _vertical_room(fx, slot.level)
    layers = max(1, int(room // h)) if room > 0 else 1
    if fx.kind == "produce_bin":
        layers = max(layers, 2)  # produce is piled
    # wide fill: one column sits ON the tag (rank 0, the default pick, straight ahead of the arm), the rest alternate
    # left / right of it; without a room the grid stays centered on the tag
    center = (cols - 1) // 2 if lateral_room_m is not None else (cols - 1) / 2
    order = sorted(range(cols), key=lambda c: (abs(c - center), c)) if lateral_room_m is not None else range(cols)
    positions: list[Vec3] = []
    base = slot.item_position
    for layer in range(layers):
        for row in range(rows):
            for col in order:
                if len(positions) >= MAX_UNITS_PER_SLOT:
                    break
                lat = (col - center) * (w + ITEM_GAP_M)
                dep = d / 2 + row * (d + ITEM_GAP_M)
                x = base.x + lateral[0] * lat + inward[0] * dep
                y = base.y + lateral[1] * lat + inward[1] * dep
                z = base.z + h / 2 + layer * h
                positions.append((round(x, 4), round(y, 4), round(z, 4)))
    # order: front row first across all layers? keep (layer, row, col) for the first layer, so the front row of the
    # bottom layer is ranks 0..cols-1 (what a shopper sees first)
    price = slot.price_cents if slot.price_cents is not None else product.price_cents
    return SlotLayout(
        slot_id=slot.slot_id,
        sku=product.sku,
        fixture_id=fx.fixture_id,
        shape=asset.shape,
        dims=(w, d, h),
        yaw=round(yaw, 6),
        color=asset.label_color,
        label_text=asset.label_text or product.name,
        barcode=product.barcode,
        price_cents=price,
        mass_kg=asset.mass_kg,
        render=render,
        positions=positions,
    )


def unit_prim(
    store_id: str, layout: SlotLayout, iid: str, rank: int, lot_id: str | None, fresh: float | None
) -> PrimSpec:
    """The product prim of one unit at its layout rank."""
    return PrimSpec(
        path=f"{store_root(store_id)}/Products/{usd_name(layout.slot_id)}/{usd_name(iid)}",
        role="product",
        shape=layout.shape,
        center=layout.position(rank),
        size=layout.dims,
        yaw=layout.yaw,
        color=layout.color,
        label_text=layout.label_text,
        sku=layout.sku,
        barcode=layout.barcode,
        instance_id=iid,
        slot_id=layout.slot_id,
        lot_id=lot_id,
        fixture_id=layout.fixture_id,
        freshness=fresh,
        mass_kg=layout.mass_kg,
        price_cents=layout.price_cents,
        collider=False,
        store_id=store_id,
        render=layout.render,
    )


def layout_for(
    slot: Slot,
    fx: Fixture,
    product: Product,
    assets: SkuAssets | None = None,
    lateral_room_m: float | None = None,
) -> SlotLayout:
    """``slot_layout`` with the SKU's per-item model (when ``assets`` resolves it): its dims + render mode."""
    ua = assets.resolve(product.sku) if assets is not None else None
    if ua is None:
        return slot_layout(slot, fx, product, lateral_room_m=lateral_room_m)
    return slot_layout(slot, fx, product, dims=ua.dims, render=ua.render, lateral_room_m=lateral_room_m)


def unit_instance(store_id: str, layout: SlotLayout, iid: str, rank: int, lot_id: str | None, fresh: float | None):
    x, y, z = layout.position(rank)
    return ItemInstance(
        instance_id=iid,
        sku=layout.sku,
        store_id=store_id,
        slot_id=layout.slot_id,
        position=Point3(x=x, y=y, z=z),
        freshness=fresh,
        lot_id=lot_id,
    )


def build_scene_spec(
    planogram: Planogram,
    products: dict[Sku, Product] | Iterable[Product],
    seed: int = 0,
    assets: SkuAssets | None = None,
) -> SceneSpec:
    """Planogram + products -> SceneSpec (store-local; fixtures as boxes, one product prim + ItemInstance per unit).

    Deterministic for the same inputs (ids and freshness do not depend on ``seed``). Problems that should not kill a
    demo (unknown sku, more stock than fits) become ``SceneSpec.warnings`` instead of exceptions. ``assets``: per-item
    models (unit dims + render mode per SKU; None = primitives everywhere).
    """
    by_sku = products if isinstance(products, dict) else {p.sku: p for p in products}
    sid = planogram.store_id
    root = store_root(sid)
    warnings: list[str] = []
    prims = _store_shell(planogram, root)
    for fx in planogram.fixtures:
        prims += _fixture_prims(fx, root, sid)

    instances: list[ItemInstance] = []
    dims: dict[str, Vec3] = {}
    yaws: dict[str, float] = {}
    layouts: dict[str, SlotLayout] = {}
    stocked: dict[Sku, Product] = {}
    for slot in planogram.slots:
        product = by_sku.get(slot.sku)
        fx = planogram.fixture(slot.fixture_id)
        if product is None:
            warnings.append(f"{slot.slot_id}: unknown sku {slot.sku!r}; slot left empty")
            continue
        if fx is None:  # unreachable with a validated Planogram, kept for safety
            warnings.append(f"{slot.slot_id}: unknown fixture {slot.fixture_id!r}")
            continue
        layout = layout_for(slot, fx, product, assets, slot_lateral_room(planogram, slot, fx))
        layouts[slot.slot_id] = layout
        stocked[product.sku] = product
        n = min(slot.qty, MAX_UNITS_PER_SLOT)
        if slot.qty > MAX_UNITS_PER_SLOT:
            warnings.append(f"{slot.slot_id}: qty {slot.qty} > MAX_UNITS_PER_SLOT; sim shows {n}")
        elif n > len(layout.positions):
            warnings.append(f"{slot.slot_id}: qty {n} > {len(layout.positions)} positions; extra units share back rows")
        for k, (lot_id, lot_fresh) in enumerate(unit_lots(slot.lots, n)):
            iid = instance_id_for(slot.slot_id, k)
            fresh = unit_freshness(lot_fresh, iid)
            prims.append(unit_prim(sid, layout, iid, k, lot_id, fresh))
            instances.append(unit_instance(sid, layout, iid, k, lot_id, fresh))
            dims[iid] = layout.dims
            yaws[iid] = layout.yaw

    return SceneSpec(
        store_id=sid,
        name=planogram.name,
        root_path=root,
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
        slot_layouts=layouts,
        products=stocked,
        warnings=warnings,
    )


# --------------------------------------------------------------------------------------------------------------------
# Rovers + cameras
# --------------------------------------------------------------------------------------------------------------------
def build_rover_spec(robot: RobotSpec, model_usd: str | None = None) -> RoverSpec:
    """Primitive model of the rover + lift + SO-101 + basket (robot-local; see the module docstring). ``model_usd``
    (``picky_rover.usda``) adds the visual-puppet plan: reference path + the primitives kept next to it."""
    root = rover_root(robot.store_id, robot.robot_id)
    color = robot.color or ROVER_DEFAULT_COLOR
    sid = robot.store_id

    def prim(
        name: str, group: RoverGroup, center: Vec3, size: Vec3, col: str, shape: PrimShape = "box", **kw
    ) -> PrimSpec:
        sub = {"base": "Base", "lift": "Lift", "arm": "Lift/Arm"}[group]
        return PrimSpec(
            path=f"{root}/{sub}/{name}",
            role="rover",
            shape=shape,
            center=center,
            size=size,
            color=col,
            collider=False,
            group=group,
            store_id=sid,
            **kw,
        )

    bx, by, bz = BASE_SIZE_M
    zc = BASE_CLEARANCE_M + bz / 2
    prims = [
        prim("chassis", "base", (0.0, 0.0, zc), (bx, by, bz), color),
        prim("bumper", "base", (bx / 2 + 0.01, 0.0, BASE_CLEARANCE_M + 0.04), (0.02, by * 0.9, 0.05), "#222222"),
    ]
    for i, (wx, wy) in enumerate(
        ((0.15, by / 2 + 0.02), (0.15, -by / 2 - 0.02), (-0.15, by / 2 + 0.02), (-0.15, -by / 2 - 0.02))
    ):
        prims.append(
            prim(
                f"wheel_{i}",
                "base",
                (wx, wy, WHEEL_RADIUS_M),
                (2 * WHEEL_RADIUS_M, 0.04, 2 * WHEEL_RADIUS_M),
                "#1f1f1f",
            )
        )
    deck = BASE_CLEARANCE_M + bz
    # lift mast at ARM_MOUNT_X_M with the pan-tilt front camera head on top (DEFAULT_ROBOT_CAMERA_MOUNTS['front'])
    fm = DEFAULT_ROBOT_CAMERA_MOUNTS["front"]
    mast_top = min(MAST_HEIGHT_M, fm.z - MAST_CAMERA_CLEARANCE_M)
    prims.append(
        prim(
            "mast",
            "base",
            (ARM_MOUNT_X_M, 0.0, deck + (mast_top - deck) / 2),
            (0.05, 0.07, mast_top - deck),
            "#d0d4da",
        )
    )
    prims.append(prim("front_cam_head", "base", (fm.x, fm.y, fm.z), (0.07, 0.12, 0.06), "#111111"))
    # neck from the mast top to the head, entirely behind the eye (x < fm.x): the forward-looking front camera never
    # sees it
    neck_x0, neck_x1, head_bottom = ARM_MOUNT_X_M - 0.04, fm.x - 0.005, fm.z - 0.03
    prims.append(
        prim(
            "front_cam_neck",
            "base",
            ((neck_x0 + neck_x1) / 2, fm.y, (mast_top + head_bottom) / 2),
            (neck_x1 - neck_x0, 0.02, head_bottom - mast_top),
            "#d0d4da",
        )
    )
    # basket on a post behind the mast (BASKET_OFFSET_LOCAL = basket floor center)
    kx, ky, kz = BASKET_SIZE_M
    ox, oy, oz = BASKET_OFFSET_M
    prims.append(prim("basket_post", "base", (ox, oy, (deck + oz) / 2), (0.05, 0.05, max(0.01, oz - deck)), "#9aa0a6"))
    prims.append(prim("basket_floor", "base", (ox, oy, oz - 0.005), (kx, ky, 0.01), "#e8562a"))
    wall = 0.01
    for name, c, s in (
        ("basket_front", (ox + kx / 2, oy, oz + kz / 2), (wall, ky, kz)),
        ("basket_back", (ox - kx / 2, oy, oz + kz / 2), (wall, ky, kz)),
        ("basket_left", (ox, oy + ky / 2, oz + kz / 2), (kx, wall, kz)),
        ("basket_right", (ox, oy - ky / 2, oz + kz / 2), (kx, wall, kz)),
    ):
        prims.append(prim(name, "base", c, s, "#e8562a"))
    bm = DEFAULT_ROBOT_CAMERA_MOUNTS["basket"]
    prims.append(prim("basket_cam", "base", (bm.x, bm.y, bm.z), (0.04, 0.04, 0.03), "#111111"))
    # lift carriage + SO-101 (authored at LIFT_REF_M, arm stretched forward; the builder scales the arm group's x by
    # the extension)
    z = LIFT_REF_M
    prims.append(prim("carriage", "lift", (ARM_MOUNT_X_M + 0.04, 0.0, z), (0.06, 0.10, 0.08), "#4b5563"))
    prims.append(prim("so101_base", "arm", (ARM_MOUNT_X_M + 0.08, 0.0, z), (0.07, 0.07, 0.05), "#f5f5f5", "cylinder"))
    x = ARM_MOUNT_X_M + 0.08
    for name, length in SO101_LINKS_M:
        prims.append(prim(f"so101_{name}", "arm", (x + length / 2, 0.0, z + 0.02), (length, 0.035, 0.035), "#f5f5f5"))
        x += length
    prims.append(prim("so101_jaw_fixed", "arm", (x - 0.02, 0.018, z), (0.05, 0.008, 0.04), "#f59e0b"))
    prims.append(prim("so101_jaw_moving", "arm", (x - 0.02, -0.018, z), (0.05, 0.008, 0.04), "#f59e0b"))
    prims.append(prim("so101_wrist_cam", "arm", (x - 0.05, 0.0, z + 0.035), (0.03, 0.03, 0.02), "#111111"))
    puppet_prims = _puppet_prims(prims, prim, color) if model_usd else []
    return RoverSpec(
        robot_id=robot.robot_id,
        store_id=sid,
        name=robot.name,
        color=color,
        root_path=root,
        dock=robot.dock,
        battery_pct=robot.battery_pct,
        basket_capacity=robot.basket_capacity,
        max_speed_mps=robot.max_speed_mps,
        camera_ids=[robot_camera_id(robot.robot_id, k) for k in robot.cameras],
        prims=prims,
        model_usd=model_usd,
        model_path=f"{root}/{puppet.MODEL_PRIM_NAME}/{puppet.MODEL_REF_NAME}" if model_usd else None,
        puppet_prims=puppet_prims,
    )


def _puppet_prims(prims: list[PrimSpec], prim: Any, color: str) -> list[PrimSpec]:
    """Primitives that stay next to the rover USD: the lift column (moved behind the SO-101 to the base center, so the
    folded arm clears it), camera head + neck, basket on its post (both standing on LeKiwi's deck) and a carriage
    bracket from the column to the SO-101 shoulder (lift group, authored at zero lift travel)."""
    by_name = {p.path.rsplit("/", 1)[-1]: p for p in prims}
    fm = DEFAULT_ROBOT_CAMERA_MOUNTS["front"]
    mast_old = by_name["mast"]
    mast_top = mast_old.center[2] + mast_old.size[2] / 2
    mx = puppet.MAST_X_M
    out = [
        prim(
            "mast",
            "base",
            (mx, 0.0, (puppet.DECK_Z_M + mast_top) / 2),
            (0.05, 0.07, mast_top - puppet.DECK_Z_M),
            "#d0d4da",
        ),
        by_name["front_cam_head"],
    ]
    neck_x0, neck_x1, head_bottom = mx - 0.02, fm.x - 0.005, fm.z - 0.03
    out.append(
        prim(
            "front_cam_neck",
            "base",
            ((neck_x0 + neck_x1) / 2, fm.y, (mast_top + head_bottom) / 2),
            (neck_x1 - neck_x0, 0.02, max(0.01, head_bottom - mast_top)),
            "#d0d4da",
        )
    )
    ox, oy, oz = BASKET_OFFSET_M
    post_x = ox + 0.05  # under the basket's front half, on LeKiwi's deck
    out.append(
        prim(
            "basket_post",
            "base",
            (post_x, oy, (puppet.DECK_Z_M + oz) / 2),
            (0.04, 0.04, oz - puppet.DECK_Z_M),
            "#9aa0a6",
        )
    )
    out += [by_name[n] for n in ("basket_floor", "basket_front", "basket_back", "basket_left", "basket_right")]
    out.append(by_name["basket_cam"])
    # bracket from the lift column to the basket camera (it floated in mid-air above the basket)
    bc = by_name["basket_cam"]
    bx0, bx1 = bc.center[0], mx
    out.append(
        prim(
            "basket_cam_arm",
            "base",
            ((bx0 + bx1) / 2, 0.0, bc.center[2] + bc.size[2] / 2 + 0.01),
            (abs(bx1 - bx0), 0.02, 0.02),
            "#d0d4da",
        )
    )
    shoulder_x = ARM_MOUNT_X_M + puppet.ARM_PAN_FORWARD_M
    x0, x1 = mx + 0.025, shoulder_x + 0.02
    out.append(
        prim("carriage", "lift", ((x0 + x1) / 2, 0.0, puppet.SHOULDER_BASE_Z_M - 0.02), (x1 - x0, 0.08, 0.04), color)
    )
    return out


def default_robots(planogram: Planogram) -> list[RobotSpec]:
    """Fallback when data/fixtures/robots.yaml is missing and the gateway pushed no robots: one rover per store,
    'rover-<suffix>-1' (store_ave -> rover-ave-1), docked at the planogram's robot home."""
    suffix = planogram.store_id.removeprefix("store_").replace("_", "-") or "x"
    return [
        RobotSpec(
            robot_id=f"rover-{suffix}-1",
            name=f"{suffix.capitalize()} rover",
            store_id=planogram.store_id,
            dock=planogram.start_pose,
            battery_pct=100.0,
        )
    ]


def default_store_cameras(planogram: Planogram) -> list[StoreCameraSpec]:
    """Fallback ceiling camera: one corner camera looking across the store (used when robots.yaml has none)."""
    b = planogram.bounds
    yaw = math.atan2(b.h, b.w)
    return [
        StoreCameraSpec(
            camera_id=store_camera_id(planogram.store_id, 1),
            store_id=planogram.store_id,
            name=f"{planogram.name} ceiling 1",
            mount=CameraMount(
                frame="world", x=b.x + 0.4, y=b.y + 0.4, z=CEILING_HEIGHT_M - 0.2, yaw=yaw, pitch=-0.45, hfov_deg=100.0
            ),
        )
    ]


def camera_infos(
    robots: Iterable[RobotSpec], store_cameras: Iterable[StoreCameraSpec], source: str = "isaac"
) -> list[CameraInfo]:
    """CameraInfo for every camera (robot cameras first, then ceiling cameras), as listed in SimHello.cameras."""
    out: list[CameraInfo] = []
    for r in robots:
        for kind in r.cameras:
            out.append(
                CameraInfo(
                    camera_id=robot_camera_id(r.robot_id, kind),
                    kind=kind,
                    name=f"{r.name} · {kind}",
                    store_id=r.store_id,
                    robot_id=r.robot_id,
                    mount=DEFAULT_ROBOT_CAMERA_MOUNTS[kind].model_copy(),
                    source=source,  # type: ignore[arg-type]
                )
            )
    for c in store_cameras:
        out.append(
            CameraInfo(
                camera_id=c.camera_id,
                kind="ceiling",
                name=c.name,
                store_id=c.store_id,
                mount=c.mount.model_copy(),
                source=source,  # type: ignore[arg-type]
            )
        )
    return out


def build_world_spec(
    planograms: Sequence[Planogram],
    products: dict[Sku, Product] | Iterable[Product],
    robots: Iterable[RobotSpec] | None = None,
    store_cameras: Iterable[StoreCameraSpec] | None = None,
    seed: int = 0,
    assets: SkuAssets | None = None,
    rover_usd: str | None = None,
) -> WorldSpec:
    """Every store side by side + every rover at its dock + every camera. Robots / cameras default per store
    (``default_robots`` / ``default_store_cameras``) when none are given for that store."""
    by_sku = products if isinstance(products, dict) else {p.sku: p for p in products}
    robots = list(robots or [])
    cams = list(store_cameras or [])
    stores = {p.store_id: build_scene_spec(p, by_sku, seed, assets=assets) for p in planograms}
    order = [p.store_id for p in planograms]
    offsets = store_offsets(order, {p.store_id: (p.bounds.w, p.bounds.h) for p in planograms})
    all_robots: list[RobotSpec] = []
    all_cams: list[StoreCameraSpec] = []
    for p in planograms:
        mine = [r for r in robots if r.store_id == p.store_id] or default_robots(p)
        all_robots += mine
        all_cams += [c for c in cams if c.store_id == p.store_id] or default_store_cameras(p)
    return WorldSpec(
        stores=stores,
        offsets=offsets,
        rovers={r.robot_id: build_rover_spec(r, model_usd=rover_usd) for r in all_robots},
        cameras=camera_infos(all_robots, all_cams),
    )


def load_scene_spec(store_id: str, seed: int = 0, data_root: str | None = None) -> SceneSpec:
    """Build the scene for ``store_id`` from data/ (via utils.data)."""
    from utils.data import load_planogram, load_products

    return build_scene_spec(load_planogram(store_id, data_root), load_products(data_root), seed=seed)


def load_world_spec(
    store_ids: Sequence[str] | None = None,
    seed: int = 0,
    data_root: str | None = None,
    assets: SkuAssets | None = None,
    rover_usd: str | None = None,
) -> WorldSpec:
    """Build every store (default: all planograms in data/) + robots.yaml robots/cameras (defaults if missing)."""
    from utils.data import DataError, list_store_ids, load_planogram, load_products, load_robots_file

    ids = list(store_ids) if store_ids else list_store_ids(data_root)
    plans = [load_planogram(sid, data_root) for sid in ids]
    try:
        rf = load_robots_file(data_root)
        robots, cams = rf.robots, rf.store_cameras
    except (DataError, FileNotFoundError):
        robots, cams = [], []
    return build_world_spec(plans, load_products(data_root), robots, cams, seed, assets=assets, rover_usd=rover_usd)


def main(argv: list[str] | None = None) -> int:
    from arms.sim.assets import rover_usd_from_env

    args = sys.argv[1:] if argv is None else argv
    assets = SkuAssets.from_env()
    world = load_world_spec(args or None, assets=assets, rover_usd=rover_usd_from_env())
    print(assets.describe)
    for sid, spec in world.stores.items():
        roles: dict[str, int] = {}
        for p in spec.prims:
            roles[p.role] = roles.get(p.role, 0) + 1
        off = world.offsets[sid]
        print(f"{sid} ({spec.name}) at {spec.root_path} offset={off} hash={spec.data_hash[:12]}")
        print(
            f"  bounds {spec.bounds.w}x{spec.bounds.h} m; prims: "
            + ", ".join(f"{k}={v}" for k, v in sorted(roles.items()))
        )
        print(f"  instances: {len(spec.instances)} across {len({i.slot_id for i in spec.instances})} slots")
        modeled = sum(1 for p in spec.product_prims() if p.render != "primitive")
        print(f"  units on per-item models: {modeled} / {len(spec.product_prims())}")
        for w in spec.warnings:
            print(f"  warning: {w}")
    for r in world.rovers.values():
        model = f" model={r.model_usd}" if r.model_usd else ""
        print(
            f"rover {r.robot_id} ({r.name}) in {r.store_id} dock=({r.dock.x}, {r.dock.y}) prims={len(r.prims)}{model}"
        )
    print("cameras: " + ", ".join(c.camera_id for c in world.cameras))
    return 0


if __name__ == "__main__":
    sys.exit(main())
