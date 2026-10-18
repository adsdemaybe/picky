"""Genie Sim supermarket -> Picky store ``store_genie`` (planogram + SKUs + sim prim map). Repo tooling, CLAUDE.md §4
rule 5: the three outputs are GENERATED from the scene dump, never hand-edited; fix this script or its input instead.

The AgiBot Genie Sim ``supermarket_06`` scene (``background.usda``, ~1,100 product prims on wooden / wire shelving) is a
real, fully stocked store. This script turns a JSON dump of it into Picky data so the existing backend (fleet, agent,
routing, robot, perception) shops it like ``store_ave`` and the Isaac worker renders the real scene instead of
primitive shelves (``arms/sim`` external-scene mode reads the prim map):

    # 1. dump (needs pxr, e.g. Isaac's python or a usd-core venv; run where the scene lives)
    python scripts/genie_to_planogram.py dump <geniesim>/background/supermarket/supermarket_06/background.usda \\
        data/sim/genie/supermarket_06.json [--heights RUN/heights.npy --topdown-meta RUN/plan.json]
    # 2. generate (base deps only): writes the three files below from the committed dump
    uv run python scripts/genie_to_planogram.py gen        # --check: exit 1 if a generated file is stale

Outputs:
- ``data/planograms/store_genie.yaml``: bounds, fixtures (every shelf / freezer box as a rect, levels from the product
  bottoms), aisles (one per shelf section), slots (one per run of one product type on one shelf level and face, with
  ``item_position`` on the front face of its front unit and an ``approach_pose`` 0.375 m out), checkout counter,
  entrance / robot home. Store frame = USD frame shifted by ``ORIGIN_USD`` (translation only, contracts/planogram.py).
- ``data/products.yaml``: the block between the ``store_genie`` markers, one fictional SKU per Genie product TYPE
  (asset id) that has a reachable slot (made-up brands, realistic US prices in cents; hand-written naming table
  ``data/sim/genie/store_genie_skus.yaml``).
- ``data/sim/store_genie_prims.yaml``: sim-only, ``slot_id -> units`` (unit n of the slot = units[n], front unit
  first: product prim path + its box in the store frame), the USD to reference and where, the fixtures the USD already
  draws. No contract change needed.

Input ``data/sim/genie/supermarket_06.json`` (the dump): product prims (path relative to the USD's default prim,
asset id, world-aligned box), fixture boxes, the building box, Genie's own object descriptions, and ``obstacles``
(building geometry the USD boxes miss, measured from a top-down depth render: a pillar, a wall ledge).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STORE_ID = "store_genie"
SLOT_PREFIX = "GEN"
DUMP = ROOT / "data/sim/genie/supermarket_06.json"
SKUS = ROOT / "data/sim/genie/store_genie_skus.yaml"
PLANOGRAM = ROOT / f"data/planograms/{STORE_ID}.yaml"
PRODUCTS = ROOT / "data/products.yaml"
PRIMS = ROOT / f"data/sim/{STORE_ID}_prims.yaml"
USD_REL = "background/supermarket/supermarket_06/background.usda"
"""The scene USD relative to the Genie Sim asset root (``PICKY_GENIESIM_ROOT`` on the worker host)."""
BEGIN = f"# --- BEGIN generated: {STORE_ID} (scripts/genie_to_planogram.py; do not hand-edit) ---"
END = f"# --- END generated: {STORE_ID} ---"

ORIGIN_USD = (-3.25, -2.10)
"""USD (x, y) of the store origin (bottom-left corner): store = USD - ORIGIN_USD. Fits every fixture of the scene."""
SIZE = (8.45, 6.75)
"""Store outline (w, h) in meters: the walkable floor inside the walls (north wall face at USD y = 4.65)."""
APPROACH_M = 0.375
"""Approach pose distance from the slot's item_position (design range 0.35-0.40 m, contracts.common)."""
APPROACH_MAX_M = 0.40
FRONT_DEPTH_MAX_M = 0.15
"""A run whose front unit sits deeper than this behind the fixture face is not a slot (the arm could not reach it)."""
RECESS_MAX_M = 0.09
"""item_position sits on the front unit's face, at most this far behind the fixture's face (validator: rect + 0.1 m)."""
ROBOT_RADIUS_M = 0.3
CLEARANCE_EPS = 0.005
REACH_Z = (0.1, 1.15)
"""Shelf boards the robot picks from: contracts.common allows 0.1-1.5 m, but the white SO-101 rover v3 that drives this
store in Isaac has a fixed riser (no lift) and its reach table tops out at a 1.2 m TCP, so top boards stay scenery."""
LEVEL_GAP_M = 0.06
"""Product bottoms closer than this on one fixture are the same shelf board."""
SLOT_MAX_WIDTH_M = 0.5
"""A run of one product type wider than this is split into several slots (each slot's units stay near its pose)."""
SLOT_GAP_M = 0.12
"""Lateral gap (between unit boxes) that starts a new slot."""
DEPTH_MAX_M = 0.6
CAPACITY_MIN = 12
OPEN_SIDES: dict[str, tuple[str, ...]] = {
    # closed-back gondolas of the north row (arms/sim/supermarket/demo/so101_config.json "open_sides" S1-S3)
    "background/supermarket_shelf_002_02": ("south",),
    "background/supermarket_shelf_03": ("south",),
    "background/Group_11/benchmark_supermarket_shelf_022": ("south",),
    "background/Group_12/benchmark_supermarket_shelf_022": ("south",),
    "background/Group_13/benchmark_supermarket_shelf_022": ("south",),
    # the two deep west-wall units form one 1.1 x 1.7 m block against the wall: open to the east only
    "background/Group_05/benchmark_supermarket_shelf_22": ("east",),
    "background/Group_08/benchmark_supermarket_shelf_22": ("east",),
}
LONG_FACE_RATIO = 1.5
"""Fixtures longer than this ratio are picked from their long faces only (gondola ends carry panels, not stock)."""
SIDES = {"west": (-1.0, 0.0), "east": (1.0, 0.0), "south": (0.0, -1.0), "north": (0.0, 1.0)}
CHECKOUT_RECT_USD = (0.9, 4.0, 2.9, 4.5)
"""Checkout counter (x0, y0, x1, y1 in USD): not part of the Genie scene, the worker draws it (as so101_demo does)."""
CHECKOUT_TOP_M = 0.76
ENTRANCE_STORE = (6.80, 5.40, -math.pi / 2)
"""Entrance / robot home: where the SO-101 demo spawns its rover (USD (3.55, 3.30), facing south)."""


