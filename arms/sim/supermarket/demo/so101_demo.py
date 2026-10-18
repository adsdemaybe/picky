"""--demo with Picky's SO-101 rover v3 (PMB2 round base + fixed riser + long-reach SO-101 + round wire basket).

Gemini is the rover's navigation brain: the app builds a store map from the scene (sections, obstacles, the checkout),
picks a shopping list the SO-101 can really grasp (jaw opening ~7 cm: bottles, cans, small boxes, snack packs) across
the store's sections, and Gemini orders the stops with a reason per leg. The rover executes the plan:

  A* path (0.05 m grid, inflated by the 0.27 m base + margin) -> differential drive along it (pure pursuit, turn in
  place) -> park PARALLEL to the shelf with the shelf on the robot's right (pan 0 = right) -> IK (so101_ik, numpy DLS)
  pre-grasp + straight slide in with min-jerk timing -> close the moving jaw until it meets the product -> attach only
  if the product is really between the jaws (jaw mesh sample points vs. its box) -> lift, retract, fold the tool down,
  swing 90 deg to the rear basket, lower, open: gravity drops it in, it rides along on the basket floor (friction) ->
  next stop. A stop that fails (no path, out of reach, grasp check failed twice) goes back to Gemini for a replan.
  Checkout: park parallel to the counter, take every item out of the basket (same IK + jaw rule, tool pointing down)
  and lay it on the counter, "Paid".

Everything is commanded through the robot's joint drives (planar base joints + 6 arm joints, position + velocity
targets); the products are PhysX rigid bodies. The mission logic only talks to an IO object, so the same code runs
in Isaac Sim (IsaacIO) and offline with perfect tracking (FakeIO, `python demo/so101_demo.py --offline ...`).
Per-frame state + events go to frames.jsonl / events.jsonl for demo/edit_so101.py.
"""

from __future__ import annotations

import collections
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "robot"))
from so101_ik import RoverKinematics, minjerk, minjerk_vel  # noqa: E402

DT = 1.0 / 60
ROBOT = "/World/Picky"
ARM = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
BASE = ["base_x", "base_y", "base_yaw"]
CTRL = BASE + ARM + ["gripper"]
NAME = "Pico"
MAX_W = 0.068  # widest product (across the jaws) we try: the SO-101 jaw opens ~8.6 cm at 1.2 rad
MIN_W = 0.022
PITCHES = [0, 5, 10, 15, 20, -5, -10]  # tool pitch (deg, + = pointing down) tried for a shelf grasp
GRIP_IN = 0.035  # deep products are gripped this far behind their front face (the jaw pads are ~4 cm long)
CAT_NAME = {
    "beverage_bottle": "Drink bottle",
    "beer": "Beer",
    "snack": "Snack",
    "cleaning_products": "Cleaner",
    "skincare_products": "Skincare",
    "disposable_paper_cup_packaging_bag": "Paper cups",
    "toy": "Toy",
    "stationery": "Stationery",
    "grain": "Grains",
    "dairy": "Dairy",
    "air_freshener": "Air freshener",
    "frozen_food": "Frozen",
}
SYSTEM = (
    "You are the navigation brain of Picky, a shopping rover in a supermarket. The rover is a round differential-drive "
    "base (0.54 m across: it drives forward along a path and turns in place) with a long-reach SO-101 arm on a riser "
    "and a round wire basket on its back. To pick an item it parks parallel to the shelf with the shelf on its RIGHT "
    "side, reaches in, and drops the item in its basket. You get a store map (sections with bounding boxes, obstacles, "
    "an ASCII occupancy picture, the rover start pose, the checkout counter) and a shopping list where every item has "
    "its section, position, shelf height and the aisle side the rover must stand on. Order the stops so the total "
    "driving is short and the rover rarely has to turn around (think about which aisles connect and which way the "
    "rover will face), then end at the checkout. Explain each leg in one short, concrete sentence a shopper would "
    "understand (mention the section and why it comes next). Answer with JSON only."
)


