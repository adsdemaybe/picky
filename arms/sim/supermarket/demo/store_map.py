"""Store map for the --demo autopilot, built from the Genie Sim stage itself.

Products (every `<name>/entity` rigid body) with their world boxes, fixtures (shelves, freezers, carts, counters,
floor-stacked goods) as obstacles, sections (adjacent fixtures that carry the same kind of product), an occupancy grid
inflated by the robot's footprint, and A* over it. Pure pxr + numpy: runs inside Isaac Sim (live stage) and in any
python with usd-core (offline, e.g. to pick the shopping list):

    python demo/store_map.py <background.usda> [arm_ik.json]   # prints sections + reachable products
"""

from __future__ import annotations

import heapq
import json
import math
import re
import sys
from dataclasses import dataclass, field

import numpy as np
from pxr import Usd, UsdGeom, UsdPhysics

FIXTURE_KEYS = ("shelf", "freezer", "cart", "counter", "fridge", "checkout")
LABELS = {
    "snack": "Snacks",
    "beverage_bottle": "Drinks",
    "beer": "Beer",
    "dairy": "Dairy",
    "cleaning_products": "Cleaning",
    "disposable_paper_cup_packaging_bag": "Paper goods",
    "skincare_products": "Personal care",
    "toy": "Toys",
    "stationery": "Stationery",
    "grain": "Grains",
    "air_freshener": "Air fresheners",
    "frozen_food": "Frozen",
}
SIDES = {"west": (-1.0, 0.0), "east": (1.0, 0.0), "south": (0.0, -1.0), "north": (0.0, 1.0)}

# robot geometry (base frame, x forward): the arm's tool point lines up with the product at (reach, ARM_Y)
ARM_Y = -0.17
FRONT_CLEAR = 0.47  # base origin -> shelf face when picking (front wheels end ~0.36 m ahead of the origin)
ROBOT_RADIUS = 0.32  # grid inflation: SO-101 rover v3 PMB2 footprint r 0.269 m + 5 cm (covers the rm2's 0.22 m too)


def category(name: str) -> str:
    m = re.match(r"(?:benchmark|genie)_([a-z_]+?)_\d", name)
    return m.group(1) if m else "other"


def sku(name: str) -> str:
    m = re.match(r"((?:benchmark|genie)_[a-z_]+?_\d+)", name)
    return m.group(1) if m else name


def product_entities(stage, root: str = "/World/background") -> list:
    """Every Genie Sim product instance: <...>/<benchmark_|genie_><cat>_<id>_<n>/entity with a rigid body. Shelves
    are grouped with their stock under Group_* prims in some scenes, so this recurses into non-product Xforms."""
    out = []

    def rec(prim, depth):
        for c in prim.GetChildren():
            n = c.GetName()
            if any(k in n for k in FIXTURE_KEYS):
                continue
            ent = c.GetChild("entity")
            if ent and ent.IsValid() and ent.HasAPI(UsdPhysics.RigidBodyAPI):
                out.append(ent)
            elif depth < 3 and not n.startswith(("benchmark_", "genie_")):
                rec(c, depth + 1)

    bg = stage.GetPrimAtPath(root)
    if bg and bg.IsValid():
        rec(bg, 0)
    return out


@dataclass
class Product:
    path: str  # the entity prim (rigid body)
    name: str  # instance name, e.g. benchmark_snack_087_003
    cat: str
    sku: str
    lo: np.ndarray
    hi: np.ndarray
    fixture: int = -1
    side: str = ""
    depth: float = 9.0  # product centre -> fixture face along the approach side
    section: str = ""

    @property
    def center(self) -> np.ndarray:
        return (self.lo + self.hi) / 2


@dataclass
class Fixture:
    path: str
    lo: np.ndarray
    hi: np.ndarray
    kind: str
    label: str = ""
    section: str = ""
    cats: dict = field(default_factory=dict)


def _box(bbc, prim):
    r = bbc.ComputeWorldBound(prim).ComputeAlignedRange()
    if r.IsEmpty():
        return None
    return np.array(r.GetMin()), np.array(r.GetMax())