# --------------------------------------------------------------------------------------------------------------------
# The catalog: one fictional SKU per Genie product type (asset id). Brands are made up (CLAUDE.md §8).
# --------------------------------------------------------------------------------------------------------------------
"""Toys and stationery (``benchmark_toy_*``, ``benchmark_stationery_*``) have no Picky category: left as scenery."""
SHELF_LIFE_DAYS = {"bakery": 7, "dairy": 21, "beverages": 120}


def load_sku_table(path: Path = SKUS) -> dict[str, tuple]:
    """``data/sim/genie/store_genie_skus.yaml`` -> asset id -> row tuple (``_ROW_KEYS`` order)."""
    import yaml  # noqa: PLC0415

    out: dict[str, tuple] = {}
    for obj, r in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
        cat, sub = str(r["category"]).split("/")
        out[obj] = (
            r["sku"], r["name"], r["brand"], cat, sub, int(r["price_cents"]), r["unit"], str(r["size"]), r["shape"],
            r["color"], list(r.get("tags") or []), list(r.get("allergens") or []), bool(r.get("perishable")),
        )  # fmt: skip
    return out


# --------------------------------------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class Box:
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]

    @property
    def c(self) -> tuple[float, float, float]:
        return tuple((a + b) / 2 for a, b in zip(self.lo, self.hi, strict=True))  # type: ignore[return-value]


@dataclass
class Unit:
    path: str
    obj: str
    box: Box
    fixture: int = -1
    side: str = ""
    level: int = -1
    below: int = -1


@dataclass
class Fix:
    path: str
    kind: str
    box: Box
    fid: str = ""
    sides: set[str] = field(default_factory=set)
    levels: list[float] = field(default_factory=list)
    section: str = ""


def to_store(x: float, y: float) -> tuple[float, float]:
    return x - ORIGIN_USD[0], y - ORIGIN_USD[1]


def store_box(lo: list[float], hi: list[float]) -> Box:
    (x0, y0), (x1, y1) = to_store(lo[0], lo[1]), to_store(hi[0], hi[1])
    return Box((x0, y0, lo[2]), (x1, y1, hi[2]))


def rect_dist(rect: tuple[float, float, float, float], x: float, y: float) -> float:
    x0, y0, x1, y1 = rect
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def r3(v: float) -> float:
    return round(float(v) + 0.0, 3)