# --- small geometry helpers ------------------------------------------------------------------------------------------
def Rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def base_T(x: float, y: float, yaw: float, z: float = 0.0) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = Rz(yaw)
    T[:3, 3] = [x, y, z]
    return T


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def quat_to_R(w, x, y, z) -> np.ndarray:
    n = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def R_to_quat(R) -> list:
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w, x, y, z = 0.25 * s, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w, x, y, z = (R[2, 1] - R[1, 2]) / s, 0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w, x, y, z = (R[0, 2] - R[2, 0]) / s, (R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w, x, y, z = (R[1, 0] - R[0, 1]) / s, (R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s
    return [float(w), float(x), float(y), float(z)]


def slerp_R(R0, R1, s: float) -> np.ndarray:
    q0, q1 = np.array(R_to_quat(R0)), np.array(R_to_quat(R1))
    d = float(q0 @ q1)
    if d < 0:
        q1, d = -q1, -d
    if d > 0.9995:
        q = q0 + s * (q1 - q0)
    else:
        th = math.acos(d)
        q = (math.sin((1 - s) * th) * q0 + math.sin(s * th) * q1) / math.sin(th)
    return quat_to_R(*(q / np.linalg.norm(q)))


def box_corners(lo, hi) -> np.ndarray:
    return np.array([[x, y, z, 1.0] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])


SIDE_N = {"west": (-1.0, 0.0), "east": (1.0, 0.0), "south": (0.0, -1.0), "north": (0.0, 1.0)}


def r2(v, n=3):
    return [round(float(x), n) for x in v]


def ik_natural(kin: RoverKinematics, p, R, roll=None, w_rot=0.1, tol=(0.003, 0.05), extra_seeds=()):
    """IK over many seeds; among the solutions within tol pick the most natural posture: elbow well above the
    shoulder and the upper arm leaning towards the target (not folded back behind the riser). (q, err) or best-effort"""
    pan0 = kin._pan_seed(np.asarray(p, float))
    r0 = kin.grasp_roll if roll is None else roll
    seeds = [list(s_) for s_ in extra_seeds]
    seeds += [
        [pan0, lift, el, wr, r0] for lift in (-0.5, -0.1, 0.3, 0.7) for el in (0.4, 1.0, 1.5) for wr in (-0.5, 0.5)
    ]
    best, best_any = None, None
    for sd_ in seeds:
        q, e = kin.solve(p, R=R, roll=roll, w_rot=w_rot, seeds=[sd_])
        if best_any is None or e[0] + 0.05 * e[1] < best_any[1][0] + 0.05 * best_any[1][1]:
            best_any = (q, e)
        if e[0] > tol[0] or e[1] > tol[1]:
            continue
        L = kin.links(q)
        sh, el = L["so101_upper_arm"][:3, 3], L["so101_lower_arm"][:3, 3]
        # horizontal direction from the pan axis to the target
        d = np.asarray(p, float)[:2] - sh[:2]
        d = d / (np.linalg.norm(d) + 1e-9)
        lean = float((el[:2] - sh[:2]) @ d)  # elbow towards the target (+) or behind the shoulder (-)
        score = (el[2] - sh[2]) + 0.6 * min(lean, 0.0)
        if best is None or score > best[0]:
            best = (score, q, e)
    return (best[1], best[2]) if best else best_any


# --- shopping list: products the SO-101 can grasp, parked parallel to the shelf ---------------------------------------
def side_pose(c, side: str, standoff: float, pan_x: float, along: float = 0.0):
    """Base pose with the product on the robot's right: the pan axis level with the grasp point (product centre moved
    `along` m in the robot's forward direction: the SO-101 grasps against its fixed jaw, so the tool point sits half a
    product width off the centre, and a 5-DoF arm only reaches a horizontal approach in its own vertical plane),
    `standoff` m out from it."""
    n = np.array(SIDE_N[side])
    yaw = math.atan2(-n[0], n[1])  # base +y = n (out of the shelf) -> the shelf is on the right
    fwd = np.array([math.cos(yaw), math.sin(yaw)])
    return np.asarray(c[:2], float) + (along - pan_x) * fwd + standoff * n, yaw


class Catalog:
    """Grasp-feasibility screen over every product in the store map (world AABBs; the real pick re-plans from the
    product's true pose)."""

    def __init__(self, smap, kin: RoverKinematics):
        self.smap, self.kin = smap, kin
        self.pan_x = float(kin.k["pan_axis_xy"][0])
        self._by_fix: dict[int, list] = {}
        for p in smap.products:
            self._by_fix.setdefault(p.fixture, []).append(p)

    def gaps(self, p, side):
        """free space along the shelf on either side of the product (m; - = towards the robot's front when it parks
        with the shelf on its right), the free height above it, and whether another product stands in front of it"""
        n = np.array(SIDE_N[side])
        t = np.array([-n[1], n[0]])  # along the shelf; the parked robot faces -t
        c = p.center
        half_t = abs((p.hi[:2] - p.lo[:2]) @ np.abs(t)) / 2
        half_n = abs((p.hi[:2] - p.lo[:2]) @ np.abs(n)) / 2
        g_lo, g_hi, above, blocked = 9.0, 9.0, 9.0, False
        for q in self._by_fix.get(p.fixture, []):
            if q is p:
                continue
            qc = q.center
            d_t = (qc[:2] - c[:2]) @ t
            d_n = (qc[:2] - c[:2]) @ n
            ext_t = abs((q.hi[:2] - q.lo[:2]) @ np.abs(t)) / 2
            ext_n = abs((q.hi[:2] - q.lo[:2]) @ np.abs(n)) / 2
            z_overlap = q.lo[2] < p.hi[2] - 0.01 and q.hi[2] > p.lo[2] + 0.01
            lat_overlap = abs(d_t) < ext_t + half_t - 0.005
            depth_overlap = abs(d_n) < ext_n + half_n + 0.03
            if z_overlap and depth_overlap and not lat_overlap:
                if d_t > 0:
                    g_hi = min(g_hi, d_t - ext_t - half_t)
                else:
                    g_lo = min(g_lo, -d_t - ext_t - half_t)
            if z_overlap and lat_overlap and d_n > 0.01:  # something between it and the aisle
                blocked = True
            if lat_overlap and abs(d_n) < ext_n + half_n and q.lo[2] >= p.hi[2] - 0.01:
                above = min(above, q.lo[2] - p.hi[2])
        return g_lo, g_hi, above, blocked

    def screen(self, p):
        """None or a dict: side, standoff, base pose, width, pitch, roll, gaps"""
        smap = self.smap
        if p.fixture < 0 or not p.side or not (0.50 <= p.center[2] <= 1.42):
            return None
        side = p.side
        size = p.hi - p.lo
        n = np.array(SIDE_N[side])
        width = float(abs(size[:2] @ np.array([-n[1], n[0]])))  # across the jaws (along the shelf)
        depth_len = float(abs(size[:2] @ n))
        if not (MIN_W <= width <= MAX_W) or not (0.05 <= size[2] <= 0.30) or depth_len > 0.24:
            return None
        if p.depth > 0.24:
            return None
        g_lo, g_hi, above, blocked = self.gaps(p, side)
        if blocked or above < 0.035:
            return None
        # default roll: fixed jaw towards the robot's front (g_lo side), moving jaw towards its back (g_hi side).
        # The moving jaw swings wide, so it goes on the roomier side (flipped roll = wrist camera under the jaws).
        flip = g_lo > g_hi
        g_fixed, g_moving = (g_hi, g_lo) if flip else (g_lo, g_hi)
        roll = self.kin.grasp_roll + (math.pi if flip else 0.0)
        front = p.depth - depth_len / 2  # aisle face of the product, from the fixture face
        along = (width / 2 - 0.0005) * (-1.0 if flip else 1.0)  # fixed jaw: robot-forward side (default roll)
        for standoff in (max(0.48, front + 0.36), max(0.44, front + 0.36), max(0.52, front + 0.40), front + 0.43):
            if standoff > 0.56:
                continue
            xy, yaw = side_pose(p.center, side, standoff, self.pan_x, along)
            if not smap.free(xy):
                continue
            pitch = self.ik_ok(p, standoff, roll, depth_len)
            if pitch is None:
                continue
            return {
                "side": side,
                "standoff": round(standoff, 3),
                "xy": xy,
                "yaw": yaw,
                "width": width,
                "pitch": pitch,
                "roll": roll,
                "flip": flip,
                "gap_fixed": g_fixed,
                "gap_moving": g_moving,
                "above": above,
                "depth_len": depth_len,
            }
        return None

    def ik_ok(self, p, standoff, roll, depth_len):
        kin = self.kin
        h = float(p.center[2])
        reach = standoff - (depth_len / 2 - min(depth_len / 2, GRIP_IN))  # deep products: grab their front end
        for pitch in PITCHES:
            ph = math.radians(pitch)
            a = np.array([0.0, -math.cos(ph), -math.sin(ph)])
            R = kin.target_rotation(a, roll)
            tcp = np.array([self.pan_x, -reach, h])
            q, e = kin.solve(tcp, R=R, roll=roll, w_rot=0.1)
            if e[0] < 0.002 and e[1] < math.radians(2.5):
                far = tcp - a * 0.20
                qf, ef = kin.solve(far, R=R, w_rot=0.1, seeds=[q])
                if ef[0] < 0.003 and ef[1] < math.radians(3):
                    return pitch
        return None


def item_name(p) -> str:
    num = p.sku.rsplit("_", 1)[-1]
    return f"{CAT_NAME.get(p.cat, p.cat.replace('_', ' ').title())} {num}"


def free_component(smap, start_xy) -> np.ndarray:
    """cells reachable from start_xy through free space (BFS on the inflated grid)"""
    occ = smap.occ
    seen = np.zeros_like(occ)
    s = smap._nearest_free(smap.cell(start_xy))
    dq = collections.deque([s])
    seen[s] = True
    while dq:
        i, j = dq.popleft()
        for a, b in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
            if 0 <= a < smap.nx and 0 <= b < smap.ny and not occ[a, b] and not seen[a, b]:
                seen[a, b] = True
                dq.append((a, b))
    return seen


def choose_items(cat: Catalog, n_items: int, start_xy, rng_seed: int = 3, fixed=()):
    """Round-robin over sections: one graspable SKU per section per round, preferring roomy slots (open jaws don't
    brush the neighbours), mid-height shelves and approach poses connected to the start."""
    smap = cat.smap
    by_name = {p.name: p for p in smap.products}
    chosen = []
    for nm in fixed:
        p = by_name.get(nm)
        g = cat.screen(p) if p is not None else None
        if g:
            chosen.append((p, g))
    if chosen:
        return chosen[:n_items]
    secs: dict[str, dict] = {}
    for p in smap.products:
        if not p.section:
            continue
        secs.setdefault(p.section, {}).setdefault(p.sku, []).append(p)
    comp = free_component(smap, start_xy)
    for min_fixed, min_moving in ((0.015, 0.03), (0.008, 0.02), (0.0, 0.012)):
        ranked = {}
        for s, skus in secs.items():
            opts = []
            for ps in skus.values():
                best = None
                for p in sorted(ps, key=lambda q: abs(q.center[2] - 1.0))[:14]:
                    g = cat.screen(p)
                    if g is None or not comp[smap.cell(g["xy"])]:
                        continue
                    if g["gap_moving"] < min_moving or g["gap_fixed"] < min_fixed:
                        continue
                    room = min(g["gap_fixed"], 0.03) + min(g["gap_moving"], 0.05)
                    score = room * 8 - abs(p.center[2] - 1.0) - 0.3 * abs(g["pitch"]) / 20 - 0.2 * g["flip"]
                    if best is None or score > best[0]:
                        best = (score, p, g)
                if best:
                    opts.append(best)
            opts.sort(key=lambda o: -o[0])
            ranked[s] = opts
        chosen = []
        used = set()
        order = sorted(ranked, key=lambda s: -len(ranked[s]))
        k = 0
        while len(chosen) < n_items and any(ranked[s] for s in order):
            s = order[k % len(order)]
            k += 1
            while ranked[s]:
                _, p, g = ranked[s].pop(0)
                if p.sku not in used:
                    used.add(p.sku)
                    chosen.append((p, g))
                    break
        n_secs = len({p.section for p, _ in chosen})
        if len(chosen) >= min(n_items, 10) and n_secs >= min(5, n_items):
            break
    return chosen


# --- occlusion grid for the camera director ---------------------------------------------------------------------------
class HeightGrid:
    """max obstacle height per map cell (fixture boxes + the top-down height render); camera eyes must sit in free
    air and see their subject."""

    def __init__(self, smap, heights=None, meta=None):
        self.m = smap
        h = np.zeros((smap.nx, smap.ny), np.float32)
        for f in smap.fixtures:
            i0, j0 = smap.cell(f.lo[:2])
            i1, j1 = smap.cell(f.hi[:2])
            sl = (slice(max(i0, 0), max(i1 + 1, 0)), slice(max(j0, 0), max(j1 + 1, 0)))
            h[sl] = np.maximum(h[sl], f.hi[2])
        if heights is not None and meta is not None:
            hh = np.asarray(heights, np.float32)
            hh = np.where(np.isfinite(hh), hh, 4.0)
            rr, cc = np.nonzero(hh > 0.08)
            px = meta["px"]
            i = np.round((meta["x0"] + (cc + 0.5) * px - smap.lo[0]) / smap.res).astype(int)
            j = np.round((meta["y1"] - (rr + 0.5) * px - smap.lo[1]) / smap.res).astype(int)
            ok = (i >= 0) & (i < smap.nx) & (j >= 0) & (j < smap.ny)
            np.maximum.at(h, (i[ok], j[ok]), hh[rr[ok], cc[ok]])
        self.h = h

    def at(self, xy) -> float:
        i, j = self.m.cell(xy)
        if not (0 <= i < self.m.nx and 0 <= j < self.m.ny):
            return 9.0
        return float(self.h[i, j])

    def eye_ok(self, eye, clear=0.25) -> bool:
        m = self.m
        if np.any(eye[:2] < m.lo - 0.1) or np.any(eye[:2] > m.hi + 0.1) or not (0.4 < eye[2] < 2.45):
            return False
        r = int(math.ceil(clear / m.res))
        i, j = m.cell(eye[:2])
        win = self.h[max(i - r, 0) : i + r + 1, max(j - r, 0) : j + r + 1]
        return win.size > 0 and float(win.max()) < eye[2] - 0.25

    def visible(self, eye, target, skip_end=0.35) -> bool:
        d = np.asarray(target, float) - eye
        L = float(np.linalg.norm(d))
        if L < 1e-3:
            return True
        for s in np.arange(0.1, max(0.1, L - skip_end), 0.04):
            p = eye + d * (s / L)
            if self.at(p[:2]) > p[2] + 0.02:
                return False
        return True


# --- IO: Isaac Sim ----------------------------------------------------------------------------------------------------
class IsaacIO:
    def __init__(self, app, stage, kin, spawn):
        from isaacsim.core.experimental.prims import Articulation  # noqa: PLC0415

        self.app, self.stage, self.kin = app, stage, kin
        self.art = Articulation(ROBOT)
        self.names = list(self.art.dof_names)
        self.idx = {n: i for i, n in enumerate(self.names)}
        self.wheels = [w["joint"] for w in kin.k["wheels"]]
        self.ctrl_idx = [self.idx[n] for n in CTRL]
        self.wheel_idx = [self.idx[n] for n in self.wheels]
        self.bodies = {}
        self._boxes = {}
        self._yaw = None
        q0 = {**{n: 0.0 for n in CTRL}, **kin.k["poses"]["stow"]}
        q0.update({"base_x": spawn[0], "base_y": spawn[1], "base_yaw": spawn[2]})
        self.art.set_dof_positions([[q0[n] for n in CTRL]], dof_indices=self.ctrl_idx)
        self.art.set_dof_position_targets([[q0[n] for n in CTRL]], dof_indices=self.ctrl_idx)
        self.q0 = q0
        self.eff_fn = None
        for nm in ("get_dof_projected_joint_forces", "get_measured_joint_efforts", "get_dof_efforts"):
            f = getattr(self.art, nm, None)
            if f is None:
                continue
            try:
                v = f()
                v = np.asarray(v.numpy() if hasattr(v, "numpy") else v, float).reshape(-1)
                if v.size >= len(self.names):
                    self.eff_fn = nm
                    break
            except Exception:  # noqa: BLE001
                pass

    def apply(self, cmd_q, cmd_v, wheel_v):
        # keep the yaw target within half a turn of the measured yaw: the mission's yaw is continuous, PhysX may
        # report the unlimited revolute joint wrapped; either way the drive then sees the short error
        q = dict(cmd_q)
        m = self._yaw if self._yaw is not None else q["base_yaw"]
        q["base_yaw"] = m + wrap(q["base_yaw"] - m)
        self.art.set_dof_position_targets([[q[n] for n in CTRL]], dof_indices=self.ctrl_idx)
        self.art.set_dof_velocity_targets([[cmd_v[n] for n in CTRL]], dof_indices=self.ctrl_idx)
        self.art.set_dof_velocity_targets([wheel_v], dof_indices=self.wheel_idx)

    def update(self):
        self.app.update()

    def q(self) -> dict:
        v = self.art.get_dof_positions().numpy()[0]
        out = {n: float(v[i]) for n, i in self.idx.items()}
        self._yaw = out["base_yaw"]
        return out

    def efforts(self):
        if not self.eff_fn:
            return None
        try:
            v = getattr(self.art, self.eff_fn)()
            v = np.asarray(v.numpy() if hasattr(v, "numpy") else v, float).reshape(-1)
            return {n: float(v[self.idx[n]]) for n in ARM + ["gripper"]}
        except Exception:  # noqa: BLE001
            return None

    # products
    def body(self, path):
        from isaacsim.core.experimental.prims import RigidPrim  # noqa: PLC0415

        if path not in self.bodies:
            self.bodies[path] = RigidPrim(path)
        return self.bodies[path]

    def body_T(self, path) -> np.ndarray:
        p, o = self.body(path).get_world_poses()
        p, o = p.numpy()[0], o.numpy()[0]
        T = np.eye(4)
        T[:3, :3] = quat_to_R(*[float(v) for v in o])
        T[:3, 3] = p
        return T

    def set_body_T(self, path, T):
        b = self.body(path)
        b.set_world_poses(positions=[T[:3, 3].tolist()], orientations=[R_to_quat(T[:3, :3])])
        b.set_velocities(linear_velocities=[[0.0, 0.0, 0.0]], angular_velocities=[[0.0, 0.0, 0.0]])

    def box(self, path):
        """(lo, hi, M): the product's untransformed box and the box frame in its body frame"""
        if path not in self._boxes:
            from pxr import UsdGeom  # noqa: PLC0415

            prim = self.stage.GetPrimAtPath(path)
            bc = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
            bb = bc.ComputeRelativeBound(prim, prim)
            r = bb.GetRange()
            self._boxes[path] = (np.array(r.GetMin()), np.array(r.GetMax()), np.array(bb.GetMatrix()).T)
        return self._boxes[path]

    def make_dynamic(self, path):
        prim = self.stage.GetPrimAtPath(path)
        if prim.GetAttribute("physics:kinematicEnabled").Get():
            prim.GetAttribute("physics:kinematicEnabled").Set(False)
            self.bodies.pop(path, None)
            return True
        return False

    def set_gravity(self, path, on: bool, vz: float = 0.0):
        b = self.body(path)
        b.set_enabled_gravities([bool(on)])
        if on:
            b.set_velocities(linear_velocities=[[0.0, 0.0, vz]], angular_velocities=[[0.0, 0.0, 0.0]])


# --- IO: offline (perfect tracking, products as boxes) ----------------------------------------------------------------
class FakeIO:
    def __init__(self, kin, spawn, smap, counter_top):
        self.kin = kin
        self.qd = {**{n: 0.0 for n in CTRL}, **kin.k["poses"]["stow"]}
        self.qd.update({"base_x": spawn[0], "base_y": spawn[1], "base_yaw": spawn[2]})
        self.T = {}
        self.boxes = {}
        for p in smap.products:
            T = np.eye(4)
            T[:3, 3] = p.center
            self.T[p.path] = T
            self.boxes[p.path] = ((p.lo - p.center), (p.hi - p.center), np.eye(4))
        self.falling = {}
        self.riding = {}
        self.bk = kin.k["basket"]
        self.counter_top = counter_top
        self.dyn = set()
        self.eff_fn = None

    def apply(self, cmd_q, cmd_v, wheel_v):
        self.qd.update({n: cmd_q[n] for n in CTRL})

    def update(self):
        Tb = base_T(self.qd["base_x"], self.qd["base_y"], self.qd["base_yaw"])
        for path, rel in self.riding.items():
            self.T[path] = Tb @ rel
        for path in list(self.falling):
            lo, hi, M = self.boxes[path]
            T = self.T[path]
            zmin = (T @ M @ box_corners(lo, hi).T)[2].min()
            pb = np.linalg.inv(Tb) @ np.r_[T[:3, 3], 1.0]
            inside = math.hypot(pb[0] - self.bk["center"][0], pb[1] - self.bk["center"][1]) < self.bk["r_top"]
            floor = self.bk["floor_z"] if inside else 0.0
            if abs(T[1, 3] - 4.25) < 0.3 and T[2, 3] > 0.7 and not inside:
                floor = self.counter_top
            cs = (T @ M @ box_corners(lo, hi).T)[:2]
            for other, rel in self.riding.items():  # crude pile: xy footprints overlap -> land on top
                To = Tb @ rel
                lo2, hi2, M2 = self.boxes[other]
                co = To @ M2 @ box_corners(lo2, hi2).T
                if (
                    cs[0].min() < co[0].max()
                    and co[0].min() < cs[0].max()
                    and cs[1].min() < co[1].max()
                    and co[1].min() < cs[1].max()
                ):
                    floor = max(floor, co[2].max())
            dz = min(self.falling[path] * DT, zmin - floor)
            self.falling[path] += 9.81 * DT
            T[2, 3] -= max(dz, 0.0)
            if zmin - floor <= 1e-4:
                del self.falling[path]
                if inside:
                    self.riding[path] = np.linalg.inv(Tb) @ T

    def q(self):
        return dict(self.qd)

    def efforts(self):
        return None

    def body_T(self, path):
        return self.T[path].copy()

    def set_body_T(self, path, T):
        self.T[path] = np.array(T)

    def box(self, path):
        return self.boxes[path]

    def make_dynamic(self, path):
        self.dyn.add(path)
        self.riding.pop(path, None)
        return True

    def set_gravity(self, path, on, vz=0.0):
        if on:
            self.falling[path] = max(0.0, -vz)
        else:
            self.falling.pop(path, None)
            self.riding.pop(path, None)


# --- recorder ---------------------------------------------------------------------------------------------------------
class Recorder:
    """Replicator render products + rgb annotators; JPEGs written by a thread pool. Render products only update on the
    physics steps that get captured (every 2nd step = 30 fps of sim time)."""

    def __init__(self, out: Path, cams: dict):
        import omni.replicator.core as rep  # noqa: PLC0415

        self.out = out
        self.pool = ThreadPoolExecutor(8)
        self.rps, self.ann = {}, {}
        for name, (path, res) in cams.items():
            (out / name).mkdir(parents=True, exist_ok=True)
            rp = rep.create.render_product(path, tuple(res))
            an = rep.AnnotatorRegistry.get_annotator("rgb")
            an.attach([rp])
            self.rps[name], self.ann[name] = rp, an
        self.active = set(cams)
        self.frame = 0
        self.on = None

    def render(self, on: bool):
        # Isaac 6.0: re-enabling a render product's updates leaks its GPU textures (OOM after ~100 toggled captures),
        # so active cameras render every step and only the set of active cameras changes (intro / main / outro)
        on = True
        if on == self.on:
            return
        for n, rp in self.rps.items():
            try:
                rp.hydra_texture.set_updates_enabled(on and n in self.active)
            except Exception:  # noqa: BLE001
                pass
        self.on = on

    def set_active(self, names):
        self.active = set(names)
        self.on = None

    def capture(self):
        from PIL import Image  # noqa: PLC0415

        for n in self.active:
            d = self.ann[n].get_data()
            if d is None or getattr(d, "size", 0) == 0:
                continue
            arr = np.asarray(d)[..., :3].copy()
            p = self.out / n / f"{self.frame:06d}.jpg"
            self.pool.submit(lambda a=arr, p=p: Image.fromarray(a).save(p, quality=92))
        self.frame += 1

    def close(self):
        self.pool.shutdown(wait=True)


# --- camera director --------------------------------------------------------------------------------------------------
class Director:
    """One world camera for the main video: chase while driving, a held framing per pick / checkout chosen so the eye
    is in free air (not inside a shelf) and sees the arm, the product and the basket; cuts instead of flying through
    shelves."""

    def __init__(self, hg: HeightGrid, set_cam):
        self.hg, self.set_cam = hg, set_cam
        self.eye = None
        self.tgt = None
        self.mode = "chase"
        self.hold = None  # (eye, target) for held shots
        self.shot = 0
        self.focal = 16.0

    def cut(self, eye, tgt, focal=None):
        self.eye, self.tgt = np.array(eye, float), np.array(tgt, float)
        if focal:
            self.focal = focal
        self.shot += 1

    def frame_work(self, work, base_xy, yaw, extra=()):
        """held framing for a pick: the shelf is on the robot's right, so look from the aisle on its left/back"""
        fwd = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        left = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
        base = np.array([base_xy[0], base_xy[1], 0.55])
        tgt = 0.55 * np.asarray(work, float) + 0.45 * base
        tgt[2] = 0.5 * work[2] + 0.5 * 0.62
        subjects = [np.asarray(work, float), base + np.array([0, 0, 0.0]), *extra]
        best = None
        for dist in (1.75, 1.5, 1.25, 2.05, 1.05):
            for az in (35, 20, 50, 5, 65, -10, 80, -25):
                a = math.radians(az)
                d = math.cos(a) * left - math.sin(a) * fwd  # az > 0: towards behind the robot (basket side)
                for z in (1.55, 1.75, 1.95, 1.35, 2.15):
                    eye = tgt + d * dist
                    eye[2] = z
                    if not self.hg.eye_ok(eye):
                        continue
                    if all(self.hg.visible(eye, s) for s in subjects):
                        best = eye
                        break
                if best is not None:
                    break
            if best is not None:
                break
        if best is None:
            best = tgt + left * 1.2 + np.array([0, 0, 1.4])
            best[2] = 2.3
        self.mode = "hold"
        self.hold = (best, tgt)
        self.cut(best, tgt, focal=17.0)

    def chase_goal(self, x, y, yaw):
        fwd = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        left = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
        p = np.array([x, y, 0.0])
        tgt = p + fwd * 0.9 + np.array([0, 0, 0.55])
        for back, side, z in (
            (1.75, 0.55, 1.65),
            (1.6, -0.5, 1.7),
            (1.4, 0.3, 1.95),
            (1.15, 0.0, 2.2),
            (0.9, 0.0, 2.35),
        ):
            eye = p - fwd * back + left * side + np.array([0, 0, z])
            if self.hg.eye_ok(eye, clear=0.2) and self.hg.visible(eye, p + np.array([0, 0, 0.6])):
                return eye, tgt
        return p - fwd * 0.8 + np.array([0, 0, 2.35]), tgt

    def update(self, x, y, yaw, held_drift=0.0):
        if self.mode == "chase":
            eye, tgt = self.chase_goal(x, y, yaw)
            if self.eye is None:
                self.cut(eye, tgt, focal=16.0)
            else:
                ne = self.eye + (eye - self.eye) * 0.05
                nt = self.tgt + (tgt - self.tgt) * 0.10
                if not self.hg.eye_ok(ne, clear=0.12) or not self.hg.visible(ne, np.array([x, y, 0.6])):
                    self.cut(eye, tgt, focal=16.0)
                else:
                    self.eye, self.tgt = ne, nt
            self.focal += (16.0 - self.focal) * 0.05
        elif self.mode == "hold" and self.hold is not None:
            e, t = self.hold
            self.eye = e + (t - e) * held_drift  # a slow push-in
            self.tgt = t
        elif self.mode == "orbit" and self.hold is not None:
            pass
        self.set_cam(self.eye, self.tgt, self.focal)


# --- follow cameras: bird's-eye + side --------------------------------------------------------------------------------
class FollowCams:
    """Two extra world cameras that track the rover every captured frame: a steep bird's-eye view from just under
    the ceiling (the top-down render at 2.55 m sees the floor, so 2.45 m is free air above the aisle) and a lateral
    side view at arm height. Both eyes are checked against the occlusion grid (free air, line of sight to the rover)
    and cut to a new valid spot instead of clipping through a shelf."""

    BIRD_Z = 2.45
    SIDE_AZ = (0, 25, -25, 180, 155, -155, 50, -50, 130, -130)  # 0 = the rover's left, >0 towards behind it
    SIDE_DIST = (1.9, 1.6, 1.3, 1.05, 0.85)
    SIDE_Z = (1.15, 1.35, 0.95)

    def __init__(self, hg: HeightGrid, set_bird, set_side):
        self.hg, self.set_bird, self.set_side = hg, set_bird, set_side
        self.b_eye = self.b_tgt = None
        self.s_eye = self.s_tgt = None
        self.s_key = None  # (az, dist, z) of the current side shot
        self.cuts = {"birdseye": 0, "side": 0}

    @staticmethod
    def _frame(x, y, yaw):
        fwd = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        left = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
        return np.array([x, y, 0.0]), fwd, left

    def bird_ok(self, eye) -> bool:
        m, hg = self.hg.m, self.hg
        if np.any(eye[:2] < m.lo + 0.05) or np.any(eye[:2] > m.hi - 0.05):
            return False
        r = int(math.ceil(0.2 / m.res))
        i, j = m.cell(eye[:2])
        win = hg.h[max(i - r, 0) : i + r + 1, max(j - r, 0) : j + r + 1]
        return win.size > 0 and float(win.max()) < eye[2] - 0.2

    def bird_goal(self, p, fwd, left):
        tgt = p + fwd * 0.2 + np.array([0, 0, 0.35])
        for back, side in ((0.7, 0.0), (0.45, 0.0), (0.7, 0.35), (0.7, -0.35), (0.2, 0.0), (0.0, 0.3), (0.0, -0.3)):
            eye = p - fwd * back + left * side + np.array([0, 0, self.BIRD_Z])
            if self.bird_ok(eye):
                return eye, tgt
        return p - fwd * 0.3 + np.array([0, 0, self.BIRD_Z]), tgt

    def side_eye(self, key, p, fwd, left):
        az, dist, z = key
        a = math.radians(az)
        d = math.cos(a) * left - math.sin(a) * fwd
        eye = p + d * dist
        eye[2] = z
        return eye

    def side_valid(self, eye, p) -> bool:
        return self.hg.eye_ok(eye, clear=0.15) and self.hg.visible(eye, p + np.array([0, 0, 0.6]), skip_end=0.3)

    def side_pick(self, p, fwd, left):
        for az in self.SIDE_AZ:
            for dist in self.SIDE_DIST:
                for z in self.SIDE_Z:
                    key = (az, dist, z)
                    if self.side_valid(self.side_eye(key, p, fwd, left), p):
                        return key
        return None

    def update(self, x, y, yaw):
        p, fwd, left = self._frame(x, y, yaw)
        # bird's-eye
        eye, tgt = self.bird_goal(p, fwd, left)
        if self.b_eye is None:
            self.b_eye, self.b_tgt = eye, tgt
        else:
            ne = self.b_eye + (eye - self.b_eye) * 0.08
            if not self.bird_ok(ne):
                ne = eye
                self.cuts["birdseye"] += 1
            self.b_eye, self.b_tgt = ne, self.b_tgt + (tgt - self.b_tgt) * 0.15
        self.set_bird(self.b_eye, self.b_tgt, 9.0)
        # side
        tgt = p + fwd * 0.05 + np.array([0, 0, 0.7])
        key = self.s_key
        if key is None or not self.side_valid(self.side_eye(key, p, fwd, left), p):
            key = self.side_pick(p, fwd, left)
            self.s_key = key
        if key is None:  # nowhere free at arm height: fall back to a high three-quarter view
            eye = p - fwd * 0.9 + left * 0.5 + np.array([0, 0, 2.2])
        else:
            eye = self.side_eye(key, p, fwd, left)
        if self.s_eye is None:
            self.s_eye, self.s_tgt = eye, tgt
        else:
            ne = self.s_eye + (eye - self.s_eye) * 0.08
            if not self.side_valid(ne, p):
                ne = eye
                self.cuts["side"] += 1
            self.s_eye, self.s_tgt = ne, self.s_tgt + (tgt - self.s_tgt) * 0.2
        dist = float(np.linalg.norm((self.s_eye - self.s_tgt)[:2]))
        self.set_side(self.s_eye, self.s_tgt, float(np.clip(7.0 + 3.0 * dist, 9.0, 13.0)))


# --- the mission ------------------------------------------------------------------------------------------------------
class Mission:
    def __init__(self, io, kin, smap, cfg, out: Path, hg: HeightGrid, set_cam=None, rec=None, log=print):
        self.io, self.kin, self.K, self.smap, self.cfg, self.out = io, kin, kin.k, smap, cfg, out
        self.log = log
        self.pan_x = float(self.K["pan_axis_xy"][0])
        self.vmax = {n: self.K["actuators"][n]["vmax_rad_s"] for n in ARM + ["gripper"]}
        self.cmd_q = {n: float(io.q()[n]) for n in CTRL}
        self.cmd_v = {n: 0.0 for n in CTRL}
        self.traj = collections.deque()
        self.base_v, self.base_w = 0.0, 0.0
        self.t, self.k = 0.0, 0
        self.qm = io.q()
        self.held = None  # (path, rel gripper->body)
        self.cargo: list[str] = []
        self.on_counter: list[str] = []
        self.done: list[str] = []
        self.queue: list[dict] = []
        self.phase, self.sub, self.target, self.gphase = "idle", "", None, "main"
        self.events = (out / "events.jsonl").open("a")
        self.frames = (out / "frames.jsonl").open("a")
        self.rec = rec
        self.hg = hg
        self.director = Director(hg, set_cam) if set_cam else None
        self.cap_every = 2  # record every Nth physics step (2 = 30 fps of sim time)
        self.cam_hooks = []  # callables (x, y, yaw) run for every captured frame (follow cameras)
        self.drift = 0.0
        self.path = []
        self.drop_k = 0
        self.eff_max = {n: 0.0 for n in ARM + ["gripper"]}
        self.stats = {"grasp_ok": 0, "grasp_fail": 0, "in_basket": 0, "missed_basket": 0, "replans": 0}
        self.tracked: set[str] = set()  # products that have moved (logged per frame)
        self.FJ = np.array(self.K["fixed_jaw_points"])
        self.MJ = np.array(self.K["moving_jaw_points"])
        self._jaw_table()

    # ---- logging
    def ev(self, kind, **kw):
        kw = {k: (r2(v) if isinstance(v, np.ndarray) else v) for k, v in kw.items()}
        rec = {"t": round(self.t, 3), "kind": kind, "robot": NAME, **kw}
        self.events.write(json.dumps(rec) + "\n")
        self.events.flush()
        self.log(f"[so101-demo] {json.dumps(rec)[:300]}")

    # ---- kinematics on the measured state
    def Tb(self, q=None):
        q = q or self.qm
        return base_T(q["base_x"], q["base_y"], q["base_yaw"])

    def arm_vec(self, q=None):
        q = q or self.qm
        return np.array([q[n] for n in ARM])

    def world_gripper(self, q=None):
        return self.Tb(q) @ self.kin.links(self.arm_vec(q))["so101_gripper"]

    def world_tcp(self, q=None):
        return self.Tb(q) @ self.kin.fk(self.arm_vec(q))

    def to_base(self, pw, q=None):
        return (np.linalg.inv(self.Tb(q)) @ np.r_[np.asarray(pw, float)[:3], 1.0])[:3]

    def corners_w(self, path, T=None):
        lo, hi, M = self.io.box(path)
        T = self.io.body_T(path) if T is None else T
        return (T @ M @ box_corners(lo, hi).T).T[:, :3]

    def center_w(self, path, T=None):
        lo, hi, M = self.io.box(path)
        T = self.io.body_T(path) if T is None else T
        return (T @ M @ np.r_[(lo + hi) / 2, 1.0])[:3]

    # ---- the step
    def step(self):
        if self.traj:
            tq = self.traj.popleft()
            for n, (qq, vv) in tq.items():
                self.cmd_q[n], self.cmd_v[n] = qq, vv
            if not self.traj:
                for n in ARM + ["gripper"]:
                    self.cmd_v[n] = 0.0
        self.io.apply(self.cmd_q, self.cmd_v, self.wheel_speeds())
        if self.held is not None:
            # place the held product where the gripper will be after this physics step (measured state + commanded
            # velocity), so the rendered frame shows it in the jaws without a one-step lag
            path, rel = self.held
            q_pred = {n: self.qm[n] + self.cmd_v.get(n, 0.0) * DT for n in self.qm}
            self.io.set_body_T(path, self.world_gripper(q_pred) @ rel)
        capture = (self.k + 1) % self.cap_every == 0
        if capture and self.director is not None:  # camera for the frame this step renders
            q = self.qm
            self.drift = min(self.drift + 0.0005, 0.12) if self.director.mode == "hold" else 0.0
            self.director.update(q["base_x"], q["base_y"], q["base_yaw"], self.drift)
        if capture:
            for f in self.cam_hooks:
                f(self.qm["base_x"], self.qm["base_y"], self.qm["base_yaw"])
        if self.rec is not None:
            self.rec.render(capture)
        self.io.update()
        self.t += DT
        self.k += 1
        self.qm = self.io.q()
        if self.k % 6 == 0:
            e = self.io.efforts()
            if e:
                for n, v in e.items():
                    self.eff_max[n] = max(self.eff_max[n], abs(v))
        if self.k % self.cap_every == 0:
            q = self.qm
            if self.rec is not None:
                self.rec.capture()
            moved = {}
            for p in self.tracked:
                T = self.io.body_T(p)
                moved[p] = r2(T[:3, 3], 4) + [round(v, 4) for v in R_to_quat(T[:3, :3])]
            d = self.director
            self.frames.write(
                json.dumps(
                    {
                        "f": (self.rec.frame - 1) if self.rec else self.k // self.cap_every,
                        "t": round(self.t, 3),
                        "phase": self.gphase,
                        "shot": d.shot if d else 0,
                        "mode": d.mode if d else "",
                        "cam": [r2(d.eye), r2(d.tgt), round(d.focal, 2)] if d and d.eye is not None else None,
                        "q": [round(q[n], 4) for n in ARM + ["gripper"]],
                        "moved": moved,
                        "rovers": [
                            {
                                "name": NAME,
                                "x": round(q["base_x"], 3),
                                "y": round(q["base_y"], 3),
                                "yaw": round(q["base_yaw"], 3),
                                "phase": self.phase,
                                "sub": self.sub,
                                "target": self.target,
                                "held": self.held is not None,
                                "cargo": len(self.cargo),
                                "done": list(self.done),
                                "queue": [s["item"] for s in self.queue],
                                "counter": len(self.on_counter),
                            }
                        ],
                    }
                )
                + "\n"
            )
            if self.k % 60 == 0:
                self.frames.flush()

    def wait(self, s):
        for _ in range(max(1, int(round(s / DT)))):
            self.step()

    def wait_arm(self):
        while self.traj:
            self.step()

    # ---- arm trajectories (queued; consumed one sample per physics step)
    def _last(self, names):
        last = {n: (self.cmd_q[n], 0.0) for n in names}
        for tq in self.traj:
            for n in names:
                if n in tq:
                    last[n] = tq[n]
        return {n: v[0] for n, v in last.items()}

    def plan_move(self, target: dict, T=None, vscale=0.8, tmin=0.5):
        names = list(target)
        q0 = self._last(names)
        dq = {n: target[n] - q0[n] for n in names}
        if T is None:
            T = max(tmin, max(1.875 * abs(dq[n]) / (vscale * self.vmax.get(n, 1.0)) for n in names))
        n_steps = max(1, int(round(T / DT)))
        for i in range(1, n_steps + 1):
            s = i / n_steps
            a, da = minjerk(s), minjerk_vel(s) / T
            self.traj.append({n: (q0[n] + dq[n] * a, dq[n] * da) for n in names})
        return T

    def move(self, target: dict, T=None, vscale=0.8, label=None):
        T = self.plan_move(target, T, vscale)
        self.wait_arm()
        if label:
            err = max(abs(self.qm[n] - target[n]) for n in target)
            self.ev("move", label=label, T=round(T, 2), final_err=round(err, 4))
        return T

    def plan_path(self, qs, T):
        """joint waypoints (arm vectors) along a straight tool line, one min-jerk time scaling; returns end q"""
        W = np.array(qs)
        seg = np.r_[0, np.cumsum(np.linalg.norm(np.diff(W, axis=0), axis=1))]
        seg = seg / seg[-1] if seg[-1] > 1e-9 else np.linspace(0, 1, len(W))
        n_steps = max(1, int(round(T / DT)))
        prev = W[0]
        for i in range(1, n_steps + 1):
            u = minjerk(i / n_steps)
            qv = np.array([np.interp(u, seg, W[:, j]) for j in range(5)])
            self.traj.append({n: (float(qv[j]), float((qv[j] - prev[j]) / DT)) for j, n in enumerate(ARM)})
            prev = qv
        return W[-1]

    def arm_dict(self, qv, g=None):
        d = {n: float(v) for n, v in zip(ARM, qv, strict=False)}
        if g is not None:
            d["gripper"] = float(g)
        return d

    def cart_path(self, q0, q1, n=14, bulge=0.04):
        """joint waypoints q0 -> q1 along a Cartesian blend (TCP position lerp + a small lift, tool rotation slerp),
        each IK seeded by the previous one so the arm stays on one branch: no elbow flips mid-move. None if a waypoint
        is unreachable."""
        T0, T1 = self.kin.fk(q0), self.kin.fk(q1)
        q = np.array(q0, float)
        out = [q.copy()]
        for s_ in np.linspace(0, 1, n)[1:]:
            p = (1 - s_) * T0[:3, 3] + s_ * T1[:3, 3] + np.array([0, 0, bulge * math.sin(math.pi * s_)])
            R = slerp_R(T0[:3, :3], T1[:3, :3], s_)
            q, e = self.kin.solve(p, R=R, w_rot=0.25, seeds=[q])
            if e[0] > 0.01 or e[1] > 0.12:
                return None
            out.append(q.copy())
        return out

    def move_cart(self, q1, n=14, bulge=0.04, vscale=0.75):
        """Cartesian-blend move from the end of the queued trajectory to q1 (joint move if the blend fails);
        returns the final joint vector"""
        q0 = np.array([self._last(ARM)[n_] for n_ in ARM])
        path = self.cart_path(q0, q1, n=n, bulge=bulge)
        if path is None:
            self.plan_move(self.arm_dict(q1), vscale=vscale)
            return np.array(q1, float)
        dq = np.abs(np.diff(np.array(path), axis=0)).sum(axis=0)
        T = max(0.8, float(np.max(1.875 * dq / (vscale * self.kin.vmax))) * 1.1)
        self.plan_path(path, T=T)
        return np.array(path[-1], float)

    def line(self, q_start, p0, p1, R, n=10):
        out = self.kin.line(q_start, p0, p1, R=R, n=n, w_rot=0.1)
        err = max(e[0] for _, e in out)
        return [q for q, _ in out], err

    # ---- base: differential drive
    def wheel_speeds(self):
        """differential drive: wheel spin from the base twist (rolling without slip)"""
        out = []
        for w in self.K["wheels"]:
            px, py = w["center"][0], w["center"][1]
            roll_dir = np.cross(np.asarray(w["axis"], float), [0.0, 0.0, 1.0])
            v_hub = np.array([self.base_v - self.base_w * py, self.base_w * px, 0.0])
            out.append(float(np.dot(v_hub, roll_dir)) / w["radius"])
        return out

    def set_base(self, x, y, yaw, v, w):
        self.cmd_q["base_x"], self.cmd_q["base_y"], self.cmd_q["base_yaw"] = x, y, yaw
        self.cmd_v["base_x"], self.cmd_v["base_y"] = v * math.cos(yaw), v * math.sin(yaw)
        self.cmd_v["base_yaw"] = w
        self.base_v, self.base_w = v, w

    def turn_to(self, yaw_goal, vscale=0.75):
        x, y, yaw0 = self.cmd_q["base_x"], self.cmd_q["base_y"], self.cmd_q["base_yaw"]
        yaw1 = yaw0 + wrap(yaw_goal - yaw0)
        dy = yaw1 - yaw0
        if abs(dy) < 0.01:
            return
        T = max(0.8, 1.875 * abs(dy) / (vscale * 0.9))
        n = max(1, int(round(T / DT)))
        for i in range(1, n + 1):
            s = i / n
            self.set_base(x, y, yaw0 + dy * minjerk(s), 0.0, dy * minjerk_vel(s) / T)
            self.step()
        self.set_base(x, y, yaw1, 0.0, 0.0)

    def drive_to(self, goal_xy, goal_yaw, vmax=0.5, label=""):
        start = np.array([self.cmd_q["base_x"], self.cmd_q["base_y"]])
        path = self.smap.astar(start, np.asarray(goal_xy, float))
        if path is None:
            return False
        pts = [np.asarray(p, float) for p in path]
        self.path = pts
        self.ev("path", points=[r2(p, 2) for p in pts], label=label)
        dense = [pts[0]]
        for a, b in zip(pts, pts[1:], strict=False):
            L = float(np.linalg.norm(b - a))
            for s in np.arange(0.05, L, 0.05):
                dense.append(a + (b - a) * (s / L))
            dense.append(b)
        dense = np.array(dense)
        seglen = np.r_[0, np.cumsum(np.linalg.norm(np.diff(dense, axis=0), axis=1))]
        total = float(seglen[-1])
        x, y, yaw = self.cmd_q["base_x"], self.cmd_q["base_y"], self.cmd_q["base_yaw"]
        v, w = 0.0, 0.0
        amax, wmax, alpha_max, Ld = 0.45, 0.9, 1.6, 0.45
        idx = 0
        t0 = self.t
        # first: face the path (turn in place) if it starts behind us
        if total > 0.05:
            la = dense[min(len(dense) - 1, int(np.searchsorted(seglen, min(total, Ld))))]
            h = math.atan2(la[1] - y, la[0] - x)
            if abs(wrap(h - yaw)) > 0.5:
                self.turn_to(h)
                yaw = self.cmd_q["base_yaw"]
        while True:
            pos = np.array([x, y])
            win = dense[idx : idx + 40]
            idx += int(np.argmin(np.linalg.norm(win - pos, axis=1)))
            s_la = min(total, seglen[idx] + Ld)
            la = dense[min(len(dense) - 1, int(np.searchsorted(seglen, s_la)))]
            dist_goal = float(np.linalg.norm(dense[-1] - pos))
            remaining = max(total - seglen[idx], dist_goal)
            if dist_goal < 0.012 or (idx >= len(dense) - 3 and dist_goal < 0.05):
                break
            alpha = wrap(math.atan2(la[1] - y, la[0] - x) - yaw)
            v_des = min(vmax, math.sqrt(2 * amax * max(remaining - 0.004, 0.0)) + 0.015)
            v_des *= max(0.0, math.cos(min(abs(alpha), math.pi / 2))) ** 2
            if abs(alpha) > alpha_max * 0.5:
                v_des = min(v_des, 0.05)
            Lla = max(float(np.linalg.norm(la - pos)), 0.1)
            w_des = 2.0 * max(v_des, 0.12) * math.sin(alpha) / Lla
            w_des = float(np.clip(w_des, -wmax, wmax))
            v += float(np.clip(v_des - v, -amax * DT, amax * DT))
            w += float(np.clip(w_des - w, -2.0 * DT, 2.0 * DT))
            yaw += w * DT
            x += v * math.cos(yaw) * DT
            y += v * math.sin(yaw) * DT
            self.set_base(x, y, yaw, v, w)
            self.step()
            if self.t - t0 > 150:
                self.ev("drive_timeout")
                break
        # settle the last few mm with a short min-jerk move, then face the goal yaw
        x0, y0 = x, y
        gx, gy = float(goal_xy[0]), float(goal_xy[1])
        n = int(round(0.5 / DT))
        for i in range(1, n + 1):
            s = minjerk(i / n)
            self.set_base(x0 + (gx - x0) * s, y0 + (gy - y0) * s, yaw, 0.0, 0.0)
            self.step()
        self.set_base(gx, gy, yaw, 0.0, 0.0)
        self.turn_to(goal_yaw)
        self.path = []
        return True

    # ---- jaws
    def _jaw_table(self):
        """inner face of the moving jaw pad (TCP frame x) for each gripper angle"""
        q = np.array([-1.5708, -0.4, 0.65, -0.25, self.kin.grasp_roll])
        L = self.kin.links(q)
        inv = np.linalg.inv(L["tcp"])
        gs = np.arange(-0.17, 1.70, 0.01)
        xs = []
        for g in gs:
            m = (inv @ self.kin.jaw(q, g) @ np.c_[self.MJ, np.ones(len(self.MJ))].T).T[:, :3]
            sel = np.abs(m[:, 2]) < 0.02
            xs.append(float(m[sel, 0].max()) if sel.any() else 0.0)
        self.jaw_g, self.jaw_x = gs, np.array(xs)

    def g_open_for(self, width, clear=0.011):
        need = 0.0005 - width - clear
        ok = np.nonzero(self.jaw_x <= need)[0]
        g = float(self.jaw_g[ok[0]]) if len(ok) else 1.4
        return float(min(max(g + 0.04, 0.5), 1.45))

    def jaw_points_world(self, q, g):
        Tg = self.world_gripper(q)
        Tj = self.Tb(q) @ self.kin.jaw(self.arm_vec(q), g)
        f = (Tg @ np.c_[self.FJ, np.ones(len(self.FJ))].T).T[:, :3]
        m = (Tj @ np.c_[self.MJ, np.ones(len(self.MJ))].T).T[:, :3]
        return f, m

    def in_box(self, pts, path, margin=0.0):
        lo, hi, M = self.io.box(path)
        T = self.io.body_T(path) @ M
        P = (np.linalg.inv(T) @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
        return np.all((P >= lo - margin) & (P <= hi + margin), axis=1)

    def contact_angle(self, path, g_open):
        q = dict(self.qm)
        for g in np.arange(g_open, -0.18, -0.005):
            _, m = self.jaw_points_world(q, g)
            if self.in_box(m, path, margin=0.0005).any():
                return float(g)
        return None

    def jaw_check(self, path):
        q = self.qm
        g = q["gripper"]
        f, m = self.jaw_points_world(q, g)
        touch_m = bool(self.in_box(m, path, margin=0.004).any())
        touch_f = bool(self.in_box(f, path, margin=0.007).any())
        Ttcp = self.world_tcp(q)
        x_in = 0.5 * (0.002 + float(np.interp(g, self.jaw_g, self.jaw_x)))
        mid = (Ttcp @ np.array([x_in, 0.0, 0.0, 1.0]))[:3]
        mid_in = bool(self.in_box(mid[None], path, margin=0.004)[0])
        return {
            "moving_jaw_touch": touch_m,
            "fixed_jaw_touch": touch_f,
            "pads_around_it": mid_in,
            "jaw": round(g, 3),
            "ok": touch_m and touch_f and mid_in,
        }

    def attach(self, path):
        self.tracked.add(path)
        if self.io.make_dynamic(path):
            self.step()
        self.io.set_gravity(path, False)
        rel = np.linalg.inv(self.world_gripper()) @ self.io.body_T(path)
        self.held = (path, rel)
        return rel

    def release(self, path):
        self.held = None
        self.io.set_gravity(path, True, vz=-0.05)

    # ---- shelf pick
    def shelf_plan(self, path, side, pitch_hint=0, roll=None):
        roll = self.kin.grasp_roll if roll is None else roll
        Tb = self.Tb()
        Tbi = np.linalg.inv(Tb)
        c_w = self.center_w(path)
        cw = self.corners_w(path)
        n = np.array([*SIDE_N[side], 0.0])
        pitches = [pitch_hint] + [p for p in PITCHES if p != pitch_hint]
        for pitch in pitches:
            ph = math.radians(pitch)
            a_w = -n * math.cos(ph) + np.array([0, 0, -math.sin(ph)])
            a_b = Tbi[:3, :3] @ a_w
            R = self.kin.target_rotation(a_b, roll)
            close_w = Tb[:3, :3] @ R[:, 0]
            close_h = np.array([close_w[0], close_w[1], 0.0]) / max(np.linalg.norm(close_w[:2]), 1e-6)
            width = float(np.ptp(cw @ close_h))  # across the jaws at the pads (the SO-101 tool x tilts ~3 deg)
            if width > MAX_W + 0.004:
                return None, f"is {width * 100:.1f} cm across: too wide for the SO-101 jaws"
            # along the approach: grab thin products at their centre, deep ones GRIP_IN behind their front face
            along = cw @ a_w
            front, dl = float(along.min()), float(np.ptp(along))
            shift = (front + min(dl / 2, GRIP_IN)) - float(c_w @ a_w)
            off_x = 0.002 - width / 2 - 0.0015
            c_grip = c_w + a_w * shift
            tcp = (Tbi @ np.r_[c_grip, 1.0])[:3] - R[:, 0] * off_x
            qg, e = ik_natural(self.kin, tcp, R, roll=roll)
            if e[0] > 0.003 or e[1] > math.radians(3):
                continue
            far_d = max(0.12, min(dl / 2, GRIP_IN) + 0.09)
            far = tcp - a_b * far_d
            qf, ef = self.kin.solve(far, R=R, w_rot=0.1, seeds=[qg])
            if ef[0] > 0.004:
                continue
            line_in, err_in = self.line(qf, far, tcp, R, n=12)
            if err_in > 0.004:
                continue
            up = tcp + np.array([0, 0, 0.025])
            back = up - np.array([a_b[0], a_b[1], 0.0]) / max(np.linalg.norm(a_b[:2]), 1e-6) * far_d
            return {
                "R": R,
                "tcp": tcp,
                "far": far,
                "qf": qf,
                "qg": qg,
                "line_in": line_in,
                "up": up,
                "back": back,
                "width": width,
                "pitch": pitch,
                "g_open": self.g_open_for(width),
                "a_b": a_b,
            }, ""
        return None, "is out of the arm's reach from this spot"

    def pick_shelf(self, it):
        path = it["path"]
        self.phase, self.sub = "reach", "IK: pre-grasp + straight slide in"
        plan, why = self.shelf_plan(path, it["side"], it.get("pitch", 0), it.get("roll"))
        if plan is None:
            return False, why
        self.ev(
            "ik_grasp",
            item=it["id"],
            pitch=plan["pitch"],
            width_cm=round(plan["width"] * 100, 1),
            g_open=round(plan["g_open"], 2),
            wrist_flip=bool(it.get("flip")),
        )
        qf = plan["qf"]
        # unfold: raise the folded arm towards the shelf side, open the jaws, then to the far pre-grasp
        self.plan_move(
            {
                "shoulder_pan": float(qf[0]),
                "shoulder_lift": -0.3,
                "elbow_flex": 0.9,
                "wrist_flex": 0.4,
                "wrist_roll": float(qf[4]),
            },
            vscale=0.85,
        )
        self.plan_move({"gripper": plan["g_open"]}, T=0.8)
        self.wait_arm()
        self.plan_move(self.arm_dict(qf), vscale=0.8)
        self.wait_arm()
        self.wait(0.15)
        self.sub = "slide in"
        self.plan_path(plan["line_in"], T=1.9)
        self.wait_arm()
        self.wait(0.25)
        g_c = self.contact_angle(path, plan["g_open"])
        if g_c is None:
            self.ev("no_contact", item=it["id"])
            return False, "wasn't between the jaws"
        self.phase, self.sub = "grasp", "closing the jaws"
        self.plan_move({"gripper": g_c}, T=0.9)
        self.wait_arm()
        self.wait(0.25)
        chk = self.jaw_check(path)
        self.ev("grasp_check", item=it["id"], g_contact=round(g_c, 3), **chk)
        if not chk["ok"]:
            self.stats["grasp_fail"] += 1
            self.plan_move({"gripper": plan["g_open"]}, T=0.5)
            self.plan_path(plan["line_in"][::-1], T=1.4)
            self.wait_arm()
            return False, "slipped out of the jaws"
        self.attach(path)
        self.stats["grasp_ok"] += 1
        self.ev("attached", item=it["id"], name=it["name"])
        self.phase, self.sub = "lift", "lift + retract"
        R = plan["R"]
        qs, _ = self.line(plan["qg"], plan["tcp"], plan["up"], R, n=5)
        self.plan_path(qs, T=0.8)
        qs2, _ = self.line(qs[-1], plan["up"], plan["back"], R, n=10)
        self.plan_path(qs2, T=1.4)
        self.wait_arm()
        return True, ""

    # ---- into the basket
    def held_corners_tcp(self):
        """the held product's box corners in the TCP frame"""
        path, rel = self.held
        lo, hi, M = self.io.box(path)
        Tg_tcp = np.array(self.K["tcp_in_gripper_link"])
        return (np.linalg.inv(Tg_tcp) @ rel @ M @ box_corners(lo, hi).T).T[:, :3]

    def down_R(self, psi):
        return Rz(psi) @ np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])

    def place_plan(self, xy_b, z_floor, fit, seeds, n_psi=24):
        """tool pointing down over xy_b; yaw chosen by fit(corners_b) (lower = better); TCP height puts the item's
        lowest corner 2 cm above z_floor. Returns (q, R, z_tcp, corners_b) or None."""
        C = self.held_corners_tcp()
        off = np.array([xy_b[0], xy_b[1], 0.0])
        cands = sorted(
            (fit((self.down_R(p) @ C.T).T + off), p) for p in np.linspace(-math.pi, math.pi, n_psi, endpoint=False)
        )
        for _score, psi in cands:
            R = self.down_R(psi)
            cb = (R @ C.T).T
            z_tcp = z_floor + 0.02 - float(cb[:, 2].min())
            p = np.array([xy_b[0], xy_b[1], z_tcp])
            sd_ = [list(s_[:4]) + [float(wrap(psi + s_[4]))] for s_ in seeds] + list(seeds)
            q, e = self.kin.solve(p, R=R, w_rot=0.25, seeds=sd_)
            if e[0] < 0.004 and e[1] < math.radians(4):
                return q, R, z_tcp, cb + p
        return None

    def basket_fit(self):
        bk = self.K["basket"]
        c = np.array(bk["center"][:2])

        def fit(cb):
            r = np.hypot(cb[:, 0] - c[0], cb[:, 1] - c[1])
            return float(r.max())

        return fit

    def pile_top(self, xy=None, r=0.12):
        """highest basket item (base frame z), only those within r of xy if given"""
        bk = self.K["basket"]
        top = bk["floor_z"]
        for p in self.cargo:
            cb = np.array([self.to_base(c) for c in self.corners_w(p)])
            if xy is not None and np.linalg.norm(cb[:, :2].mean(axis=0) - xy) > r:
                continue
            top = max(top, float(cb[:, 2].max()))
        return top

    def to_basket(self, it):
        bk = self.K["basket"]
        self.phase, self.sub = "basket", "tool down, swing to the basket"
        # drop where the pile is lowest (items spread over the basket floor before they stack)
        c0 = np.array(bk["center"][:2])
        spots = [c0] + [
            c0 + r * np.array([math.cos(a), math.sin(a)])
            for r in (0.05, 0.08)
            for a in np.linspace(0, 2 * math.pi, 8, endpoint=False)
        ]
        xy = min(spots, key=lambda s_: self.pile_top(s_, r=0.07) + 0.3 * float(np.linalg.norm(s_ - c0)))
        self.drop_k += 1
        z_floor = max(bk["floor_z"], self.pile_top(xy, r=0.07) + 0.005)
        seeds = [[1.5708, 0.95, 0.85, -0.23, 0.0], [1.5708, 0.3, 1.07, 0.2, 0.0], [1.5708, -0.07, 1.09, 0.55, 0.0]]
        pl = self.place_plan(xy, z_floor, self.basket_fit(), seeds)
        if pl is None:
            pl = self.place_plan(np.array(bk["center"][:2]), z_floor, self.basket_fit(), seeds)
        if pl is None:
            self.ev("drop_ik_failed", item=it["id"])
            q_hi = np.array([1.5708, -0.07, 1.09, 0.55, 0.0])
            pl = (q_hi, None, None, None)
        q_drop, R, z_tcp, cb = pl
        if R is not None:
            lift = max(0.0, bk["rim_z"] + 0.05 - float(cb[:, 2].min()))
            p_hi = np.array([xy[0], xy[1], z_tcp + lift])
            q_hi, e_hi = self.kin.solve(p_hi, R=R, w_rot=0.25, seeds=[q_drop])
        else:
            q_hi = q_drop
        # carry pose on the right side (the high drop pose with the pan turned 90 deg towards the shelf), reached by a
        # Cartesian blend from the retract pose (tool turns from horizontal to pointing down), then the pan swing
        q_side = np.array(q_hi, float)
        q_side[0] = q_hi[0] - math.pi / 2
        q_end = self.move_cart(q_side, n=16, bulge=0.05)
        self.wait_arm()
        self.sub = "swing to the basket"
        q_sw = np.array(q_end, float)
        q_sw[0] = q_hi[0]
        self.plan_move({"shoulder_pan": float(q_sw[0])}, vscale=0.85)
        self.wait_arm()
        self.sub = "lower into the basket"
        if R is not None:
            p_sw = self.kin.fk(q_sw)[:3, 3]
            line_dn, _ = self.line(q_sw, p_sw, np.array([xy[0], xy[1], z_tcp]), R, n=8)
        else:
            line_dn = [q_sw, q_sw]
        self.plan_path(line_dn, T=0.9)
        self.wait_arm()
        self.wait(0.15)
        path = it["path"]
        self.release(path)
        self.plan_move({"gripper": min(self.qm["gripper"] + 0.35, 1.5)}, T=0.45)
        self.wait_arm()
        self.ev("released", item=it["id"])
        self.wait(0.9)
        cb = self.to_base(self.center_w(path))
        r = math.hypot(cb[0] - bk["center"][0], cb[1] - bk["center"][1])
        inside = bool(r < bk["r_top"] and bk["floor_z"] - 0.03 < cb[2] < bk["rim_z"] + 0.06)
        self.ev("in_basket", item=it["id"], name=it["name"], inside=inside, base_xyz=cb, n=len(self.cargo) + inside)
        if inside:
            self.cargo.append(path)
            self.stats["in_basket"] += 1
        else:
            self.stats["missed_basket"] += 1
        # up out of the basket, then fold for travel (finishes while the base starts driving)
        self.sub = ""
        self.plan_path(line_dn[::-1], T=0.7)
        self.plan_move({**self.travel_pose(), "gripper": 0.0}, vscale=0.85)
        return inside

    def travel_pose(self):
        s = dict(self.K["poses"]["stow"])
        s.pop("gripper", None)
        s["shoulder_pan"] = 0.785
        return s

    # ---- checkout
    def unload_plan(self, path):
        cw = self.corners_w(path)
        cb = np.array([self.to_base(c) for c in cw])
        c_b = cb.mean(axis=0)
        best = None
        for k in range(36):
            psi = -math.pi + 2 * math.pi * k / 36
            R = self.down_R(psi)
            width = float(np.ptp(cb @ R[:, 0]))
            if width > MAX_W + 0.004:
                continue
            off_x = 0.002 - width / 2 - 0.0015
            top = float(cb[:, 2].max())
            z = max(float(c_b[2]), top - 0.03)
            tcp = np.array([c_b[0], c_b[1], z]) - R[:, 0] * off_x
            seeds = [
                [1.5708, 1.09, 0.77, -0.29, psi],
                [1.5708, 0.95, 0.85, -0.23, 0.0],
                [1.5708, 1.05, 0.6, -0.08, 0.0],
            ]
            q, e = self.kin.solve(tcp, R=R, w_rot=0.25, seeds=seeds)
            if e[0] > 0.004 or e[1] > math.radians(4):
                continue
            score = width + 0.02 * abs(wrap(q[4]))
            if best is None or score < best[0]:
                best = (score, R, tcp, q, width)
        if best is None:
            return None
        _, R, tcp, q, width = best
        pre = tcp + np.array([0, 0, 0.12])
        qp, _ = self.kin.solve(pre, R=R, w_rot=0.25, seeds=[q])
        line_dn, err = self.line(qp, pre, tcp, R, n=8)
        return {
            "R": R,
            "tcp": tcp,
            "q": q,
            "qp": qp,
            "pre": pre,
            "line": line_dn,
            "width": width,
            "g_open": self.g_open_for(width),
        }

    def unload_one(self, path, spot_b, counter_top, name):
        self.phase, self.sub = "unload", "taking it out of the basket"
        pl = self.unload_plan(path)
        if pl is None:
            self.ev("unload_skip", item=name, why="no grasp")
            return False
        # over the basket at a safe height first (raise where we are, swing, then down to the pre-grasp)
        bk = self.K["basket"]
        p_safe = np.array([bk["center"][0], bk["center"][1], max(bk["rim_z"], counter_top) + 0.16])
        q_safe, e_safe = self.kin.solve(
            p_safe,
            approach=(0, 0, -1),
            ori_mode="axis",
            seeds=[[1.5708, -0.07, 1.09, 0.55, 0.0], [1.5708, 0.3, 1.07, 0.2, 0.0]],
        )
        if e_safe[0] < 0.01:
            q_raise = np.array(q_safe, float)
            q_raise[0] = self.qm["shoulder_pan"]
            if abs(wrap(q_raise[0] - q_safe[0])) > 0.3:
                self.plan_move(self.arm_dict(q_raise), vscale=0.85)
            self.plan_move(self.arm_dict(q_safe), vscale=0.85)
        self.plan_move({**self.arm_dict(pl["qp"]), "gripper": pl["g_open"]}, vscale=0.85)
        self.wait_arm()
        self.plan_path(pl["line"], T=1.0)
        self.wait_arm()
        self.wait(0.15)
        g_c = self.contact_angle(path, pl["g_open"])
        if g_c is None:
            self.ev("unload_skip", item=name, why="not between the jaws")
            self.plan_path(pl["line"][::-1], T=0.8)
            self.wait_arm()
            return False
        self.plan_move({"gripper": g_c}, T=0.7)
        self.wait_arm()
        self.wait(0.2)
        chk = self.jaw_check(path)
        self.ev("unload_grasp_check", item=name, **chk)
        if not chk["ok"]:
            self.plan_move({"gripper": pl["g_open"]}, T=0.4)
            self.plan_path(pl["line"][::-1], T=0.8)
            self.wait_arm()
            return False
        if path in self.cargo:
            self.cargo.remove(path)
        self.attach(path)
        self.ev("unload_attached", item=name)
        # lift clear of the rim and the counter, swing to the counter side, lay it down
        self.sub = "onto the counter"
        R = pl["R"]
        C = self.held_corners_tcp()
        z_need = max(self.K["basket"]["rim_z"], counter_top) + 0.06
        lo_c = float((R @ C.T).T[:, 2].min())
        p_hi = np.array([pl["tcp"][0], pl["tcp"][1], max(pl["tcp"][2] + 0.10, z_need - lo_c)])
        qs, _ = self.line(pl["q"], pl["tcp"], p_hi, R, n=8)
        self.plan_path(qs, T=1.0)
        self.wait_arm()

        def fit(cb):  # long side across the counter edge (along base y), so items line up along base x
            return float(np.ptp(cb[:, 0]))

        seeds = [[0.0, 0.3, 0.9, 0.4, 0.0], [0.0, 0.7, 0.4, 0.5, 0.0], [-0.3, 0.5, 0.6, 0.5, 0.0]]
        pl2 = self.place_plan(spot_b, counter_top - 0.01, fit, seeds)
        if pl2 is None:
            self.ev("place_ik_failed", item=name, spot=r2(spot_b))
            spot_b = np.array([spot_b[0], spot_b[1] + 0.08])
            pl2 = self.place_plan(spot_b, counter_top - 0.01, fit, seeds)
        if pl2 is None:
            pl2 = self.place_plan(np.array([self.pan_x, -0.5]), counter_top - 0.01, fit, seeds)
        q_pl, R2, z_tcp, cb2 = pl2
        p_up = np.array([spot_b[0], spot_b[1], z_tcp + 0.10])
        q_up, _ = self.kin.solve(p_up, R=R2, w_rot=0.25, seeds=[q_pl])
        q_side = np.array(qs[-1], float)
        q_side[0] = q_up[0]
        self.plan_move(self.arm_dict(q_side), vscale=0.85)
        q_up = self.move_cart(q_up, n=10, bulge=0.03)
        self.wait_arm()
        p_up = self.kin.fk(q_up)[:3, 3]
        dn, _ = self.line(q_up, p_up, np.array([spot_b[0], spot_b[1], z_tcp]), R2, n=6)
        self.plan_path(dn, T=0.8)
        self.wait_arm()
        self.release(path)
        self.plan_move({"gripper": min(self.qm["gripper"] + 0.35, 1.5)}, T=0.4)
        self.wait_arm()
        self.wait(0.5)
        self.on_counter.append(path)
        self.ev("on_counter", item=name, n=len(self.on_counter), at=self.center_w(path))
        self.plan_path(dn[::-1], T=0.6)
        self.wait_arm()
        return True

    def checkout(self, slot, counter_top, names):
        self.phase, self.sub, self.target = "to_checkout", "heading to checkout", None
        x, y, yaw = slot
        ok = self.drive_to((x, y), yaw, label="checkout")
        if not ok:
            self.ev("checkout_unreachable")
            return False
        self.wait_arm()
        self.phase, self.sub = "checkout", "unloading onto the counter"
        self.ev("at_checkout", items=len(self.cargo))
        if self.director is not None:
            fwd = np.array([math.cos(yaw), math.sin(yaw)])
            right = np.array([math.sin(yaw), -math.cos(yaw)])
            work = np.r_[np.array([x, y]) + fwd * (self.pan_x + 0.3) + right * 0.55, counter_top + 0.1]
            self.director.frame_work(work, (x, y), yaw)
        cfgp = self.cfg.get("place", {})
        xs = cfgp.get("xs", [0.25, 0.15, 0.05, -0.05, -0.15, -0.25])
        lateral = cfgp.get("lateral", 0.58)
        shift = cfgp.get("shift", 0.65)
        n = 0
        tries = 0
        while self.cargo and tries < 40:
            tries += 1
            if n and n % len(xs) == 0 and self.cargo:
                self.phase, self.sub = "checkout", "moving along the counter"
                self.plan_move(self.travel_pose(), vscale=0.85)
                self.wait_arm()
                cx, cy, cyaw = self.cmd_q["base_x"], self.cmd_q["base_y"], self.cmd_q["base_yaw"]
                self.drive_to(
                    (cx + math.cos(cyaw) * shift, cy + math.sin(cyaw) * shift), cyaw, vmax=0.3, label="counter"
                )
                if self.director is not None:
                    q = self.qm
                    fwd = np.array([math.cos(cyaw), math.sin(cyaw)])
                    right = np.array([math.sin(cyaw), -math.cos(cyaw)])
                    work = np.r_[
                        np.array([q["base_x"], q["base_y"]]) + fwd * (self.pan_x + 0.3) + right * 0.55,
                        counter_top + 0.1,
                    ]
                    self.director.frame_work(work, (q["base_x"], q["base_y"]), cyaw)
            top = max(self.cargo, key=lambda p: float(self.center_w(p)[2]))
            spot = np.array([self.pan_x + xs[n % len(xs)], -lateral])
            nm = names.get(top, top.rsplit("/", 2)[-2])
            if self.unload_one(top, spot, counter_top, nm):
                n += 1
            elif top in self.cargo:  # could not take it out: leave it in the basket
                self.cargo.remove(top)
        self.plan_move({**self.travel_pose(), "gripper": 0.0}, vscale=0.85)
        self.wait_arm()
        self.phase, self.sub = "paid", ""
        self.ev("paid", items=n, left_in_basket=len(self.cargo))
        return True


# --- the trip ---------------------------------------------------------------------------------------------------------
def trip(ms: Mission, items: dict, plan_stops: list, planner, ctx: dict):
    """execute Gemini's stops; failed stops go back to Gemini"""
    ms.queue = list(plan_stops)
    failures = {}
    while ms.queue:
        stop = ms.queue.pop(0)
        iid = stop["item"]
        it = items.get(iid)
        if it is None or iid in ms.done:
            continue
        ms.target = iid
        ms.phase, ms.sub = "drive", it["name"]
        ms.ev(
            "leg",
            item=iid,
            name=it["name"],
            section=it["section"],
            section_label=it["section_label"],
            reason=stop.get("reason", ""),
        )
        ok = ms.drive_to(it["xy"], it["yaw"], label=iid)
        why = "has no collision-free path to its aisle"
        if ok:
            ms.wait_arm()
            ms.phase = "reach"
            if ms.director is not None:
                ms.director.frame_work(it["center"], it["xy"], it["yaw"])
            ok, why = ms.pick_shelf(it)
            if not ok and why == "slipped out of the jaws" and failures.get(iid, 0) == 0:
                failures[iid] = 1
                ms.ev("retry", item=iid)
                ok, why = ms.pick_shelf(it)
        if not ok:
            ms.ev("pick_failed", item=iid, name=it["name"], why=why)
            ms.plan_move({**ms.travel_pose(), "gripper": 0.0}, vscale=0.85)
            ms.wait_arm()
            replan(ms, items, stop, f"{it['name']} {why}.", planner, ctx)
            continue
        ms.to_basket(it)
        ms.done.append(iid)
        ms.ev("item_done", item=iid, name=it["name"], n=len(ms.done))
        if ms.director is not None:
            ms.director.mode = "chase"
    return True


def replan(ms: Mission, items, stop, event, planner, ctx):
    ms.stats["replans"] += 1
    ms.phase, ms.sub = "replan", "asking Gemini to replan"
    failed = items[stop["item"]]
    remaining = [stop] + ms.queue
    rem = []
    for st in remaining:
        it = items[st["item"]]
        e = {"id": st["item"], "name": it["name"], "section": it["section"], "pos": r2(it["center"][:2], 2)}
        if st["item"] == stop["item"]:
            e["failed"] = event
            e["alternatives"] = []
            for k, alt in enumerate(ctx["alternatives"](failed)[:3]):
                aid = f"{stop['item']}-alt{k + 1}"
                items[aid] = alt
                e["alternatives"].append(
                    {"id": aid, "section": alt["section"], "pos": r2(alt["center"][:2], 2), "side": alt["side"]}
                )
        rem.append(e)
    ms.ev("replan_request", event=event)
    ms.wait(0.4)
    t0 = time.time()
    try:
        q = ms.qm
        out = planner.replan(
            ctx["map_summary"],
            {"name": NAME, "pose": r2([q["base_x"], q["base_y"]], 2)},
            rem,
            event,
            ctx["checkout_info"],
        )
        stops = (out.get("robots") or [{}])[0].get("stops", [])
        valid = [s for s in stops if s.get("item") in items and s["item"] not in ms.done]
        ms.queue = valid
        ms.ev(
            "replan",
            summary=out.get("summary", ""),
            stops=valid,
            latency_s=round(time.time() - t0, 1),
            model=planner.model_used,
        )
    except Exception as e:  # noqa: BLE001
        ms.ev("replan_failed", error=str(e)[:200])
    ms.wait(0.4)


def make_plan(planner, smap, items, start, ctx, log=print):
    sec_label = {s["id"]: f"{s['label']} ({s['where']})" for s in smap.sections}
    for it in items.values():
        it["section_label"] = sec_label.get(it["section"], "")
    items_info = [
        {
            "id": i,
            "name": it["name"],
            "section": it["section"],
            "section_label": it["section_label"],
            "pos": r2(it["center"][:2], 2),
            "shelf_height_m": round(float(it["center"][2]), 2),
            "side": it["side"],
            "rover_stands_at": r2(it["xy"], 2),
        }
        for i, it in items.items()
    ]
    robots_info = [{"name": NAME, "start": r2(start[:2], 2), "facing_deg": round(math.degrees(start[2]))}]
    t0 = time.time()
    try:
        resp = planner.plan(ctx["map_summary"], robots_info, items_info, ctx["checkout_info"])
        stops, seen = [], set()
        rb = next((r for r in resp.get("robots", []) if r.get("robot") == NAME), (resp.get("robots") or [{}])[0])
        for st in rb.get("stops", []):
            if st.get("item") in items and st["item"] not in seen:
                seen.add(st["item"])
                stops.append(st)
        missing = [i for i in items if i not in seen]
        for i in missing:
            stops.append({"item": i, "reason": "added back (the plan skipped it)"})
        src = "gemini" + (f" (repaired {len(missing)} missing)" if missing else "")
        return {
            "source": src,
            "model": planner.model_used,
            "latency_s": round(time.time() - t0, 1),
            "summary": resp.get("summary", ""),
            "stops": stops,
            "checkout_reason": rb.get("checkout_reason", ""),
        }
    except Exception as e:  # noqa: BLE001
        log(f"[so101-demo] Gemini plan failed: {e}; greedy fallback")
        left = list(items)
        pos = np.array(start[:2])
        stops = []
        while left:
            b = min(left, key=lambda i: np.linalg.norm(items[i]["xy"] - pos))
            left.remove(b)
            stops.append({"item": b, "reason": "nearest remaining item (LLM unavailable)"})
            pos = items[b]["xy"]
        return {
            "source": "greedy-fallback",
            "model": None,
            "latency_s": round(time.time() - t0, 1),
            "summary": "nearest-item order (LLM unavailable)",
            "stops": stops,
            "checkout_reason": "",
        }


def build_items(cat: Catalog, chosen):
    items = {}
    for k, (p, g) in enumerate(chosen):
        iid = f"I{k + 1}"
        items[iid] = {
            "id": iid,
            "path": p.path,
            "product": p.name,
            "name": item_name(p),
            "sku": p.sku,
            "section": p.section,
            "side": g["side"],
            "xy": np.asarray(g["xy"], float),
            "yaw": float(g["yaw"]),
            "center": np.asarray(p.center, float),
            "standoff": g["standoff"],
            "pitch": g["pitch"],
            "width": g["width"],
            "size": p.hi - p.lo,
            "roll": g["roll"],
            "flip": g["flip"],
            "gap_fixed": g["gap_fixed"],
            "gap_moving": g["gap_moving"],
        }
    return items


def alternatives_fn(cat: Catalog, used: set):
    def alts(it):
        out = []
        for p in cat.smap.products:
            if p.sku != it["sku"] or p.path in used:
                continue
            g = cat.screen(p)
            if g is None:
                continue
            out.append(
                {
                    "id": "",
                    "path": p.path,
                    "product": p.name,
                    "name": it["name"],
                    "sku": p.sku,
                    "section": p.section,
                    "side": g["side"],
                    "xy": np.asarray(g["xy"], float),
                    "yaw": float(g["yaw"]),
                    "center": np.asarray(p.center, float),
                    "standoff": g["standoff"],
                    "pitch": g["pitch"],
                    "width": g["width"],
                    "size": p.hi - p.lo,
                    "section_label": it.get("section_label", ""),
                    "roll": g["roll"],
                    "flip": g["flip"],
                }
            )
        out.sort(key=lambda a: float(np.linalg.norm(a["center"] - it["center"])))
        return out

    return alts


def plan_doc(plan, items, smap, meta, slot, ck, spawn):
    return {
        "source": plan["source"],
        "model": plan["model"],
        "latency_s": plan["latency_s"],
        "summary": plan["summary"],
        "robots": {NAME: plan["stops"]},
        "checkout_reason": {NAME: plan.get("checkout_reason", "")},
        "items": {
            i: {
                "name": it["name"],
                "product": it["product"],
                "section": it["section"],
                "section_label": it.get("section_label", ""),
                "center": r2(it["center"], 2),
                "stand": r2(it["xy"], 2),
                "width_cm": round(it["width"] * 100, 1),
            }
            for i, it in items.items()
        },
        "sections": [{**s, "lo": r2(s["lo"]), "hi": r2(s["hi"])} for s in smap.sections],
        "map": smap.to_json(),
        "topdown": meta,
        "slots": {NAME: list(slot)},
        "checkout": {"lo": ck["lo"], "hi": ck["hi"]},
        "spawn": list(spawn),
        "robot": "Picky SO-101 rover v3: PAL PMB2 base + long-reach SO-101 + round wire basket",
    }


def orbit_start(hg: HeightGrid, c, radius, z, arc) -> float:
    """start angle of an orbit arc (radius, height z, length arc) around c whose eye stays in free air with c in view"""
    angs = np.linspace(0, 2 * math.pi, 72, endpoint=False)

    def ok(a):
        eye = np.array([c[0] + radius * math.cos(a), c[1] + radius * math.sin(a), z])
        return hg.eye_ok(eye) and hg.visible(eye, c)

    good = np.array([ok(a) for a in angs])
    n = int(math.ceil(arc / (2 * math.pi / 72))) + 1
    best, best_k = -1, 0
    for k in range(72):
        run = sum(good[(k + j) % 72] for j in range(n))
        if run > best:
            best, best_k = run, k
    return float(angs[best_k])


def product_closeups(app, stage, items, out: Path, hide=(), look_at=None):
    """one 448 px close-up per shopping-list item from the aisle (for Gemini to name it + the plan card)"""
    import omni.replicator.core as rep  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    from pxr import UsdGeom  # noqa: PLC0415

    (out / "items").mkdir(parents=True, exist_ok=True)
    vis = []
    for hp in hide:
        im = UsdGeom.Imageable(stage.GetPrimAtPath(hp))
        vis.append(im)
        im.MakeInvisible()
    cam = "/World/picky_itemcam"
    it0 = next(iter(items.values()))
    look_at(stage, cam, it0["center"] + np.array([0, 0, 0.5]), it0["center"], focal=40.0)
    rp = rep.create.render_product(cam, (448, 448))
    ann = rep.AnnotatorRegistry.get_annotator("rgb")
    ann.attach([rp])
    shots = []
    import carb.settings  # noqa: PLC0415

    cs = carb.settings.get_settings()
    cop = cs.get("/omni/replicator/captureOnPlay")
    cs.set("/omni/replicator/captureOnPlay", False)  # rendered before the timeline plays
    for iid, it in items.items():
        n = np.array([*SIDE_N[it["side"]], 0.0])
        eye = it["center"] + n * 0.42 + np.array([0, 0, 0.06])
        look_at(stage, cam, eye, it["center"], focal=40.0)
        for _ in range(12):
            app.update()
        d = ann.get_data()
        if d is None or getattr(d, "size", 0) == 0:
            continue
        f = out / "items" / f"{iid}.jpg"
        Image.fromarray(np.asarray(d)[..., :3].copy()).save(f, quality=92)
        shots.append((iid, f.read_bytes()))
    if cop is not None:
        cs.set("/omni/replicator/captureOnPlay", cop)
    ann.detach()
    rp.destroy()
    for im in vis:
        im.MakeVisible()
    return shots


# --- Isaac Sim entry (teleop_app.py --demo) ---------------------------------------------------------------------------
def run(app, args, tele):
    """tele = the teleop_app module (scene helpers)."""
    import omni.usd  # noqa: PLC0415
    from pxr import Gf, UsdGeom, UsdLux, UsdPhysics  # noqa: PLC0415

    from demo import autopilot  # noqa: PLC0415
    from demo.planner import Planner  # noqa: PLC0415
    from demo.store_map import StoreMap, product_entities  # noqa: PLC0415

    def log(m):
        print(m, flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        import carb.settings  # noqa: PLC0415

        # DLSS (the default AA) crashed the renderer (NGX CreateFeature failed) once six render products were live
        carb.settings.get_settings().set("/rtx/post/aa/op", 2)  # 2 = FXAA
    except Exception as e:  # noqa: BLE001
        log(f"[so101-demo] could not switch AA: {e}")
    for f in ("events.jsonl", "frames.jsonl", "llm_log.jsonl"):
        (out / f).unlink(missing_ok=True)
    cfg = json.loads(Path(args.so101_config).read_text())
    kin = RoverKinematics(Path(args.robot).with_suffix(".kin.json"))
    t_setup = time.time()
    stage = tele.open_scene(args.scene)
    if stage is None:
        raise SystemExit(f"could not open {args.scene}")
    tele.ensure_physics_and_light(stage)
    for p in stage.Traverse():  # 60 Hz physics: one app.update() = one 1/60 s step = one trajectory sample
        if p.IsA(UsdPhysics.Scene):
            try:
                from pxr import PhysxSchema  # noqa: PLC0415

                PhysxSchema.PhysxSceneAPI.Apply(p).CreateTimeStepsPerSecondAttr().Set(60)
            except Exception as e:  # noqa: BLE001
                log(f"[so101-demo] could not set the physics rate: {e}")
    # exposure: the Genie Sim lights blow the white floor/shelves out a little; scale every light
    n_l = 0
    for p in stage.Traverse():
        if p.HasAPI(UsdLux.LightAPI) or any(
            p.IsA(t)
            for t in (UsdLux.DomeLight, UsdLux.DistantLight, UsdLux.RectLight, UsdLux.SphereLight, UsdLux.DiskLight)
        ):
            a = p.GetAttribute("inputs:intensity")
            if a and a.Get() is not None:
                a.Set(float(a.Get()) * args.light_scale)
                n_l += 1
    log(f"[so101-demo] scaled {n_l} lights by {args.light_scale}")
    spawn = (cfg["spawn"][0], cfg["spawn"][1], math.radians(cfg["spawn"][2]))
    stage.DefinePrim(ROBOT, "Xform").GetReferences().AddReference(str(Path(args.robot).resolve()))
    tele.add_ground(stage)
    extra = autopilot.add_checkout(stage, cfg["checkout"])
    smap = StoreMap(stage, extra_fixtures=extra)
    log(
        f"[so101-demo] store map: {len(smap.products)} products, {len(smap.fixtures)} fixtures, "
        f"{len(smap.sections)} sections"
    )
    # products: kinematic rigid bodies (stay on the shelves) with convex-hull colliders, dynamic once grasped
    ents = product_entities(stage)
    for ent in ents:
        tele.make_dynamic(ent)
        ent.GetAttribute("physics:kinematicEnabled").Set(True)
    log(f"[so101-demo] {len(ents)} products set up")
    # dump the boxes so the map + item choice can be rerun offline
    from demo.store_map import scene_boxes  # noqa: PLC0415

    pb, fb, bb = scene_boxes(stage)
    (out / "scene_boxes.json").write_text(
        json.dumps(
            {
                "products": [[p, n, r2(lo), r2(hi)] for p, n, lo, hi in pb],
                "fixtures": [[p, k, r2(lo), r2(hi)] for p, k, lo, hi in fb],
                "building": [r2(bb[0]), r2(bb[1])] if bb else None,
                "extra": [[e[0], e[1], e[2], e[3]] for e in extra],
            }
        )
    )

    meta = autopilot.topdown(app, stage, smap, out, hide=[ROBOT])
    smap.restrict_sides(cfg.get("open_sides", {}))
    heights = np.load(out / "heights.npy").astype(np.float32)
    hg = HeightGrid(smap, heights, meta)

    # floor height at the spawn + world anchor for the planar base joints (base_x/base_y/base_yaw = world x/y/yaw)
    wf = stage.GetPrimAtPath(f"{ROBOT}/joints/world_fix")
    wf.GetAttribute("physics:localPos0").Set(Gf.Vec3f(0.0, 0.0, float(cfg.get("floor_z", 0.0))))
    wf.GetAttribute("physics:localRot0").Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))

    cat = Catalog(smap, kin)
    chosen = choose_items(cat, args.max_items, np.array(spawn[:2]), fixed=cfg.get("items", []))
    items = build_items(cat, chosen)
    log(f"[so101-demo] shopping list: {[(i, it['name'], it['section']) for i, it in items.items()]}")
    ck = cfg["checkout"]
    counter_top = float(ck["hi"][2]) + 0.02
    slot = tuple(cfg["checkout_slot"][:2]) + (math.radians(cfg["checkout_slot"][2]),)
    ctx = {
        "map_summary": {**smap.summary(), "ascii_map": smap.ascii()},
        "checkout_info": {
            "counter_min": r2(ck["lo"][:2], 2),
            "counter_max": r2(ck["hi"][:2], 2),
            "rover_parks_at": r2(slot[:2], 2),
        },
        "alternatives": alternatives_fn(cat, {it["path"] for it in items.values()}),
    }
    planner = Planner(str(out / "llm_log.jsonl"), system=SYSTEM)
    # name the products from close-up renders (Gemini vision); category names if that fails
    try:
        shots = product_closeups(app, stage, items, out, hide=[ROBOT], look_at=autopilot.look_at)
        names = planner.name_products(shots) if shots else {}
        seen = {}
        for iid, nm in names.items():
            if iid in items and isinstance(nm, str) and 2 <= len(nm) <= 40:
                nm = nm.strip().rstrip(".")
                seen[nm] = seen.get(nm, 0) + 1
                items[iid]["name"] = nm if seen[nm] == 1 else f"{nm} #{seen[nm]}"
        log(f"[so101-demo] product names: {[(i, it['name']) for i, it in items.items()]}")
    except Exception as e:  # noqa: BLE001
        log(f"[so101-demo] product naming skipped: {e}")
    plan = make_plan(planner, smap, items, spawn, ctx, log)
    (out / "plan.json").write_text(json.dumps(plan_doc(plan, items, smap, meta, slot, ck, spawn), indent=1))
    log(
        f"[so101-demo] setup {time.time() - t_setup:.0f}s; plan {plan['source']} {plan['model']} "
        f"{plan['latency_s']}s: {plan['summary']}"
    )
    if args.plan_only:
        return

    # cameras
    def set_cam_factory(path):
        def set_cam(eye, tgt, focal):
            autopilot.look_at(stage, path, eye, tgt, focal=focal)
            c = UsdGeom.Camera(stage.GetPrimAtPath(path))
            c.GetFocalLengthAttr().Set(float(focal))

        return set_cam

    set_cam = set_cam_factory("/World/picky_director")
    autopilot.look_at(stage, "/World/picky_director", [spawn[0] - 2, spawn[1], 1.8], [spawn[0], spawn[1], 0.5], 16.0)
    cams = {"director": ("/World/picky_director", tuple(args.res))}
    for nm, res in (("wrist", (640, 360)), ("front", (640, 360)), ("basket", (480, 270))):
        cams[nm] = (ROBOT + kin.k["cameras"][nm][len("/Picky") :], res)
    # bird's-eye + side follow cameras (960x540 keeps the render cost low)
    bird_path, side_path = "/World/picky_birdseye", "/World/picky_side"
    autopilot.look_at(stage, bird_path, [spawn[0] - 0.6, spawn[1], 2.45], [spawn[0], spawn[1], 0.35], 9.0)
    autopilot.look_at(stage, side_path, [spawn[0], spawn[1] + 1.5, 1.15], [spawn[0], spawn[1], 0.7], 11.0)
    cams["birdseye"] = (bird_path, (960, 540))
    cams["side"] = (side_path, (960, 540))
    keep = {c.strip() for c in str(getattr(args, "rec_cams", "") or "").split(",") if c.strip()}
    if keep:  # fewer render products = less GPU memory + faster renders on a shared GPU
        cams = {k: v for k, v in cams.items() if k in keep or k == "director"}
    log(f"[so101-demo] recording cameras: {sorted(cams)}")
    try:
        from omni.kit.viewport.utility import get_active_viewport  # noqa: PLC0415

        get_active_viewport().updates_enabled = False
    except Exception:  # noqa: BLE001
        pass
    rec = Recorder(out / "frames", cams) if args.record else None

    import omni.timeline  # noqa: PLC0415

    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    for _ in range(4):
        app.update()
    io = IsaacIO(app, stage, kin, spawn)
    tl0 = timeline.get_current_time()
    for _ in range(30):
        io.apply(io.q0, {n: 0.0 for n in CTRL}, [0.0, 0.0])
        app.update()
    log(
        f"[so101-demo] 30 updates advanced the timeline by {timeline.get_current_time() - tl0:.4f} s (expect 0.5); "
        f"effort api {io.eff_fn}"
    )
    ms = Mission(io, kin, smap, cfg, out, hg, set_cam=set_cam, rec=rec, log=log)
    ms.cap_every = max(1, int(getattr(args, "capture_every", 2)))
    follow = FollowCams(hg, set_cam_factory(bird_path), set_cam_factory(side_path))
    ms.cam_hooks.append(follow.update)
    names = {it["path"]: it["name"] for it in items.values()}
    ms.ev("plan", source=plan["source"], model=plan["model"], summary=plan["summary"], stops=plan["stops"])
    t_run = time.time()
    try:
        # intro: slow orbit round the rover
        ms.gphase = "intro"
        if rec:
            rec.set_active(["director"])
        d = ms.director
        d.mode = "orbit"
        n_intro = int(args.intro / DT)
        c_in = np.array([spawn[0], spawn[1], 0.55])
        a0 = orbit_start(hg, c_in, 2.1, 1.75, 0.9)
        for i in range(n_intro):
            u = minjerk(i / max(n_intro - 1, 1))
            a = a0 + 0.9 * u
            eye = np.array([c_in[0] + 2.1 * math.cos(a), c_in[1] + 2.1 * math.sin(a), 1.75 - 0.2 * u])
            d.eye, d.tgt = eye, c_in
            ms.step()
        d.mode = "chase"
        if rec:
            rec.set_active(list(cams))
        ms.gphase = "main"
        ms.ev("start")
        trip(ms, items, plan["stops"], planner, ctx)
        ms.checkout(slot, counter_top, names)
        ms.gphase = "outro"
        if rec:
            rec.set_active([c for c in ("director", "basket") if c in cams])
        q = ms.qm
        d.mode = "orbit"
        n_out = int(args.outro / DT)
        c_out = np.array([q["base_x"], q["base_y"], 0.6])
        a0 = orbit_start(hg, c_out, 2.0, 1.9, 0.8)
        for i in range(n_out):
            u = minjerk(i / max(n_out - 1, 1))
            a = a0 + 0.8 * u
            d.eye = np.array([c_out[0] + 2.0 * math.cos(a), c_out[1] + 2.0 * math.sin(a), 1.9 + 0.2 * u])
            d.tgt = c_out
            ms.step()
    finally:
        stats = {
            **ms.stats,
            "sim_s": round(ms.t, 1),
            "wall_s": round(time.time() - t_run, 1),
            "frames": rec.frame if rec else 0,
            "effort_api": io.eff_fn,
            "follow_cam_cuts": follow.cuts,
            "effort_max_Nm": {k: round(v, 3) for k, v in ms.eff_max.items()},
            "llm_calls": sum('"response"' in ln for ln in open(out / "llm_log.jsonl"))
            if (out / "llm_log.jsonl").exists()
            else 0,
        }
        ms.ev("done", **stats)
        (out / "stats.json").write_text(json.dumps(stats, indent=1))
        if rec:
            rec.close()