class StoreMap:
    def __init__(self, stage, res: float = 0.05, inflate: float = ROBOT_RADIUS, extra_fixtures=()):
        bbc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        self.res, self.inflate = res, inflate
        self.products: list[Product] = []
        for ent in product_entities(stage):
            b = _box(bbc, ent)
            if b is None:
                continue
            name = ent.GetParent().GetName()
            self.products.append(Product(str(ent.GetPath()), name, category(name), sku(name), b[0], b[1]))
        self.fixtures: list[Fixture] = []
        building = None
        for prim in Usd.PrimRange(stage.GetPrimAtPath("/World/background")):
            n = prim.GetName()
            if n == "background" and building is None and prim.GetParent().GetName() == "background":
                building = _box(bbc, prim)
                continue
            if any(k in n for k in FIXTURE_KEYS):
                parent_fix = any(any(k in a.GetName() for k in FIXTURE_KEYS) for a in _ancestors(prim))
                if parent_fix:
                    continue
                b = _box(bbc, prim)
                if b is not None and b[0][2] < 0.2:
                    kind = next(k for k in FIXTURE_KEYS if k in n)
                    self.fixtures.append(Fixture(str(prim.GetPath()), b[0], b[1], kind))
        for path, lo, hi, kind in extra_fixtures:
            self.fixtures.append(Fixture(path, np.array(lo, float), np.array(hi, float), kind))
        # floor-stacked goods (pallets of water etc.) block the floor too
        for p in self.products:
            size = p.hi - p.lo
            if p.lo[2] < 0.1 and size[0] * size[1] > 0.3:
                self.fixtures.append(Fixture(p.path, p.lo, p.hi, "stack"))
        self.products = [p for p in self.products if not (p.lo[2] < 0.1 and np.prod((p.hi - p.lo)[:2]) > 0.3)]
        if building is None:
            lo = np.min([f.lo for f in self.fixtures], axis=0) - 1.5
            hi = np.max([f.hi for f in self.fixtures], axis=0) + 1.5
            building = (lo, hi)
        self.lo = building[0][:2] + 0.25  # keep off the walls
        self.hi = building[1][:2] - 0.25
        self._assign()
        self._sections()
        self._grid()

    # --- products -> fixtures, sides, sections -------------------------------------------------------------------
    def _assign(self):
        for p in self.products:
            c = p.center
            best = None
            for i, f in enumerate(self.fixtures):
                if f.kind == "stack":
                    continue
                inside = np.all(c[:2] >= f.lo[:2] - 0.08) and np.all(c[:2] <= f.hi[:2] + 0.08)
                if inside and f.lo[2] - 0.1 <= c[2] <= f.hi[2] + 0.3:
                    area = np.prod(f.hi[:2] - f.lo[:2])
                    if best is None or area < best[0]:
                        best = (area, i)
            if best is None:
                continue
            p.fixture = best[1]
            f = self.fixtures[p.fixture]
            d = {
                "west": c[0] - f.lo[0],
                "east": f.hi[0] - c[0],
                "south": c[1] - f.lo[1],
                "north": f.hi[1] - c[1],
            }
            # only sides the robot can stand at count (not against a wall or another fixture)
            free = {s: v for s, v in d.items() if self._side_open(f, s)}
            if free:
                p.side, p.depth = min(free.items(), key=lambda kv: kv[1])
            f.cats[p.cat] = f.cats.get(p.cat, 0) + 1

    def restrict_sides(self, open_sides: dict) -> None:
        """Map annotation: sections whose shelves have a closed back only open on the listed sides
        ({"S1": ["south"], ...}); products there re-pick their approach side among those."""
        for p in self.products:
            allowed = open_sides.get(p.section)
            if not allowed or p.fixture < 0:
                continue
            f = self.fixtures[p.fixture]
            c = p.center
            d = {"west": c[0] - f.lo[0], "east": f.hi[0] - c[0], "south": c[1] - f.lo[1], "north": f.hi[1] - c[1]}
            free = {s_: v for s_, v in d.items() if s_ in allowed}
            p.side, p.depth = min(free.items(), key=lambda kv: kv[1]) if free else ("", 9.0)
        self.open_sides = dict(open_sides)

    def _side_open(self, f: Fixture, side: str) -> bool:
        allowed = getattr(self, "open_sides", {}).get(f.section)
        if allowed is not None and side not in allowed:
            return False
        nx, ny = SIDES[side]
        mid = (f.lo[:2] + f.hi[:2]) / 2
        half = (f.hi[:2] - f.lo[:2]) / 2
        probe = mid + np.array([nx, ny]) * (half + FRONT_CLEAR + 0.05)
        if np.any(probe < self.lo) or np.any(probe > self.hi):
            return False
        for g in self.fixtures:
            if g is f:
                continue
            if np.all(probe >= g.lo[:2] - 0.15) and np.all(probe <= g.hi[:2] + 0.15):
                return False
        return True

    def _sections(self):
        for f in self.fixtures:
            if f.cats:
                top = max(f.cats.items(), key=lambda kv: kv[1])[0]
                f.label = LABELS.get(top, top.replace("_", " ").title())
        # union adjacent fixtures with the same label
        n = len(self.fixtures)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                a, b = self.fixtures[i], self.fixtures[j]
                if a.label and a.label == b.label:
                    gap = np.maximum(0, np.maximum(a.lo[:2] - b.hi[:2], b.lo[:2] - a.hi[:2]))
                    if np.all(gap < 0.15):
                        parent[find(i)] = find(j)
        groups: dict[int, list[int]] = {}
        for i, f in enumerate(self.fixtures):
            if f.label:
                groups.setdefault(find(i), []).append(i)
        shelves = [f for f in self.fixtures if f.label]
        f_lo = np.min([f.lo[:2] for f in shelves], axis=0) if shelves else self.lo
        f_hi = np.max([f.hi[:2] for f in shelves], axis=0) if shelves else self.hi
        mid_store = (f_lo + f_hi) / 2  # compass words relative to the shelving area, not the whole building
        span = np.maximum(f_hi - f_lo, 1e-3)
        self.sections = []
        for k, idxs in enumerate(
            sorted(groups.values(), key=lambda g: (-self.fixtures[g[0]].lo[1], self.fixtures[g[0]].lo[0]))
        ):
            lo = np.min([self.fixtures[i].lo for i in idxs], axis=0)
            hi = np.max([self.fixtures[i].hi for i in idxs], axis=0)
            c = (lo[:2] + hi[:2]) / 2
            rel = (c - mid_store) / (span / 2)
            near_wall = np.any(lo[:2] - self.lo < 0.6) or np.any(self.hi - hi[:2] < 0.6)
            ns = "north" if rel[1] > 0.4 else "south" if rel[1] < -0.4 else ""
            ew = "east" if rel[0] > 0.4 else "west" if rel[0] < -0.4 else ""
            where = (ns + ew) or "center"
            where += " wall" if near_wall else (" row" if ns and not ew else " island")
            sid = f"S{k + 1}"
            label = self.fixtures[idxs[0]].label
            for i in idxs:
                self.fixtures[i].section = sid
            self.sections.append({"id": sid, "label": label, "where": where, "lo": lo[:2], "hi": hi[:2]})
        for p in self.products:
            if p.fixture >= 0:
                p.section = self.fixtures[p.fixture].section

    # --- occupancy grid + A* ------------------------------------------------------------------------------------
    def _grid(self):
        self.nx = int(math.ceil((self.hi[0] - self.lo[0]) / self.res)) + 1
        self.ny = int(math.ceil((self.hi[1] - self.lo[1]) / self.res)) + 1
        self.raw = np.zeros((self.nx, self.ny), bool)  # cells covered by an obstacle (not inflated)
        for f in self.fixtures:
            if f.hi[2] < 0.05:
                continue
            i0, j0 = self.cell(f.lo[:2])
            i1, j1 = self.cell(f.hi[:2])
            self.raw[max(i0, 0) : max(i1 + 1, 0), max(j0, 0) : max(j1 + 1, 0)] = True
        self._inflate()

    def add_heightmap(self, h: np.ndarray, x0: float, y1: float, px: float, zmin: float = 0.10):
        """Merge a top-down height map (rendered from an orthographic camera below the ceiling) into the obstacles:
        catches everything the USD boxes miss (pillars, walls, cabinets that are part of the building mesh).
        Pixels with no depth (NaN) are geometry that reaches above the camera - pillars and walls."""
        rr, cc = np.nonzero(~np.isfinite(h) | (h > zmin))
        i = np.round((x0 + (cc + 0.5) * px - self.lo[0]) / self.res).astype(int)
        j = np.round((y1 - (rr + 0.5) * px - self.lo[1]) / self.res).astype(int)
        ok = (i >= 0) & (i < self.nx) & (j >= 0) & (j < self.ny)
        self.raw[i[ok], j[ok]] = True
        self._inflate()

    def _inflate(self):
        r = int(math.ceil(self.inflate / self.res))
        pad = np.pad(self.raw, r)
        occ = np.zeros_like(self.raw)
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                if di * di + dj * dj <= r * r:
                    occ |= pad[r + di : r + di + self.nx, r + dj : r + dj + self.ny]
        occ[0, :] = occ[-1, :] = occ[:, 0] = occ[:, -1] = True
        self.occ = occ
        if hasattr(self, "_clear"):
            del self._clear

    def cell(self, xy) -> tuple[int, int]:
        return int(round((xy[0] - self.lo[0]) / self.res)), int(round((xy[1] - self.lo[1]) / self.res))

    def world(self, ij) -> np.ndarray:
        return self.lo + np.array(ij, float) * self.res

    def free(self, xy, margin: float = 0.0) -> bool:
        i, j = self.cell(xy)
        if not (0 <= i < self.nx and 0 <= j < self.ny):
            return False
        if margin <= 0:
            return not self.occ[i, j]
        r = int(math.ceil(margin / self.res))
        return not self.occ[max(0, i - r) : i + r + 1, max(0, j - r) : j + r + 1].any()

    def _nearest_free(self, ij):
        if not self.occ[ij]:
            return ij
        best = None
        for r in range(1, 40):
            for di in range(-r, r + 1):
                for dj in (-r, r) if abs(di) != r else range(-r, r + 1):
                    a, b = ij[0] + di, ij[1] + dj
                    if 0 <= a < self.nx and 0 <= b < self.ny and not self.occ[a, b]:
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, (a, b))
            if best:
                return best[1]
        return ij

    def astar(self, start, goal) -> list[np.ndarray] | None:
        """Collision-free path start -> goal (world xy) as a few waypoints (line-of-sight shortcut)."""
        s, g = self._nearest_free(self.cell(start)), self._nearest_free(self.cell(goal))
        if not (0 <= g[0] < self.nx and 0 <= g[1] < self.ny):
            return None
        moves = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0)]
        moves += [(a, b, 1.4142) for a in (1, -1) for b in (1, -1)]
        # prefer aisle centres: small cost for cells close to obstacles
        if not hasattr(self, "_clear"):
            self._clear = self._clearance()
        openq = [(0.0, s)]
        came = {s: None}
        cost = {s: 0.0}
        while openq:
            _, cur = heapq.heappop(openq)
            if cur == g:
                break
            for di, dj, w in moves:
                nb = (cur[0] + di, cur[1] + dj)
                if not (0 <= nb[0] < self.nx and 0 <= nb[1] < self.ny) or self.occ[nb]:
                    continue
                nc = cost[cur] + w * (1.0 + 2.0 * max(0.0, 0.4 - self._clear[nb]))
                if nb not in cost or nc < cost[nb]:
                    cost[nb] = nc
                    came[nb] = cur
                    heapq.heappush(openq, (nc + math.hypot(g[0] - nb[0], g[1] - nb[1]), nb))
        if g not in came:
            return None
        cells = []
        c = g
        while c is not None:
            cells.append(c)
            c = came[c]
        cells.reverse()
        pts = [np.array(start, float)]
        k = 0
        while k < len(cells) - 1:  # greedy line-of-sight shortcut
            j = len(cells) - 1
            while j > k + 1 and not self._los(cells[k], cells[j]):
                j -= 1
            pts.append(self.world(cells[j]))
            k = j
        pts[-1] = np.array(goal, float)
        return pts

    def _clearance(self):
        """Distance (m) from each free cell to the nearest blocked cell, capped (cheap multi-pass)."""
        d = np.where(self.occ, 0.0, 9.0)
        for _ in range(12):
            for ax in (0, 1):
                for sh in (1, -1):
                    d = np.minimum(d, np.roll(d, sh, axis=ax) + self.res)
        return d

    def _los(self, a, b) -> bool:
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        for t in np.linspace(0, 1, n + 1):
            i = int(round(a[0] + (b[0] - a[0]) * t))
            j = int(round(a[1] + (b[1] - a[1]) * t))
            if self.occ[i, j]:
                return False
        return True

    # --- picking geometry ---------------------------------------------------------------------------------------
    def approach(self, p: Product, side: str | None = None, reach: float | None = None):
        """Base pose (x, y, yaw) that faces the product's fixture from `side` with the arm lined up on it."""
        side = side or p.side
        f = self.fixtures[p.fixture]
        nx, ny = SIDES[side]
        c = p.center
        depth = {"west": c[0] - f.lo[0], "east": f.hi[0] - c[0], "south": c[1] - f.lo[1], "north": f.hi[1] - c[1]}[side]
        reach = FRONT_CLEAR + depth if reach is None else reach
        yaw = math.atan2(-ny, -nx)  # face into the fixture
        cy, sy = math.cos(yaw), math.sin(yaw)
        bx = c[0] - (cy * reach - sy * ARM_Y)
        by = c[1] - (sy * reach + cy * ARM_Y)
        return np.array([bx, by]), yaw, reach

    def ascii(self, cell: float = 0.25) -> str:
        """Coarse occupancy picture for the LLM: '#' blocked, '.' drivable; top row = north (max y)."""
        k = max(1, int(round(cell / self.res)))
        rows = []
        for j in range(self.ny - 1, -1, -k):
            rows.append("".join("#" if self.occ[i : i + k, max(j - k + 1, 0) : j + 1].mean() > 0.5 else "."
                                for i in range(0, self.nx, k)))  # fmt: skip
        return (f"cell {cell} m; columns west->east from x={self.lo[0]:.2f}; rows north->south from y={self.hi[1]:.2f}\n"  # noqa: E501
                + "\n".join(rows))  # fmt: skip

    def summary(self) -> dict:
        """Compact map for the LLM planner."""
        return {
            "frame": "metres; x east, y north; yaw 0 = facing east",
            "store_bounds": {"min": _r(self.lo), "max": _r(self.hi)},
            "sections": [
                {"id": s["id"], "label": s["label"], "where": s["where"], "min": _r(s["lo"]), "max": _r(s["hi"])}
                for s in self.sections
            ],
            "obstacles": [
                {"min": _r(f.lo[:2]), "max": _r(f.hi[:2]), "kind": f.kind, "section": f.section or None}
                for f in self.fixtures
            ],
        }

    def to_json(self) -> dict:
        return {
            "lo": _r(self.lo),
            "hi": _r(self.hi),
            "sections": [{**s, "lo": _r(s["lo"]), "hi": _r(s["hi"])} for s in self.sections],
            "fixtures": [{"lo": _r(f.lo), "hi": _r(f.hi), "kind": f.kind, "section": f.section} for f in self.fixtures],
        }