def ean13(first12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return first12 + str((10 - total % 10) % 10)


# --------------------------------------------------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------------------------------------------------
class Genie:
    def __init__(self, dump: dict[str, Any]) -> None:
        self.dump = dump
        self.bounds = (0.0, 0.0, SIZE[0], SIZE[1])
        self.fixtures = [Fix(f["path"], f["kind"], store_box(f["lo"], f["hi"])) for f in dump["fixtures"]]
        self.fixtures.sort(key=lambda f: (round(f.box.lo[1], 2), round(f.box.lo[0], 2)))
        self.obstacles = [
            (*to_store(o["lo"][0], o["lo"][1]), *to_store(o["hi"][0], o["hi"][1])) for o in dump.get("obstacles", [])
        ]
        cx0, cy0 = to_store(CHECKOUT_RECT_USD[0], CHECKOUT_RECT_USD[1])
        cx1, cy1 = to_store(CHECKOUT_RECT_USD[2], CHECKOUT_RECT_USD[3])
        self.counter = (cx0, cy0, cx1, cy1)
        self.units = [Unit(p["path"], p["object"], store_box(p["lo"], p["hi"])) for p in dump["products"]]
        self.warnings: list[str] = []

    # ---- obstacles for clearance checks (fixtures + counter + extra obstacles + store outline)
    def rects(self) -> list[tuple[float, float, float, float]]:
        out = [(f.box.lo[0], f.box.lo[1], f.box.hi[0], f.box.hi[1]) for f in self.fixtures]
        return [*out, self.counter, *self.obstacles]

    def clear(self, x: float, y: float) -> bool:
        b = self.bounds
        r = ROBOT_RADIUS_M + CLEARANCE_EPS
        if not (b[0] + r <= x <= b[2] - r and b[1] + r <= y <= b[3] - r):
            return False
        return all(rect_dist(rc, x, y) >= r for rc in self.rects())

    @staticmethod
    def long_faces(f: Fix) -> tuple[str, ...]:
        w, h = f.box.hi[0] - f.box.lo[0], f.box.hi[1] - f.box.lo[1]
        if w > LONG_FACE_RATIO * h:
            return ("south", "north")
        if h > LONG_FACE_RATIO * w:
            return ("west", "east")
        return tuple(SIDES)

    @staticmethod
    def face(f: Fix, side: str) -> float:
        return {"west": f.box.lo[0], "east": f.box.hi[0], "south": f.box.lo[1], "north": f.box.hi[1]}[side]

    @staticmethod
    def lateral(side: str, x: float, y: float) -> float:
        return y if side in ("west", "east") else x

    def _front(self, u: Unit, side: str) -> float:
        """Coordinate of the unit's face toward ``side`` (along the side's normal axis)."""
        b = u.box
        return {"west": b.lo[0], "east": b.hi[0], "south": b.lo[1], "north": b.hi[1]}[side]

    def _depth(self, u: Unit, f: Fix, side: str) -> float:
        nx, ny = SIDES[side]
        return (self.face(f, side) - self._front(u, side)) * (nx + ny)

    def approach(self, side: str, front: float, lat: float, dist: float = APPROACH_M) -> tuple[float, float, float]:
        nx, ny = SIDES[side]
        if side in ("west", "east"):
            x, y = front + nx * dist, lat
        else:
            x, y = lat, front + ny * dist
        return x, y, math.atan2(-ny, -nx)

    # ---- products -> fixture + face
    def assign(self) -> None:
        for u in self.units:
            cx, cy, cz = u.box.c
            best: tuple[float, int] | None = None
            for i, f in enumerate(self.fixtures):
                b = f.box
                inside = b.lo[0] - 0.08 <= cx <= b.hi[0] + 0.08 and b.lo[1] - 0.08 <= cy <= b.hi[1] + 0.08
                if inside and b.lo[2] - 0.1 <= cz <= b.hi[2] + 0.3:
                    area = (b.hi[0] - b.lo[0]) * (b.hi[1] - b.lo[1])
                    if best is None or area < best[0]:
                        best = (area, i)
            if best is None:
                continue
            f = self.fixtures[best[1]]
            allowed = OPEN_SIDES.get(f.path, self.long_faces(f))
            options = []
            for side in allowed:
                lat = self.lateral(side, cx, cy)
                ax, ay, _ = self.approach(side, self.face(f, side), lat)
                if self.clear(ax, ay):
                    options.append((self._depth(u, f, side), side))
            if not options:
                continue
            depth, side = min(options)
            if depth > DEPTH_MAX_M:
                continue
            u.fixture, u.side = best[1], side
            f.sides.add(side)

    # ---- shelf boards (levels) per fixture from the product bottoms; stacked units ride the unit below
    def levels(self) -> None:
        by_fix: dict[int, list[Unit]] = defaultdict(list)
        for u in self.units:
            if u.fixture >= 0:
                by_fix[u.fixture].append(u)
        for fi, units in by_fix.items():
            for u in units:  # the unit directly below (stacked packs)
                for j, o in enumerate(units):
                    if o is u or abs(o.box.hi[2] - u.box.lo[2]) > 0.03:
                        continue
                    ox = min(u.box.hi[0], o.box.hi[0]) - max(u.box.lo[0], o.box.lo[0])
                    oy = min(u.box.hi[1], o.box.hi[1]) - max(u.box.lo[1], o.box.lo[1])
                    if ox > 0.3 * (u.box.hi[0] - u.box.lo[0]) and oy > 0.3 * (u.box.hi[1] - u.box.lo[1]):
                        u.below = j
                        break
            bottoms = sorted(u.box.lo[2] for u in units if u.below < 0)
            clusters: list[list[float]] = []
            for z in bottoms:
                if clusters and z - clusters[-1][-1] <= LEVEL_GAP_M:
                    clusters[-1].append(z)
                else:
                    clusters.append([z])
            f = self.fixtures[fi]
            f.levels = [max(0.0, sum(c) / len(c)) for c in clusters]
            for u in units:
                base = u
                seen = 0
                while base.below >= 0 and seen < 10:
                    base = units[base.below]
                    seen += 1
                z = base.box.lo[2]
                u.level = min(range(len(f.levels)), key=lambda k: abs(f.levels[k] - z))

    # ---- sections (aisles): adjacent fixtures with the same dominant category on the same face
    def sections(self, cat_of: dict[str, str]) -> None:
        dom: dict[int, str] = {}
        for i, _f in enumerate(self.fixtures):
            cats: dict[str, int] = defaultdict(int)
            for u in self.units:
                if u.fixture == i and u.obj in cat_of:
                    cats[cat_of[u.obj]] += 1
            if cats:
                dom[i] = max(cats.items(), key=lambda kv: (kv[1], kv[0]))[0]
        parent = list(range(len(self.fixtures)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in dom:
            for j in dom:
                if j <= i or dom[i] != dom[j]:
                    continue
                a, b = self.fixtures[i].box, self.fixtures[j].box
                gx = max(0.0, a.lo[0] - b.hi[0], b.lo[0] - a.hi[0])
                gy = max(0.0, a.lo[1] - b.hi[1], b.lo[1] - a.hi[1])
                if gx < 0.15 and gy < 0.15:
                    parent[find(i)] = find(j)
        groups: dict[int, list[int]] = defaultdict(list)
        for i in dom:
            groups[find(i)].append(i)
        self.section_list: list[dict[str, Any]] = []
        ordered = sorted(
            groups.values(),
            key=lambda g: (min(self.fixtures[i].box.lo[1] for i in g), min(self.fixtures[i].box.lo[0] for i in g)),
        )  # noqa: E501
        for k, idxs in enumerate(ordered):
            sid = f"genie-s{k + 1}"
            for i in idxs:
                self.fixtures[i].section = sid
            self.section_list.append({"aisle_id": sid, "fixtures": idxs, "category": dom[idxs[0]]})

    # ---- slots
    def slots(self, skus: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[tuple[int, str, int, str], list[Unit]] = defaultdict(list)
        for u in self.units:
            if u.fixture >= 0 and u.level >= 0 and u.obj in skus:
                groups[(u.fixture, u.side, u.level, u.obj)].append(u)
        raw: list[dict[str, Any]] = []
        for (fi, side, level, obj), units in sorted(groups.items()):
            f = self.fixtures[fi]

            # lateral runs: a new slot at a gap or when the run gets too wide
            def lat_span(u: Unit, side: str = side) -> tuple[float, float]:
                if side in ("west", "east"):
                    return u.box.lo[1], u.box.hi[1]
                return u.box.lo[0], u.box.hi[0]

            ordered = sorted(units, key=lambda u: lat_span(u)[0])
            runs: list[list[Unit]] = []
            run_lo = run_hi = 0.0
            for u in ordered:
                lo, hi = lat_span(u)
                if runs and lo - run_hi <= SLOT_GAP_M and hi - run_lo <= SLOT_MAX_WIDTH_M:
                    runs[-1].append(u)
                    run_hi = max(run_hi, hi)
                else:
                    runs.append([u])
                    run_lo, run_hi = lo, hi
            for run in runs:
                lats = [sum(lat_span(u)) / 2 for u in run]
                mid = (min(lat_span(u)[0] for u in run) + max(lat_span(u)[1] for u in run)) / 2

                # rank: front units first, then the one nearest the run's middle, bottom layer before stacked
                def rank(u: Unit, f: Fix = f, side: str = side, mid: float = mid) -> tuple[float, float, float]:
                    return (round(self._depth(u, f, side), 2), round(abs(sum(lat_span(u)) / 2 - mid), 3), u.box.lo[2])

                run.sort(key=rank)
                front = run[0]
                lat = sum(lat_span(front)) / 2
                nx, ny = SIDES[side]
                front_coord = self._front(front, side)
                # item_position on the front face of the front unit, kept on the fixture (validator: rect + 0.1 m)
                face = self.face(f, side)
                if self._depth(front, f, side) > FRONT_DEPTH_MAX_M:
                    self.warnings.append(f"{obj} on {f.fid} {side} L{level}: front unit too deep, skipped")
                    continue
                recess = min(max(self._depth(front, f, side), 0.0), RECESS_MAX_M)
                front_coord = face - (nx + ny) * recess
                # the base keeps ROBOT_RADIUS_M off the fixture face: a recessed front unit gets a longer approach
                dist = min(APPROACH_MAX_M, max(APPROACH_M, recess + ROBOT_RADIUS_M + 2 * CLEARANCE_EPS))
                ax, ay, th = self.approach(side, front_coord, lat, dist)
                z = f.levels[level]
                if not self.clear(ax, ay):
                    self.warnings.append(f"{obj} on {f.fid} {side} L{level}: approach pose not clear, skipped")
                    continue
                if not REACH_Z[0] <= z <= REACH_Z[1]:
                    continue
                if side in ("west", "east"):
                    ix, iy = front_coord, lat
                else:
                    ix, iy = lat, front_coord
                raw.append(
                    {
                        "fixture": fi,
                        "side": side,
                        "level": level,
                        "obj": obj,
                        "units": run,
                        "item": (ix, iy, z),
                        "approach": (ax, ay, th),
                        "lat": lat,
                        "lats": lats,
                    }
                )
        return raw


def build(
    dump: dict[str, Any], sku_table: dict[str, tuple] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str]]:
    g = Genie(dump)
    for i, f in enumerate(g.fixtures):
        name = f.path.rsplit("/", 1)[-1]
        kind = "FRZ" if f.kind == "freezer" else "SHELF"
        f.fid = f"GEN-{kind}-{i + 1:02d}"
        del name
    table = load_sku_table() if sku_table is None else sku_table
    skus = {obj: row for obj, row in table.items() if obj in dump["objects"]}
    sku_rows = {obj: dict(zip(_ROW_KEYS, row, strict=True)) for obj, row in skus.items()}
    g.assign()
    g.levels()
    g.sections({obj: r["category"] for obj, r in sku_rows.items()})
    raw = g.slots(sku_rows)
    used = sorted({s["obj"] for s in raw}, key=lambda o: list(table).index(o))

    # ---- products (one per used object)
    products = []
    for n, obj in enumerate(used):
        r = sku_rows[obj]
        size = dump["objects"][obj].get("size") or [0.08, 0.15, 0.08]
        dims = [r3(size[0]), r3(size[2]), r3(size[1])]  # Genie assets are y-up: (x, z, y) -> width, depth, height
        cat, sub = r["category"], r["subcategory"]
        desc = (dump["objects"][obj].get("description") or "").strip()
        if not desc.isascii():
            desc = ""
        prod: dict[str, Any] = {
            "sku": r["sku"],
            "name": r["name"],
            "brand": r["brand"],
            "category": cat,
            "subcategory": sub,
            "description": f"{r['brand']} {r['name'].lower()}, {r['size']}." if not desc else _desc(desc),
            "tags": [*r["tags"], "genie_store"],
            "dietary": [t for t in r["tags"] if t in ("vegan", "vegetarian", "gluten_free", "dairy_free")],
            "allergens": r["allergens"],
            "price_cents": r["price"],
            "unit": r["unit"],
            "size": r["size"],
            "barcode": ean13(f"2000009{n + 1:05d}"),
            "perishable": r["perishable"],
        }
        if r["perishable"]:
            prod["shelf_life_days"] = SHELF_LIFE_DAYS.get(cat, 30)
        prod["asset"] = {
            "shape": r["shape"],
            "dims_m": dims,
            "label_color": r["color"],
            "mass_kg": 0.4,
        }
        products.append(prod)
    sku_of = {obj: sku_rows[obj]["sku"] for obj in used}

    # ---- fixtures, aisles, slots
    sec_label = _section_labels(g)
    fixtures_out = []
    for f in g.fixtures:
        b = f.box
        sides = sorted(f.sides)
        yaw_side = _yaw_side(f, g)
        double = len(sides) >= 2 and any(
            SIDES[a][0] == -SIDES[c][0] and SIDES[a][1] == -SIDES[c][1] for a in sides for c in sides
        )  # noqa: E501
        levels = [r3(z) for z in (f.levels or [0.05])]
        fixtures_out.append(
            {
                "fixture_id": f.fid,
                "kind": "freezer" if f.kind == "freezer" else "shelf",
                "label": sec_label.get(f.section, {}).get("label")
                or ("Freezer (empty)" if f.kind == "freezer" else "Shelf"),  # noqa: E501
                **({"aisle_id": f.section} if f.section else {}),
                "rect": {"x": r3(b.lo[0]), "y": r3(b.lo[1]), "w": r3(b.hi[0] - b.lo[0]), "h": r3(b.hi[1] - b.lo[1])},
                "yaw": round(math.atan2(SIDES[yaw_side][1], SIDES[yaw_side][0]), 3),
                "double_sided": bool(double),
                "height_m": r3(max(b.hi[2], max(levels) + 0.05)),
                "levels": len(levels),
                "level_z_m": levels,
            }
        )
    cx0, cy0, cx1, cy1 = g.counter
    fixtures_out.append(
        {
            "fixture_id": "GEN-COUNTER",
            "kind": "checkout",
            "label": "Checkout counter",
            "rect": {"x": r3(cx0), "y": r3(cy0), "w": r3(cx1 - cx0), "h": r3(cy1 - cy0)},
            "yaw": round(-math.pi / 2, 3),
            "double_sided": False,
            "height_m": CHECKOUT_TOP_M,
            "levels": 1,
            "level_z_m": [CHECKOUT_TOP_M],
        }
    )
    slots_out: list[dict[str, Any]] = []
    prim_map: dict[str, list[dict[str, Any]]] = {}
    per_fix_index: dict[tuple[int, str], int] = defaultdict(int)
    for s in sorted(raw, key=lambda s: (s["fixture"], s["side"], -s["level"], s["lat"])):
        f = g.fixtures[s["fixture"]]
        key = (s["fixture"], s["side"])
        idx = per_fix_index[key]
        per_fix_index[key] += 1
        side_code = s["side"][0].upper()
        slot_id = f"{SLOT_PREFIX}-{f.fid.split('-', 1)[1]}-{side_code}{s['level']}-{idx:02d}"
        sku = sku_of[s["obj"]]
        qty = len(s["units"])
        ix, iy, iz = s["item"]
        ax, ay, th = s["approach"]
        slot = {
            "slot_id": slot_id,
            "fixture_id": f.fid,
            **({"aisle_id": f.section} if f.section else {}),
            "sku": sku,
            "level": s["level"],
            "index": idx,
            "item_position": {"x": r3(ix), "y": r3(iy), "z": r3(iz)},
            "approach_pose": {"x": r3(ax), "y": r3(ay), "theta": round(th, 3)},
            "facings": max(1, len({round(v, 2) for v in s["lats"]})),
            "capacity": max(CAPACITY_MIN, qty),
            "qty": qty,
            "lots": [{"lot_id": f"gen-{slot_id.lower()}-l1", "qty": qty}],
        }
        slots_out.append(slot)
        prim_map[slot_id] = [
            {
                "path": u.path,
                "c": [r3(v) for v in u.box.c],
                "s": [r3(hi - lo) for lo, hi in zip(u.box.lo, u.box.hi, strict=True)],
            }
            for u in s["units"]
        ]

    aisles_out = []
    for sec in g.section_list:
        sid = sec["aisle_id"]
        poses = [s["approach_pose"] for s in slots_out if s.get("aisle_id") == sid]
        if not poses:
            for f in fixtures_out:
                if f.get("aisle_id") == sid:
                    del f["aisle_id"]
            continue
        x0 = max(0.0, min(p["x"] for p in poses) - 0.3)
        y0 = max(0.0, min(p["y"] for p in poses) - 0.3)
        x1 = min(SIZE[0], max(p["x"] for p in poses) + 0.3)
        y1 = min(SIZE[1], max(p["y"] for p in poses) + 0.3)
        cats = sorted(
            {
                next(p["category"] for p in products if p["sku"] == s["sku"])
                for s in slots_out
                if s.get("aisle_id") == sid
            }
        )  # noqa: E501
        aisles_out.append(
            {
                "aisle_id": sid,
                "label": sec_label[sid]["label"],
                "rect": {"x": r3(x0), "y": r3(y0), "w": r3(x1 - x0), "h": r3(y1 - y0)},
                "categories": cats,
                "fixture_ids": [g.fixtures[i].fid for i in sec["fixtures"]],
            }
        )

    ex, ey, eth = ENTRANCE_STORE
    checkout_x = (cx0 + cx1) / 2
    planogram = {
        "schema_version": 1,
        "store_id": STORE_ID,
        "name": "Picky Market - Genie Sim Supermarket",
        "address": "AgiBot Genie Sim supermarket_06 (fictional demo store, simulated only)",
        "bounds": {"x": 0.0, "y": 0.0, "w": SIZE[0], "h": SIZE[1]},
        "entrance": {"x": ex, "y": ey, "theta": round(eth, 3)},
        "checkout": {"x": r3(checkout_x), "y": r3(cy0 - 0.37), "theta": round(math.pi / 2, 3)},
        "robot_home": {"x": ex, "y": ey, "theta": round(eth, 3)},
        "obstacles": [{"x": r3(o[0]), "y": r3(o[1]), "w": r3(o[2] - o[0]), "h": r3(o[3] - o[1])} for o in g.obstacles],
        "fixtures": fixtures_out,
        "aisles": aisles_out,
        "slots": slots_out,
    }
    used_fixtures = sorted(f.fid for f in g.fixtures)
    prims = {
        "schema": "picky external scene v1",
        "store_id": STORE_ID,
        "usd": {
            "root_env": "PICKY_GENIESIM_ROOT",
            "path": USD_REL,
            "default_prim": dump.get("default_prim", "/World"),
            "translate": [-ORIGIN_USD[0], -ORIGIN_USD[1], 0.0],
        },
        "fixtures_in_usd": used_fixtures,
        "slots": prim_map,
    }
    return planogram, products, prims, g.warnings


_ROW_KEYS = (
    "sku",
    "name",
    "brand",
    "category",
    "subcategory",
    "price",
    "unit",
    "size",
    "shape",
    "color",
    "tags",
    "allergens",
    "perishable",
)


def _desc(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(Chinese|Japanese) (text|characters|labeling|branding)", "printed text", text)
    return text if len(text) <= 200 else text[:197].rstrip() + "..."


def _yaw_side(f: Fix, g: Genie) -> str:
    counts: dict[str, int] = defaultdict(int)
    idx = g.fixtures.index(f)
    for u in g.units:
        if u.fixture == idx:
            counts[u.side] += 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    for side in ("south", "north", "west", "east"):
        lat = (f.box.lo[1] + f.box.hi[1]) / 2 if side in ("west", "east") else (f.box.lo[0] + f.box.hi[0]) / 2
        ax, ay, _ = g.approach(side, g.face(f, side), lat)
        if g.clear(ax, ay):
            return side
    return "south"


CATEGORY_LABEL = {
    "household": "Cleaning & paper goods",
    "bakery": "Bakery",
    "snacks": "Snacks",
    "beverages": "Drinks",
    "personal_care": "Personal care",
    "dairy": "Dairy drinks",
    "pantry": "Pantry",
}


def _section_labels(g: Genie) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    xs = [f.box.lo[0] for f in g.fixtures] + [f.box.hi[0] for f in g.fixtures]
    ys = [f.box.lo[1] for f in g.fixtures] + [f.box.hi[1] for f in g.fixtures]
    mid = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    span = ((max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2)
    for sec in g.section_list:
        idxs = sec["fixtures"]
        lo = (min(g.fixtures[i].box.lo[0] for i in idxs), min(g.fixtures[i].box.lo[1] for i in idxs))
        hi = (max(g.fixtures[i].box.hi[0] for i in idxs), max(g.fixtures[i].box.hi[1] for i in idxs))
        c = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2)
        rel = ((c[0] - mid[0]) / span[0], (c[1] - mid[1]) / span[1])
        ns = "north" if rel[1] > 0.4 else "south" if rel[1] < -0.4 else ""
        ew = "east" if rel[0] > 0.4 else "west" if rel[0] < -0.4 else ""
        near_wall = lo[0] < 0.6 or lo[1] < 0.6 or SIZE[0] - hi[0] < 0.6 or SIZE[1] - hi[1] < 0.6
        where = (ns + ew) or "center"
        where += " wall" if near_wall else (" row" if ns and not ew else " island")
        out[sec["aisle_id"]] = {"label": f"{CATEGORY_LABEL.get(sec['category'], sec['category'].title())} ({where})"}
    return out


# --------------------------------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------------------------------
PLANOGRAM_HEADER = (
    "# Picky demo data (DubHacks 2026). Schema: contracts/planogram.py. Validate: uv run python -m utils.data\n"
    """# GENERATED by scripts/genie_to_planogram.py from data/sim/genie/ (scene dump + naming table); do not hand-edit
# (CLAUDE.md §4 rule 5): fix the generator or its inputs, then `uv run python scripts/genie_to_planogram.py gen`.
# Fourth store: the AgiBot Genie Sim supermarket_06 scene (real shelving, ~1,100 product meshes) as a Picky store.
# Store frame = USD frame shifted by (+3.25, +2.10) m (translation only). One slot per run of one product type on one
# shelf board and face; item_position = front face of the front unit; approach pose 0.375 m out, facing the shelf.
# The checkout counter is drawn by the sim (not in the scene). Sim prim map: data/sim/store_genie_prims.yaml.
"""
)
PRIMS_HEADER = """# Sim-only: which Genie Sim product prim is which unit of which store_genie slot (arms/sim external
# scene mode).
# GENERATED by scripts/genie_to_planogram.py; do not hand-edit. Unit n of a slot = units[n] (front unit first), the
# same order as contracts.sim.instance_id(slot_id, n): path relative to the USD's default prim, c = box center and
# s = world-aligned box size (store frame, meters; the sim's ground truth for that unit). The worker
# references <PICKY_GENIESIM_ROOT>/<usd.path> under the store root, translated by usd.translate (USD -> store frame),
# and draws only the fixtures NOT listed in fixtures_in_usd (the checkout counter). Without the USD it falls back to
# the planogram's primitive shelves.
"""


def _dump(obj: Any) -> str:
    import yaml  # noqa: PLC0415 - the pxr dump runs in pythons without pyyaml

    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=None, width=120, allow_unicode=True)


def render(dump: dict[str, Any], products_text: str) -> dict[Path, str]:
    planogram, products, prims, warnings = build(dump)
    for w in warnings:
        print(f"  warning: {w}", file=sys.stderr)
    plan_txt = PLANOGRAM_HEADER + _dump(planogram)
    prims_txt = PRIMS_HEADER + _dump(prims)
    block = BEGIN + "\n" + _dump(products).rstrip("\n") + "\n" + END + "\n"
    if BEGIN in products_text:
        head, rest = products_text.split(BEGIN, 1)
        tail = rest.split(END + "\n", 1)[1] if END + "\n" in rest else ""
        new_products = head + block + tail
    else:
        new_products = products_text.rstrip("\n") + "\n" + block
    return {PLANOGRAM: plan_txt, PRIMS: prims_txt, PRODUCTS: new_products}


def cmd_gen(args: argparse.Namespace) -> int:
    dump = json.loads(Path(args.dump).read_text(encoding="utf-8"))
    outputs = render(dump, PRODUCTS.read_text(encoding="utf-8"))
    stale = [p for p, txt in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != txt]
    if args.check:
        for p in stale:
            print(f"stale: {p.relative_to(ROOT)} (run: uv run python scripts/genie_to_planogram.py gen)")
        return 1 if stale else 0
    for p, txt in outputs.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    import yaml  # noqa: PLC0415

    plan = yaml.safe_load(outputs[PLANOGRAM])
    units = sum(s["qty"] for s in plan["slots"])
    print(
        f"{STORE_ID}: {len(plan['fixtures'])} fixtures, {len(plan['aisles'])} aisles, {len(plan['slots'])} slots, "
        f"{len({s['sku'] for s in plan['slots']})} SKUs, {units} units -> {', '.join(str(p.relative_to(ROOT)) for p in outputs)}"  # noqa: E501
    )
    return 0


# --------------------------------------------------------------------------------------------------------------------
# Dump (pxr): the scene -> JSON
# --------------------------------------------------------------------------------------------------------------------
FIXTURE_KEYS = ("shelf", "freezer", "cart", "counter", "fridge", "checkout")
PRODUCT_RE = re.compile(r"^(?:benchmark|genie)_[a-z_]+_\d+")


def obstacles_from_heightmap(heights_path: str, meta: dict[str, float], dump: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocked pixels of a top-down height map (``arms/sim/supermarket/demo/autopilot.topdown``) that no product /
    fixture box explains and that lie inside the store outline -> obstacle boxes (pillars, ledges). numpy + scipy."""
    import numpy as np  # noqa: PLC0415
    from scipy import ndimage  # noqa: PLC0415

    h = np.load(heights_path).astype(np.float32)
    n = h.shape[0]
    cc, rr = np.meshgrid(np.arange(n), np.arange(n))
    xs = meta["x0"] + (cc + 0.5) * meta["px"]
    ys = meta["y1"] - (rr + 0.5) * meta["px"]
    blocked = (~np.isfinite(h)) | (h > 0.10)
    covered = np.zeros_like(blocked)
    for p in [*dump["products"], *dump["fixtures"]]:
        lo, hi = p["lo"], p["hi"]
        covered |= (xs >= lo[0] - 0.03) & (xs <= hi[0] + 0.03) & (ys >= lo[1] - 0.03) & (ys <= hi[1] + 0.03)
    x0, y0 = ORIGIN_USD
    inside = (xs > x0) & (xs < x0 + SIZE[0]) & (ys > y0) & (ys < y0 + SIZE[1])
    lab, _ = ndimage.label(blocked & ~covered & inside)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab)):
        if sl is None or (lab[sl] == i + 1).sum() < 30:
            continue
        m = lab[sl] == i + 1
        lo_x, lo_y, hi_x, hi_y = xs[sl][m].min(), ys[sl][m].min(), xs[sl][m].max(), ys[sl][m].max()
        cx0, cy0, cx1, cy1 = CHECKOUT_RECT_USD
        if lo_x < cx1 and hi_x > cx0 and lo_y < cy1 and hi_y > cy0:
            continue  # the demo renders carry their own checkout counter: it is a fixture here, not an obstacle
        out.append(
            {
                "lo": [round(float(xs[sl][m].min()), 3), round(float(ys[sl][m].min()), 3)],
                "hi": [round(float(xs[sl][m].max()), 3), round(float(ys[sl][m].max()), 3)],
                "source": "top-down depth render",
            }
        )
    return out


def cmd_dump(args: argparse.Namespace) -> int:  # pragma: no cover - needs pxr + the scene
    import ast  # noqa: PLC0415

    from pxr import Usd, UsdGeom  # type: ignore[import-not-found]  # noqa: PLC0415

    stage = Usd.Stage.Open(args.usd)
    root = stage.GetDefaultPrim()
    root_path = str(root.GetPath())
    layer_dir = Path(args.usd).resolve().parent
    bbc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    bg = stage.GetPrimAtPath(f"{root_path}/background")
    products: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    objects: dict[str, dict[str, Any]] = {}

    def box(prim: Any) -> tuple[list[float], list[float]] | None:
        r = bbc.ComputeWorldBound(prim).ComputeAlignedRange()
        if r.IsEmpty():
            return None
        return [round(v, 4) for v in r.GetMin()], [round(v, 4) for v in r.GetMax()]

    def asset_of(prim: Any) -> str:
        for key in ("payload", "references"):
            op = prim.GetMetadata(key)
            for item in op.GetAddedOrExplicitItems() if op is not None else []:
                if item.assetPath:
                    return str(item.assetPath)
        return ""

    def rel(prim: Any) -> str:
        return str(prim.GetPath())[len(root_path) + 1 :]

    def rec(prim: Any, depth: int) -> None:
        for c in prim.GetChildren():
            name = c.GetName()
            if any(k in name for k in FIXTURE_KEYS):
                continue
            ent = c.GetChild("entity")
            if ent and ent.IsValid() and PRODUCT_RE.match(name):
                b = box(ent)
                if b is None:
                    continue
                asset = asset_of(c)
                obj = Path(asset).parent.name if asset else name.rsplit("_", 1)[0]
                products.append({"path": rel(c), "object": obj, "lo": b[0], "hi": b[1]})
                if obj not in objects:
                    info: dict[str, Any] = {"semantic_name": "", "description": "", "size": None}
                    adir = (layer_dir / asset).resolve().parent
                    try:
                        txt = (adir / "description.py").read_text()
                        try:
                            d = json.loads(txt)
                        except ValueError:
                            d = ast.literal_eval(txt)
                        info["semantic_name"] = (d.get("semantic_name") or [""])[0]
                        info["description"] = (d.get("full_description") or [""])[0]
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        info["size"] = [
                            round(v, 4) for v in json.loads((adir / "object_parameters.json").read_text())["size"]
                        ]  # noqa: E501
                    except Exception:  # noqa: BLE001
                        pass
                    objects[obj] = info
            elif depth < 3 and not name.startswith(("benchmark_", "genie_")):
                rec(c, depth + 1)

    rec(bg, 0)
    building = None
    for prim in Usd.PrimRange(bg):
        name = prim.GetName()
        if name == "background" and building is None and prim.GetParent().GetName() == "background":
            building = box(prim)
            continue
        if not any(k in name for k in FIXTURE_KEYS):
            continue
        anc, inner = prim.GetParent(), False
        while anc and anc.IsValid() and str(anc.GetPath()) != "/":
            inner = inner or any(k in anc.GetName() for k in FIXTURE_KEYS)
            anc = anc.GetParent()
        b = None if inner else box(prim)
        if b is not None and b[0][2] < 0.2:
            fixtures.append(
                {"path": rel(prim), "kind": next(k for k in FIXTURE_KEYS if k in name), "lo": b[0], "hi": b[1]}
            )  # noqa: E501
    doc: dict[str, Any] = {
        "source": f"scripts/genie_to_planogram.py dump of {USD_REL}",
        "default_prim": root_path,
        "building": building,
        "products": sorted(products, key=lambda p: p["path"]),
        "fixtures": sorted(fixtures, key=lambda f: f["path"]),
        "objects": dict(sorted(objects.items())),
        "obstacles": [],
    }
    if args.heights and args.topdown_meta:
        meta = json.loads(Path(args.topdown_meta).read_text())
        doc["obstacles"] = obstacles_from_heightmap(args.heights, meta.get("topdown", meta), doc)
    Path(args.out).write_text(json.dumps(doc, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"{len(products)} products ({len(objects)} types), {len(fixtures)} fixtures, {len(doc['obstacles'])} obstacles"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="dump JSON -> planogram + products block + prim map")
    g.add_argument("--dump", default=str(DUMP))
    g.add_argument("--check", action="store_true", help="exit 1 if a generated file differs (CI / tests)")
    d = sub.add_parser("dump", help="scene USD -> dump JSON (needs pxr)")
    d.add_argument("usd")
    d.add_argument("out")
    d.add_argument("--heights", help="heights.npy of a top-down depth render (adds obstacles)")
    d.add_argument("--topdown-meta", help="JSON with the render's x0 / y1 / px (plan.json 'topdown' of a demo run)")
    args = ap.parse_args(argv)
    return cmd_gen(args) if args.cmd == "gen" else cmd_dump(args)


if __name__ == "__main__":
    sys.exit(main())