# --- offline dry run (no Isaac): perfect tracking, products as their boxes --------------------------------------------
def offline_main():
    import argparse  # noqa: PLC0415

    sys.path.insert(0, str(HERE.parent))
    from demo.store_map import StoreMap  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--boxes", required=True, help="scene_boxes.json from an Isaac run (or a converted dump)")
    ap.add_argument("--heights", default=None)
    ap.add_argument("--topdown-meta", default=None, help="plan.json holding a 'topdown' block")
    ap.add_argument("--kin", required=True)
    ap.add_argument("--config", default=str(HERE / "so101_config.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-items", type=int, default=12)
    ap.add_argument("--gemini", action="store_true")
    ap.add_argument("--select-only", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("events.jsonl", "frames.jsonl"):
        (out / f).unlink(missing_ok=True)
    cfg = json.loads(Path(a.config).read_text())
    kin = RoverKinematics(a.kin)
    d = json.loads(Path(a.boxes).read_text())
    boxes = (
        [(p, n, np.array(lo), np.array(hi)) for p, n, lo, hi in d["products"]],
        [(p, k, np.array(lo), np.array(hi)) for p, k, lo, hi in d["fixtures"]],
        (np.array(d["building"][0]), np.array(d["building"][1])) if d.get("building") else None,
    )
    ck = cfg["checkout"]
    extra = [("/World/picky_checkout/counter", ck["lo"], ck["hi"], "checkout")]
    smap = StoreMap(None, extra_fixtures=extra, boxes=boxes)
    heights = meta = None
    if a.heights and a.topdown_meta:
        heights = np.load(a.heights).astype(np.float32)
        meta = json.loads(Path(a.topdown_meta).read_text())["topdown"]
        smap.add_heightmap(heights, meta["x0"], meta["y1"], meta["px"])
    smap.restrict_sides(cfg.get("open_sides", {}))
    hg = HeightGrid(smap, heights, meta)
    spawn = (cfg["spawn"][0], cfg["spawn"][1], math.radians(cfg["spawn"][2]))
    print("spawn free:", smap.free(spawn[:2]), "slot free:", smap.free(cfg["checkout_slot"][:2]))
    cat = Catalog(smap, kin)
    t0 = time.time()
    chosen = choose_items(cat, a.max_items, np.array(spawn[:2]), fixed=cfg.get("items", []))
    print(f"chose {len(chosen)} items in {time.time() - t0:.1f}s")
    items = build_items(cat, chosen)
    for i, it in items.items():
        g = chosen[int(i[1:]) - 1][1]
        print(
            i,
            it["product"],
            it["name"],
            it["section"],
            it["side"],
            "h",
            round(it["center"][2], 2),
            "w",
            round(it["width"] * 100, 1),
            "standoff",
            it["standoff"],
            "pitch",
            it["pitch"],
            "gaps f/m",
            round(g["gap_fixed"], 3),
            round(g["gap_moving"], 3),
            "flip",
            g["flip"],
            "above",
            round(g["above"], 3),
            "dl",
            round(g["depth_len"], 3),
        )
    if a.select_only:
        return
    slot = tuple(cfg["checkout_slot"][:2]) + (math.radians(cfg["checkout_slot"][2]),)
    ctx = {
        "map_summary": {**smap.summary(), "ascii_map": smap.ascii()},
        "checkout_info": {
            "counter_min": r2(ck["lo"][:2], 2),
            "counter_max": r2(ck["hi"][:2], 2),
            "rover_parks_at": r2(slot[:2], 2),
        },
        "alternatives": alternatives_fn(cat, {it["path"] for it in items.values()}),
    }
    if a.gemini:
        from demo.planner import Planner  # noqa: PLC0415

        planner = Planner(str(out / "llm_log.jsonl"), system=SYSTEM)
    else:

        class _P:
            model_used = None

            def plan(self, *x):
                raise RuntimeError("offline")

            def replan(self, *x):
                raise RuntimeError("offline")

        planner = _P()
    plan = make_plan(planner, smap, items, spawn, ctx)
    (out / "plan.json").write_text(
        json.dumps(plan_doc(plan, items, smap, meta, slot, ck, spawn), indent=1, default=str)
    )
    counter_top = float(ck["hi"][2]) + 0.02
    io = FakeIO(kin, spawn, smap, counter_top)
    ms = Mission(io, kin, smap, cfg, out, hg, set_cam=lambda *x: None)
    t1 = time.time()
    trip(ms, items, plan["stops"], planner, ctx)
    ms.checkout(slot, counter_top, {it["path"]: it["name"] for it in items.values()})
    print(
        f"offline run: sim {ms.t:.1f}s in {time.time() - t1:.1f}s wall; stats {ms.stats}; done {ms.done}; "
        f"on counter {len(ms.on_counter)}"
    )


if __name__ == "__main__":
    offline_main()