def _ancestors(prim):
    p = prim.GetParent()
    while p and p.IsValid() and str(p.GetPath()) != "/":
        yield p
        p = p.GetParent()


def _r(v):
    return [round(float(x), 3) for x in v]


def ik_reachable(ik: dict, x: float, z: float) -> bool:
    """Is (x, ARM_Y, z) in the arm's front IK table (with its 4 neighbours, so interpolation is valid)?"""
    xs, zs = np.array(ik["front"]["x"]), np.array(ik["front"]["z"])
    i = int(np.searchsorted(xs, x)) - 1
    j = int(np.searchsorted(zs, z)) - 1
    if i < 0 or j < 0 or i + 1 >= len(xs) or j + 1 >= len(zs):
        return False
    return all(f"{xs[a]:.3f},{ik['arm_y']:.3f},{zs[b]:.3f}" in ik["front"]["q"] for a in (i, i + 1) for b in (j, j + 1))


def main() -> None:
    stage = Usd.Stage.Open(sys.argv[1])
    m = StoreMap(stage)
    ik = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None
    print(f"bounds {m.lo} {m.hi}; {len(m.products)} products, {len(m.fixtures)} fixtures")
    for s in m.sections:
        print(s["id"], s["label"], s["where"], _r(s["lo"]), _r(s["hi"]))
    rows = []
    for p in m.products:
        if p.fixture < 0 or not p.side:
            continue
        pos, yaw, reach = m.approach(p)
        ok_ik = ik is None or ik_reachable(ik, reach + 0.0, p.center[2])
        ok_ik_pre = ik is None or ik_reachable(ik, reach - 0.12, p.center[2] + 0.03)
        if p.depth < 0.2 and ok_ik and ok_ik_pre and m.free(pos):
            rows.append((p.section, p.cat, p.sku, p.name, p.side, round(p.depth, 2), _r(p.center), _r(pos)))
    rows.sort()
    print(f"{len(rows)} pickable products")
    for r in rows:
        print(*r)
    json.dump({"map": m.to_json(), "pickable": rows}, open("/tmp/store_map_offline.json", "w"))


if __name__ == "__main__":
    main()
