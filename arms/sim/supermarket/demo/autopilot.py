"""--demo autopilot for teleop_app.py: an LLM-planned shopping run with the same robot, presets and grab.

Flow per rover (a generator advanced once per physics step, so several rovers run at the same time):
  Gemini plan (which rover takes which item, in what order, why) -> for each stop: A* path over the store map ->
  drive (swerve base, the same command path as keyboard driving) -> face the shelf -> arm to a pre-grasp in front of
  the product (IK table) -> slide in -> measure the gripper/product error in sim and correct (arm + base strafe) ->
  close + attach the product that is actually at the gripper (no snapping) -> lift, retract -> basket pose ->
  release (gravity drops it into the basket) -> home. A failed stop (no path, product not at the gripper) goes back to
  Gemini for a replan. Then checkout: drive to the counter, pick each item out of the basket, put it on the counter.

Everything the video needs (poses, phases, captions, LLM text) is logged per frame to frames.jsonl + events.jsonl.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade

DT = 1 / 60
PRE_BACK, PRE_UP = 0.11, 0.03  # pre-grasp: this far back from / above the product, then slide straight in
HOME = [0.0, -0.431, 0.853, 1.571, -1.859, 0.251]
BASKET = [-0.48, -0.69, 3.16, -1.571, -1.859, -1.4]
# basket interior in the base frame (from build_rm2_basket.py: centre (-0.13, 0.04), 34 x 30 cm, floor z 0.863)
BASKET_LO, BASKET_HI = np.array([-0.30, -0.11, 0.80]), np.array([0.04, 0.19, 1.15])


def minjerk(s: float) -> float:
    return s * s * s * (10 - 15 * s + 6 * s * s)


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class ArmIK:
    """Interpolates the precomputed IK tables (robot/solve_arm_ik.py)."""

    def __init__(self, path: str):
        d = json.loads(Path(path).read_text())
        self.arm_y = d["arm_y"]
        f = d["front"]
        self.fx, self.fz, self.fq = np.array(f["x"]), np.array(f["z"]), f["q"]
        b = d["down"]
        self.bx, self.by, self.bz, self.bq = np.array(b["x"]), np.array(b["y"]), np.array(b["z"]), b["q"]

    @staticmethod
    def _cell(axis, v):
        i = int(np.clip(np.searchsorted(axis, v) - 1, 0, len(axis) - 2))
        return i, float(np.clip((v - axis[i]) / (axis[i + 1] - axis[i]), 0, 1))

    def _interp(self, table, axes, vals, fixed=None):
        cells = [self._cell(a, v) for a, v in zip(axes, vals, strict=False)]
        acc, wsum, got = np.zeros(6), 0.0, []
        for corner in range(1 << len(axes)):
            idx, w = [], 1.0
            for k, (i, t) in enumerate(cells):
                bit = (corner >> k) & 1
                idx.append(axes[k][i + bit])
                w *= t if bit else 1 - t
            key = ",".join(f"{v:.3f}" for v in (idx if fixed is None else fixed(idx)))
            q = table.get(key)
            if q is not None:
                got.append((w, np.array(q)))
        if not got:
            return None
        ref = max(got, key=lambda g: g[0])[1]
        for w, q in got:
            if np.abs(q - ref).max() < 0.6:  # same IK branch only
                acc += w * q
                wsum += w
        return list(acc / wsum) if wsum > 1e-6 else list(ref)

    def front(self, x: float, z: float):
        """Gripper horizontal, pointing +x, tool point at (x, arm_y, z) in the base frame."""
        if not (self.fx[0] <= x <= self.fx[-1] and self.fz[0] <= z <= self.fz[-1]):
            return None
        return self._interp(self.fq, [self.fx, self.fz], [x, z], fixed=lambda i: [i[0], self.arm_y, i[1]])

    def down(self, x: float, y: float, z: float):
        """Tool point at (x, y, z) over the basket (gripper as close to pointing down as the arm allows)."""
        x, y, z = (float(np.clip(v, a[0], a[-1])) for v, a in ((x, self.bx), (y, self.by), (z, self.bz)))
        return self._interp(self.bq, [self.bx, self.by, self.bz], [x, y, z])


# --- per-frame log / recorder ---------------------------------------------------------------------------------------
class Recorder:
    """Replicator render products + rgb annotators; JPEGs written by a thread pool. Cameras can be switched off
    (no render) when the edit won't use them."""

    def __init__(self, out: Path, cams: dict[str, tuple[str, tuple[int, int]]]):
        import omni.replicator.core as rep
        from PIL import Image  # noqa: F401

        self.out = out
        self.pool = ThreadPoolExecutor(6)
        self.rps, self.ann = {}, {}
        for name, (path, res) in cams.items():
            (out / name).mkdir(parents=True, exist_ok=True)
            rp = rep.create.render_product(path, res)
            an = rep.AnnotatorRegistry.get_annotator("rgb")
            an.attach([rp])
            self.rps[name], self.ann[name] = rp, an
        self.active = set(cams)
        self.frame = 0
        self.pending = []

    def set_active(self, names) -> None:
        self.active = set(names)
        self.render(True)

    def render(self, on: bool) -> None:
        """Render products only update on frames that get captured (every 2nd physics step)."""
        for n, rp in self.rps.items():
            try:
                rp.hydra_texture.set_updates_enabled(on and n in self.active)
            except Exception:  # noqa: BLE001
                pass

    def capture(self) -> None:
        from PIL import Image

        for n in self.active:
            data = self.ann[n].get_data()
            if data is None or getattr(data, "size", 0) == 0:
                continue
            arr = np.asarray(data)[..., :3].copy()
            path = self.out / n / f"{self.frame:06d}.jpg"
            self.pending.append(self.pool.submit(lambda a=arr, p=path: Image.fromarray(a).save(p, quality=90)))
        self.pending = [f for f in self.pending if not f.done()]
        self.frame += 1

    def close(self):
        self.pool.shutdown(wait=True)


# --- cameras -------------------------------------------------------------------------------------------------------
def look_at(stage, path: str, eye, target, focal: float = 18.0):
    cam = UsdGeom.Camera(stage.GetPrimAtPath(path))
    if not cam:
        cam = UsdGeom.Camera.Define(stage, path)
        cam.CreateFocalLengthAttr(focal)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.03, 200.0))
        UsdGeom.Xformable(cam).AddTransformOp()
    eye, target = np.array(eye, float), np.array(target, float)
    z = eye - target
    z /= np.linalg.norm(z)
    x = np.cross([0.0, 0.0, 1.0], z)
    x /= np.linalg.norm(x) + 1e-9
    y = np.cross(z, x)
    m = Gf.Matrix4d(*x, 0, *y, 0, *z, 0, *eye, 1)
    UsdGeom.Xformable(cam).GetOrderedXformOps()[0].Set(m)
    return path


def add_wrist_camera(stage, link6_path: str) -> str:
    """Camera on the gripper: behind the fingers looking along the approach axis (link6 +z)."""
    path = link6_path + "/wrist_cam"
    cam = UsdGeom.Camera.Define(stage, path)
    cam.CreateFocalLengthAttr(10.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.02, 50.0))
    # camera axes in link6: X = -y, Y (up) = -x, looks along -Z = +z (approach); sits 7.5 cm "above" the fingers
    m = Gf.Matrix4d(0, -1, 0, 0, -1, 0, 0, 0, 0, 0, -1, 0, -0.075, 0.0, -0.13, 1)
    xf = UsdGeom.Xformable(cam)
    xf.ClearXformOpOrder()
    xf.AddTransformOp().Set(m)
    return path


def add_box(stage, path, lo, hi, color, collide=True):
    lo, hi = np.array(lo, float), np.array(hi, float)
    cube = UsdGeom.Cube.Define(stage, path)
    xf = UsdGeom.Xformable(cube)
    xf.AddTranslateOp().Set(Gf.Vec3d(*((lo + hi) / 2)))
    xf.AddScaleOp().Set(Gf.Vec3f(*((hi - lo) / 2)))
    cube.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    mat = UsdShade.Material.Define(stage, path + "_mat")
    sh = UsdShade.Shader.Define(stage, path + "_mat/shader")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("diffuseColor", Sdf_ValueType("color3f")).Set(Gf.Vec3f(*color))
    sh.CreateInput("roughness", Sdf_ValueType("float")).Set(0.5)
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(cube.GetPrim()).Bind(mat)
    if collide:
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return cube


def Sdf_ValueType(name):  # noqa: N802
    from pxr import Sdf

    return getattr(Sdf.ValueTypeNames, name[0].upper() + name[1:])


def add_checkout(stage, cfg: dict) -> list:
    """A checkout counter (static collider) + a green sign: the 'clearly marked' checkout area."""
    lo, hi = cfg["lo"], cfg["hi"]
    add_box(stage, "/World/picky_checkout/counter", lo, hi, (0.42, 0.30, 0.20))
    top = [lo[0] - 0.01, lo[1] - 0.01, hi[2]], [hi[0] + 0.01, hi[1] + 0.01, hi[2] + 0.02]
    add_box(stage, "/World/picky_checkout/top", top[0], top[1], (0.12, 0.12, 0.13))
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    add_box(stage, "/World/picky_checkout/post", [cx - 0.02, cy - 0.02, hi[2]], [cx + 0.02, cy + 0.02, 1.9],
            (0.2, 0.2, 0.2), collide=False)  # fmt: skip
    long_x = (hi[0] - lo[0]) >= (hi[1] - lo[1])
    s = [0.45, 0.02] if long_x else [0.02, 0.45]
    add_box(stage, "/World/picky_checkout/sign", [cx - s[0], cy - s[1], 1.75], [cx + s[0], cy + s[1], 2.05],
            (0.05, 0.65, 0.25), collide=False)  # fmt: skip
    return [("/World/picky_checkout/counter", [*lo], [*hi], "checkout")]


# --- the rover -----------------------------------------------------------------------------------------------------
class Rover:
    def __init__(self, ctx, picky, name: str):
        self.c, self.p, self.name = ctx, picky, name
        self.phase, self.caption, self.sub = "idle", "", ""
        self.queue: list[dict] = []  # planned stops still to do
        self.done: list[dict] = []
        self.path: list = []
        self.target = None
        self.v = np.zeros(2)
        self.w = 0.0
        self.picked = []
        self.leg_reason = ""

    # -- helpers --
    def ev(self, kind: str, **kw):
        self.c.event(kind, robot=self.name, **kw)

    def wait(self, s: float):
        for _ in range(max(1, int(s / DT))):
            yield

    def base_xy(self):
        return np.array([self.p.x, self.p.y])

    def to_base(self, pw):
        d = np.array(pw, float) - np.array([self.p.x, self.p.y, self.p.z])
        c, s = math.cos(-self.p.yaw), math.sin(-self.p.yaw)
        return np.array([c * d[0] - s * d[1], s * d[0] + c * d[1], d[2]])

    def tcp(self):
        return self.p.gripper_pos()

    def arm_to(self, q, T: float):
        q0, q1 = np.array(self.p.arm, float), np.array(q, float)
        n = max(1, int(T / DT))
        for k in range(1, n + 1):
            self.p.arm = list(q0 + (q1 - q0) * minjerk(k / n))
            yield

    def arm_line(self, a, b, T: float):
        """Straight tool-point line in the base frame (x, z) through the front IK table."""
        n = max(1, int(T / DT))
        for k in range(1, n + 1):
            s = minjerk(k / n)
            q = self.c.ik.front(a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s)
            if q is None:
                return False
            self.p.arm = q
            yield
        return True

    # -- driving --
    def drive(self, goal, yaw_goal: float, vmax: float = 0.55):
        path = self.c.smap.astar(self.base_xy(), goal)
        if path is None:
            return False
        self.path = path
        self.ev("path", points=[[round(float(v), 3) for v in p] for p in path])
        amax, wmax = 0.5, 0.9
        seg = 1
        t0 = self.c.t
        while True:
            pos = self.base_xy()
            # advance along the polyline: lookahead target
            while seg < len(path) - 1 and np.linalg.norm(path[seg] - pos) < 0.35:
                seg += 1
            tgt = path[seg]
            remaining = np.linalg.norm(tgt - pos) + sum(
                np.linalg.norm(path[i + 1] - path[i]) for i in range(seg, len(path) - 1)
            )
            final = seg == len(path) - 1
            d = tgt - pos
            dist = np.linalg.norm(d)
            heading = math.atan2(d[1], d[0]) if dist > 1e-3 else self.p.yaw
            want_yaw = yaw_goal if (final and remaining < 0.7) or remaining < 0.25 else heading
            yerr = wrap(want_yaw - self.p.yaw)
            vdes = min(vmax, math.sqrt(2 * amax * max(remaining - 0.005, 0)) + 0.02)
            vdes *= max(0.15, math.cos(min(abs(wrap(heading - self.p.yaw)), math.pi / 2))) if remaining > 0.7 else 1
            vvec = d / dist * vdes if dist > 1e-4 else np.zeros(2)
            dv = vvec - self.v
            lim = amax * DT
            if np.linalg.norm(dv) > lim:
                dv = dv / np.linalg.norm(dv) * lim
            self.v = self.v + dv
            wdes = float(np.clip(2.2 * yerr, -wmax, wmax))
            self.w += float(np.clip(wdes - self.w, -2.0 * DT, 2.0 * DT))
            c, s = math.cos(self.p.yaw), math.sin(self.p.yaw)
            self.p.cmd = np.array([c * self.v[0] + s * self.v[1], -s * self.v[0] + c * self.v[1], self.w])
            if final and dist < 0.008 and abs(yerr) < 0.01:
                break
            if self.c.t - t0 > 90:
                self.ev("drive_timeout")
                break
            yield
        self.p.cmd = np.zeros(3)
        self.v[:] = 0
        self.w = 0.0
        # snap the last millimetres (kinematic base) so the arm table lines up exactly
        self.p.x, self.p.y, self.p.yaw = float(goal[0]), float(goal[1]), float(yaw_goal)
        self.path = []
        yield
        return True

    def strafe(self, dy: float, T: float = 0.6):
        """Small sideways base move (base frame y) - the swerve wheels steer 90 degrees for it."""
        n = max(1, int(T / DT))
        x0, y0 = self.p.x, self.p.y
        c, s = math.cos(self.p.yaw), math.sin(self.p.yaw)
        prev = 0.0
        for k in range(1, n + 1):
            u = minjerk(k / n) * dy
            v = (u - prev) / DT
            prev = u
            self.p.cmd = np.array([0.0, v, 0.0])
            yield
        self.p.cmd = np.zeros(3)
        self.p.x, self.p.y = x0 - s * dy, y0 + c * dy

    # -- picking --
    def pick(self, prod, reach: float):
        """Pre-grasp, slide in, correct from measured error, grab what is really at the gripper."""
        ik = self.c.ik
        center = prod.center
        z = float(center[2])
        x_pre = reach - PRE_BACK
        self.p.gripper_open = True
        self.phase, self.sub = "reach", "lining up the gripper"
        q_pre = ik.front(x_pre, z + PRE_UP)
        if q_pre is None or ik.front(reach, z) is None:
            return False, "is out of the arm's reach from this aisle"
        yield from self.arm_to(q_pre, 1.8)
        yield from self.wait(0.3)
        ok = yield from self.arm_line((x_pre, z + PRE_UP), (reach, z), 1.5)
        if not ok:
            return False, "is out of the arm's reach from this aisle"
        yield from self.wait(0.35)
        x, zz = reach, z
        self.sub = "fine alignment"
        err = None
        for it in range(4):  # closed-loop: measure in sim, correct arm (x, z) and base (y)
            err = self.to_base(center) - self.to_base(self.tcp())
            self.ev("align", it=it, err_cm=[round(float(v) * 100, 1) for v in err])
            if np.linalg.norm(err) < 0.012:
                break
            x, zz = x + float(err[0]), zz + float(err[2])
            q = ik.front(x, zz)
            if q is None:
                break
            if abs(err[1]) > 0.004:
                g = self.strafe(float(err[1]), 0.5)
                a = self.arm_to(q, 0.5)
                for _ in zip(g, a, strict=False):
                    yield
            else:
                yield from self.arm_to(q, 0.5)
            yield from self.wait(0.3)
        gap = float(np.linalg.norm(self.to_base(center) - self.to_base(self.tcp())))
        if gap > 0.05:
            return False, f"wasn't at the gripper ({gap * 100:.0f} cm off)"
        self.sub = "grasp"
        width = float(min(prod.hi[0] - prod.lo[0], prod.hi[1] - prod.lo[1]))
        self.p.finger_target = float(np.clip(width / 2 - 0.004, 0.0, 0.04))
        self.p.gripper_open = False
        yield from self.wait(0.6)
        if not self.p.grab(target=self.c.entity(prod), keep_offset=True, max_dist=0.3):
            self.p.gripper_open = True
            return False, "could not be attached"
        self.ev("grabbed", item=prod.name, gap_cm=round(gap * 100, 1))
        if self.c.debug:
            for k in range(30):
                if k % 5 == 0:
                    pos = self.p.held_body.get_world_poses()[0].numpy()[0]
                    self.ev(
                        "held_dbg", k=k, tensor=_r2(pos), usd=_r2(self.c.world_center(self.p.held)), tcp=_r2(self.tcp())
                    )
                yield
        self.phase, self.sub = "lift", "lift + retract"
        yield from self.arm_line((x, zz), (x, zz + 0.04), 0.6)
        yield from self.arm_line((x, zz + 0.04), (x_pre, zz + 0.05), 1.3)
        return True, ""

    def to_basket(self, prod):
        self.phase, self.sub = "basket", "into the basket"
        yield from self.arm_to(HOME, 1.4)
        yield from self.arm_to(BASKET, 1.6)
        yield from self.wait(0.3)
        held = self.p.held
        self.p.release()
        self.p.finger_target = None
        self.ev("released", item=prod.name)
        yield from self.wait(1.2)
        if held is not None:
            pb = self.to_base(self.c.world_center(held))
            inside = bool(np.all(pb >= BASKET_LO) and np.all(pb <= BASKET_HI))
            self.ev("in_basket", item=prod.name, inside=inside, base_xyz=[round(float(v), 3) for v in pb])
            if inside:
                self.p.stow(held)
        self.phase, self.sub = "home", ""
        yield from self.arm_to(HOME, 1.2)

    # -- checkout --
    def unload(self, slot_i: int):
        """Take the top item out of the basket and set it on the counter in front."""
        if not self.p.cargo:
            return False
        ent = max(self.p.cargo, key=lambda c: self.c.world_center(c[0])[2])[0]
        pb = self.to_base(self.c.world_center(ent))
        q = self.c.ik.down(pb[0], pb[1], pb[2])
        if q is None:
            return False
        self.phase, self.sub = "unload", "taking it out of the basket"
        self.p.gripper_open = True
        yield from self.arm_to(BASKET, 1.0)
        yield from self.arm_to(q, 1.2)
        yield from self.wait(0.3)
        gap = float(np.linalg.norm(self.c.world_center(ent) - self.tcp()))
        self.ev("unload_align", gap_cm=round(gap * 100, 1))
        if gap > 0.06:
            # one correction from the measured error
            pb2 = pb + (self.to_base(self.c.world_center(ent)) - self.to_base(self.tcp()))
            q2 = self.c.ik.down(pb2[0], pb2[1], pb2[2])
            if q2 is not None:
                yield from self.arm_to(q2, 0.6)
            gap = float(np.linalg.norm(self.c.world_center(ent) - self.tcp()))
        if gap > 0.08:
            self.ev("unload_skip", gap_cm=round(gap * 100, 1))
            return False
        self.p.gripper_open = False
        yield from self.wait(0.5)
        self.p.unstow(ent)
        self.p.grab(target=ent, keep_offset=True, max_dist=0.5)
        name = ent.GetParent().GetName()
        self.ev("unload_grab", item=name, gap_cm=round(gap * 100, 1))
        yield from self.arm_to(BASKET, 1.0)
        yield from self.arm_to(HOME, 1.0)
        top = self.c.cfg["checkout"]["hi"][2]
        xr = self.c.counter_reach
        q_drop = self.c.ik.front(xr, top + 0.14)
        yield from self.arm_to(q_drop, 1.4)
        self.p.release()
        self.ev("on_counter", item=name)
        yield from self.wait(0.8)
        yield from self.arm_to(HOME, 1.0)
        return True


class Ctx:
    """Shared state for the rovers: map, IK, planner, logs, the sim clock."""

    def __init__(self, stage, smap, ik, cfg, out: Path, world_pos):
        self.stage, self.smap, self.ik, self.cfg, self.out = stage, smap, ik, cfg, out
        self.t = 0.0
        self.events = (out / "events.jsonl").open("a")
        self.world_pos = world_pos
        self.bbc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        self.counter_reach = 0.62
        self._ents = {}
        self.debug = False

    def event(self, kind: str, **kw):
        rec = {"t": round(self.t, 3), "kind": kind, **kw}
        self.events.write(json.dumps(rec) + "\n")
        self.events.flush()
        print(f"[picky-demo] {rec}", flush=True)

    def entity(self, prod):
        if prod.path not in self._ents:
            self._ents[prod.path] = self.stage.GetPrimAtPath(prod.path)
        return self._ents[prod.path]

    def world_center(self, ent) -> np.ndarray:
        """Bounding-box centre of a (possibly moving) product: its rest-pose local box centre moved by its pose."""
        key = str(ent.GetPath())
        self._local_c = getattr(self, "_local_c", {})
        if key not in self._local_c:
            r = self.bbc.ComputeUntransformedBound(ent).ComputeAlignedRange()
            mid = (np.array(r.GetMin()) + np.array(r.GetMax())) / 2
            self._local_c[key] = Gf.Vec3d(*mid)
        m = UsdGeom.Xformable(ent).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        p = m.Transform(self._local_c[key])
        return np.array([p[0], p[1], p[2]])


# --- rover trip: plan -> stops -> checkout ---------------------------------------------------------------------------
def rover_trip(rv: Rover):
    c = rv.c
    while rv.queue:
        stop = rv.queue.pop(0)
        prod = c.items[stop["item"]]
        rv.target, rv.leg_reason = stop["item"], stop.get("reason", "")
        side = stop.get("side") if stop.get("side") in prod_sides(c, prod) else prod.side
        pose, yaw, reach = c.smap.approach(prod, side)
        rv.phase, rv.sub = "drive", c.names[stop["item"]]
        rv.ev("leg", item=stop["item"], name=c.names[stop["item"]], section=prod.section, side=side,
              reason=rv.leg_reason)  # fmt: skip
        if not c.smap.free(pose):
            ok = False
            why = f"the approach spot on the {side} side is blocked"
        else:
            ok = yield from rv.drive(pose, yaw)
            why = "no collision-free path to it"
        if ok:
            rv.phase = "pick"
            ok, why = yield from rv.pick(prod, reach)
        if not ok:
            rv.ev("pick_failed", item=stop["item"], why=why)
            rv.p.gripper_open = True
            yield from rv.arm_to(HOME, 1.0)
            yield from replan(rv, stop, f"{c.names[stop['item']]} ({prod.name}) {why}.")
            continue
        yield from rv.to_basket(prod)
        rv.done.append(stop["item"])
        c.picked.add(stop["item"])
        rv.ev("item_done", item=stop["item"], name=c.names[stop["item"]], n=len(rv.done))
    # checkout
    rv.target, rv.leg_reason = None, c.plan_reason.get(rv.name, "")
    rv.phase, rv.sub = "to_checkout", "heading to checkout"
    rv.ev("leg", item="checkout", name="Checkout", reason=rv.leg_reason)
    sx, sy, syaw = c.slots[rv.name]
    ok = yield from rv.drive(np.array([sx, sy]), syaw)
    if not ok:
        rv.ev("checkout_unreachable")
        return
    rv.phase, rv.sub = "checkout", "unloading"
    n = 0
    tries = 0
    while rv.p.cargo and tries < 30:
        tries += 1
        xr = 0.56 if n % 2 == 0 else 0.70
        c.counter_reach = xr
        ok = yield from rv.unload(n)
        if ok:
            n += 1
            if n % 2 == 0:
                yield from rv.strafe(c.slot_step[rv.name], 0.8)
        else:
            break
    rv.phase, rv.sub = "paid", ""
    rv.ev("paid", items=n, left_in_basket=len(rv.p.cargo))
    yield from rv.arm_to(HOME, 1.0)


def prod_sides(c, prod):
    f = c.smap.fixtures[prod.fixture]
    return [s for s in ("north", "south", "east", "west") if c.smap._side_open(f, s)]


def replan(rv: Rover, stop: dict, event: str):
    c = rv.c
    remaining = [stop] + rv.queue
    alts = {}
    failed = c.items[stop["item"]]
    for p in c.pickable:
        if p.sku == failed.sku and p.name != failed.name and p.path not in c.used_paths:
            alts.setdefault(stop["item"], []).append(p)
    rem = []
    for st in remaining:
        it = c.items[st["item"]]
        entry = {"id": st["item"], "name": c.names[st["item"]], "section": it.section, "pos": _r2(it.center[:2])}
        if st["item"] == stop["item"]:
            entry["failed"] = event
            entry["alternatives"] = []
            for k, p in enumerate(
                sorted(alts.get(stop["item"], []), key=lambda p: np.linalg.norm(p.center - failed.center))[:3]
            ):
                aid = f"{stop['item']}-alt{k + 1}"
                c.items[aid], c.names[aid] = p, c.names[stop["item"]]
                entry["alternatives"].append(
                    {"id": aid, "section": p.section, "pos": _r2(p.center[:2]), "side": p.side}
                )
        rem.append(entry)
    rv.phase, rv.sub = "replan", "asking Gemini to replan"
    rv.ev("replan_request", event=event)
    yield from rv.wait(0.5)
    t0 = time.time()
    try:
        out = c.planner.replan(
            c.map_summary, {"name": rv.name, "pose": _r2([rv.p.x, rv.p.y])}, rem, event, c.checkout_info
        )
        stops = (out.get("robots") or [{}])[0].get("stops", [])
        valid = [s for s in stops if s.get("item") in c.items and s["item"] not in c.picked]
        rv.queue = valid
        rv.ev("replan", summary=out.get("summary", ""), stops=valid, latency_s=round(time.time() - t0, 1),
              model=c.planner.model_used)  # fmt: skip
    except Exception as e:  # noqa: BLE001
        rv.queue = [s for s in rv.queue]  # drop the failed stop
        rv.ev("replan_failed", error=str(e)[:200])
    yield from rv.wait(0.5)


def chase_pose(rv):
    """Behind + right of the rover, above the shelves, looking at the arm's work area."""
    c, s = math.cos(rv.p.yaw), math.sin(rv.p.yaw)

    def w(bx, by, z):
        return np.array([rv.p.x + c * bx - s * by, rv.p.y + s * bx + c * by, z])

    return w(-1.45, -0.55, 1.85), w(0.75, -0.12, 0.85)


def _r2(v):
    return [round(float(x), 2) for x in v]


def greedy_plan(c, robots, item_ids):
    """Fallback if Gemini is unreachable: nearest-neighbour split + order (logged as such)."""
    left = list(item_ids)
    plan = {r["name"]: [] for r in robots}
    pos = {r["name"]: np.array(r["start"][:2]) for r in robots}
    while left:
        for r in robots:
            if not left:
                break
            best = min(left, key=lambda i: np.linalg.norm(c.items[i].center[:2] - pos[r["name"]]))
            left.remove(best)
            plan[r["name"]].append({"item": best, "side": c.items[best].side, "reason": "nearest remaining item"})
            pos[r["name"]] = c.items[best].center[:2]
    return plan


# --- setup helpers -------------------------------------------------------------------------------------------------
def topdown(app, stage, smap, out: Path, hide: list[str], px_n: int = 1024):
    """Orthographic top-down render (RGB for the video's mini-map + depth -> height map -> extra obstacles)."""
    import omni.replicator.core as rep
    from PIL import Image

    span = float(max(smap.hi - smap.lo)) + 0.8
    cx, cy = (smap.lo + smap.hi) / 2
    cam_z = 2.55
    cam = UsdGeom.Camera.Define(stage, "/World/picky_topdown")
    xf = UsdGeom.Xformable(cam)
    xf.ClearXformOpOrder()
    xf.AddTranslateOp().Set(Gf.Vec3d(float(cx), float(cy), cam_z))
    cam.CreateProjectionAttr("orthographic")
    cam.CreateHorizontalApertureAttr(span * 10)
    cam.CreateVerticalApertureAttr(span * 10)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.01, 10.0))
    vis = []
    for p in hide:
        im = UsdGeom.Imageable(stage.GetPrimAtPath(p))
        vis.append((im, im.ComputeVisibility()))
        im.MakeInvisible()
    rp = rep.create.render_product("/World/picky_topdown", (px_n, px_n))
    rgb = rep.AnnotatorRegistry.get_annotator("rgb")
    dep = rep.AnnotatorRegistry.get_annotator("distance_to_image_plane")
    rgb.attach([rp])
    dep.attach([rp])
    # annotators only capture while the timeline plays (Replicator captureOnPlay); the SO-101 demo renders this
    # before play, so capture on every update for now and fall back to an explicit orchestrator step
    import carb.settings

    cs = carb.settings.get_settings()
    cop = cs.get("/omni/replicator/captureOnPlay")
    cs.set("/omni/replicator/captureOnPlay", False)
    for _ in range(40):
        app.update()
    for k in range(200):
        if getattr(rgb.get_data(), "size", 0) > 0 and getattr(dep.get_data(), "size", 0) > 0:
            break
        if k % 20 == 19:
            try:
                rep.orchestrator.step(rt_subframes=4, pause_timeline=False)
            except Exception as e:  # noqa: BLE001
                print(f"[topdown] orchestrator.step: {e}", flush=True)
        app.update()
    # restore capture-on-play (unset counts as on): left False, every annotator during the run keeps returning its
    # first frame (the frozen-frame recordings of runs isaac1/isaac2)
    cs.set("/omni/replicator/captureOnPlay", True if cop is None else cop)
    img = np.asarray(rgb.get_data())[..., :3]
    d = np.asarray(dep.get_data(), dtype=np.float32)
    rgb.detach()
    dep.detach()
    rp.destroy()
    for im, _v in vis:
        im.MakeVisible()
    h = np.where(np.isfinite(d) & (d > 0.02), cam_z - d, np.nan)  # NaN: geometry above the camera (pillar, wall)
    # a camera inside a pillar/wall that reaches the ceiling sees the floor through it (back faces) but renders black
    h[img.astype(np.float32).mean(axis=2) < 10] = np.nan
    Image.fromarray(img).save(out / "topdown.jpg", quality=92)
    np.save(out / "heights.npy", h.astype(np.float16))
    px = span / px_n
    x0, y1 = float(cx - span / 2), float(cy + span / 2)
    smap.add_heightmap(h, x0, y1, px)
    meta = {"x0": x0, "y1": y1, "px": px, "n": px_n, "span": span}
    return meta


def pickable(smap, ik, used_names=()):
    from .store_map import FRONT_CLEAR

    out = []
    for p in smap.products:
        if p.fixture < 0 or not p.side or p.depth > 0.2 or p.name in used_names:
            continue
        size = p.hi - p.lo
        lateral = size[0] if p.side in ("north", "south") else size[1]
        # fingers open to 8 cm; wider packs (snack bags, cups) are still attached, the fingers just meet the pack
        if lateral > 0.14 or size[2] > 0.32 or not (0.45 <= p.center[2] <= 1.45):
            continue
        pose, yaw, reach = smap.approach(p)
        if not smap.free(pose):
            continue
        if ik.front(reach, p.center[2]) is None or ik.front(reach - PRE_BACK, p.center[2] + PRE_UP) is None:
            continue
        if reach < FRONT_CLEAR:
            continue
        out.append(p)
    return out


def choose_items(ctx, cfg, n_max):
    smap = ctx.smap
    by_name = {p.name: p for p in smap.products}
    chosen = []
    for it in cfg.get("items", []):
        p = by_name.get(it["product"])
        if p is not None:
            chosen.append((it.get("name") or p.sku, p))
    if not chosen:  # auto: round-robin over sections, distinct SKUs, closest to 1 m height
        secs = {}
        for p in ctx.pickable:
            secs.setdefault(p.section, {}).setdefault(p.sku, []).append(p)
        order = sorted(secs)
        k = 0
        while len(chosen) < n_max and any(secs.values()):
            s = order[k % len(order)]
            k += 1
            if not secs[s]:
                continue
            sku_ = min(secs[s], key=lambda q: min(abs(x.center[2] - 1.0) for x in secs[s][q]))
            p = min(secs[s].pop(sku_), key=lambda x: abs(x.center[2] - 1.0))
            chosen.append((p.sku, p))
    return chosen[:n_max]


def run_demo(app, stage, pickys, names, smap, cfg, args, step_all, world_pos):
    from .planner import Planner

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("events.jsonl", "frames.jsonl"):
        (out / f).unlink(missing_ok=True)
    ik = ArmIK(args.ik)
    c = Ctx(stage, smap, ik, cfg, out, world_pos)
    c.picked, c.used_paths = set(), set()
    c.debug = bool(getattr(args, "debug", False))
    t_setup = time.time()

    meta = topdown(app, stage, smap, out, hide=[p.path for p in pickys])
    smap.restrict_sides(cfg.get("open_sides", {}))
    c.pickable = pickable(smap, ik)
    chosen = choose_items(c, cfg, args.max_items)
    c.items, c.names = {}, {}
    for k, (nm, p) in enumerate(chosen):
        iid = f"I{k + 1}"
        c.items[iid], c.names[iid] = p, nm
        c.used_paths.add(p.path)
    (out / "pickable.json").write_text(json.dumps(
        [{"name": p.name, "sku": p.sku, "section": p.section, "side": p.side, "center": _r2(p.center), "size": _r2(p.hi - p.lo)}  # noqa: E501
         for p in c.pickable], indent=0))  # fmt: skip
    rovers = [Rover(c, p, n) for p, n in zip(pickys, names, strict=False)]

    # checkout slots
    ck = cfg["checkout"]
    c.slots = {
        r.name: (ck["slots"][r.name][0], ck["slots"][r.name][1], math.radians(ck["slots"][r.name][2])) for r in rovers
    }
    c.slot_step = {r.name: ck["slots"][r.name][3] for r in rovers}
    c.checkout_info = {"counter_min": _r2(ck["lo"][:2]), "counter_max": _r2(ck["hi"][:2]),
                       "rover_slots": {n: _r2(v[:2]) for n, v in c.slots.items()}}  # fmt: skip
    c.map_summary = {**smap.summary(), "ascii_map": smap.ascii()}
    sec_label = {s["id"]: f"{s['label']} ({s['where']})" for s in smap.sections}
    items_info = [
        {"id": i, "name": c.names[i], "section": p.section, "section_label": sec_label.get(p.section, ""),
         "pos": _r2(p.center[:2]), "shelf_height_m": round(float(p.center[2]), 2), "sides": prod_sides(c, p)}
        for i, p in c.items.items()
    ]  # fmt: skip
    robots_info = [
        {"name": r.name, "start": _r2([r.p.x, r.p.y]), "facing_deg": round(math.degrees(r.p.yaw))} for r in rovers
    ]
    planner = Planner(str(out / "llm_log.jsonl"))
    c.planner = planner
    c.plan_reason = {}
    plan_src, summary = "gemini", ""
    t0 = time.time()
    try:
        resp = planner.plan(c.map_summary, robots_info, items_info, c.checkout_info)
        per = {r["name"]: [] for r in robots_info}
        seen = set()
        for rb in resp.get("robots", []):
            if rb.get("robot") not in per:
                continue
            for st in rb.get("stops", []):
                if st.get("item") in c.items and st["item"] not in seen:
                    seen.add(st["item"])
                    per[rb["robot"]].append(st)
            c.plan_reason[rb["robot"]] = rb.get("checkout_reason", "")
        missing = [i for i in c.items if i not in seen]
        for i in missing:  # repair: give a dropped item to the robot whose last stop is closest
            best = min(
                per,
                key=lambda n: np.linalg.norm(
                    c.items[(per[n] or [{"item": i}])[-1]["item"]].center[:2] - c.items[i].center[:2]
                ),
            )
            per[best].append({"item": i, "side": c.items[i].side, "reason": "added back (the plan skipped it)"})
        summary = resp.get("summary", "")
        if missing:
            plan_src = f"gemini (repaired {len(missing)} missing)"
    except Exception as e:  # noqa: BLE001
        print(f"[picky-demo] Gemini plan failed: {e}; greedy fallback", flush=True)
        per = greedy_plan(c, robots_info, list(c.items))
        plan_src, summary = "greedy-fallback", "nearest-item order (LLM unavailable)"
    plan_latency = time.time() - t0
    for rv in rovers:
        rv.queue = list(per.get(rv.name, []))
    plan_doc = {
        "source": plan_src, "model": planner.model_used, "latency_s": round(plan_latency, 1), "summary": summary,
        "robots": {rv.name: rv.queue for rv in rovers}, "checkout_reason": c.plan_reason,
        "items": {i: {"name": c.names[i], "product": p.name, "section": p.section, "section_label": sec_label.get(p.section, ""),  # noqa: E501
                      "center": _r2(p.center)} for i, p in c.items.items()},
        "sections": [{**s, "lo": _r2(s["lo"]), "hi": _r2(s["hi"])} for s in smap.sections],
        "map": smap.to_json(), "topdown": meta, "slots": {n: list(v) for n, v in c.slots.items()},
        "checkout": {"lo": ck["lo"], "hi": ck["hi"]},
    }  # fmt: skip
    (out / "plan.json").write_text(json.dumps(plan_doc, indent=1))
    c.event("plan", source=plan_src, model=planner.model_used, summary=summary,
            robots={rv.name: rv.queue for rv in rovers})  # fmt: skip
    print(
        f"[picky-demo] setup {time.time() - t_setup:.0f}s, plan {plan_src} in {plan_latency:.1f}s: {summary}",
        flush=True,
    )
    if args.plan_only:
        return

    # cameras
    # chase cameras are world cameras that follow each rover (the robot's Xform itself doesn't move: the articulation
    # root below it is posed through PhysX), over the right shoulder where the arm works
    cams = {}
    chase_eye = {}
    for i, rv in enumerate(rovers):
        sfx = "" if i == 0 else str(i + 1)
        cams["chase" + sfx] = (
            look_at(stage, f"/World/picky_chase_{rv.name}", *chase_pose(rv), focal=15.0),
            tuple(args.res),
        )
        cams["wrist" + sfx] = (add_wrist_camera(stage, str(rv.p.link6.GetPath())), (640, 360))
    r0 = rovers[0]
    look_at(stage, "/World/picky_side_cam", [r0.p.x + 1.5, r0.p.y + 1.5, 1.5], [r0.p.x, r0.p.y, 0.8], focal=16.0)
    look_at(stage, "/World/picky_orbit_cam", [r0.p.x + 2, r0.p.y, 1.5], [r0.p.x, r0.p.y, 0.8], focal=16.0)
    ov = cfg.get("overview", {"eye": [*(smap.hi - 0.2), 2.5], "target": [*((smap.lo + smap.hi) / 2), 0.2]})
    look_at(stage, "/World/picky_over_cam", ov["eye"], ov["target"], focal=ov.get("focal", 12.0))
    cams["side"] = ("/World/picky_side_cam", tuple(args.res))
    cams["orbit"] = ("/World/picky_orbit_cam", tuple(args.res))
    cams["over"] = ("/World/picky_over_cam", tuple(args.res))
    try:
        from omni.kit.viewport.utility import get_active_viewport

        get_active_viewport().updates_enabled = False  # nothing looks at the GUI viewport while recording
    except Exception:  # noqa: BLE001
        pass
    rec = Recorder(out / "frames", cams) if args.record else None
    frames = (out / "frames.jsonl").open("w")
    side_eye = None
    k = 0

    def tick(phase: str, orbit_center=None, orbit_t=0.0):
        nonlocal k, side_eye
        if rec:
            rec.render((k + 1) % 2 == 0)
        step_all()
        c.t += DT
        k += 1
        if k % 2:
            return
        # camera director
        if orbit_center is not None:
            a = orbit_t * 2 * math.pi / 16.0 + cfg.get("orbit_start", 0.6)
            ox, oy = orbit_center
            rad = cfg.get("orbit_radius", 1.4)
            look_at(stage, "/World/picky_orbit_cam", [ox + rad * math.cos(a), oy + rad * math.sin(a), 1.6],
                    [ox, oy, 0.8], focal=14.0)  # fmt: skip
        for rv in rovers:
            eye, tgt = chase_pose(rv)
            e0 = chase_eye.get(rv.name)
            chase_eye[rv.name] = eye if e0 is None else e0 + (eye - e0) * 0.08
            t0_ = chase_eye.get(rv.name + "_t")
            chase_eye[rv.name + "_t"] = tgt if t0_ is None else t0_ + (tgt - t0_) * 0.15
            look_at(stage, f"/World/picky_chase_{rv.name}", chase_eye[rv.name], chase_eye[rv.name + "_t"], focal=15.0)
        cyw, syw = math.cos(r0.p.yaw), math.sin(r0.p.yaw)
        want = np.array([r0.p.x + cyw * 0.5 - syw * 1.7, r0.p.y + syw * 0.5 + cyw * 1.7, 1.45])
        ci = smap.cell(want[:2])
        if (
            not (0 <= ci[0] < smap.nx and 0 <= ci[1] < smap.ny)
            or smap.raw[max(ci[0] - 3, 0) : ci[0] + 4, max(ci[1] - 3, 0) : ci[1] + 4].any()
        ):
            want = np.array([r0.p.x + cyw * 0.3 - syw * 1.1, r0.p.y + syw * 0.3 + cyw * 1.1, 2.1])
        side_eye = want if side_eye is None else side_eye + (want - side_eye) * 0.06
        look_at(stage, "/World/picky_side_cam", side_eye, [r0.p.x + cyw * 0.3, r0.p.y + syw * 0.3, 0.85], focal=16.0)
        if rec:
            rec.capture()
        frames.write(json.dumps({
            "f": (rec.frame - 1) if rec else k // 2, "t": round(c.t, 3), "phase": phase,
            "rovers": [{"name": rv.name, "x": round(rv.p.x, 3), "y": round(rv.p.y, 3), "yaw": round(rv.p.yaw, 3),
                        "phase": rv.phase, "sub": rv.sub, "target": rv.target, "held": bool(rv.p.held),
                        "cargo": len(rv.p.cargo), "done": list(rv.done), "queue": [s["item"] for s in rv.queue]}
                       for rv in rovers]}) + "\n")  # fmt: skip

    t_run = time.time()
    if rec:
        rec.set_active(["orbit", "over"])
    c.event("intro")
    n_intro = int(args.intro / DT)
    for i in range(n_intro):
        tick("intro", orbit_center=(r0.p.x, r0.p.y), orbit_t=i * DT)
    if rec:
        rec.set_active([n for n in cams if n != "orbit"])
    c.event("start")
    gens = {rv.name: rover_trip(rv) for rv in rovers}
    while gens:
        for n, g in list(gens.items()):
            try:
                next(g)
            except StopIteration:
                del gens[n]
                c.event("rover_done", robot=n)
        tick("main")
        if c.t > args.max_sim_s:
            c.event("timeout")
            break
    if rec:
        rec.set_active(["orbit", "over"])
    c.event("outro")
    for i in range(int(args.outro / DT)):
        tick("outro", orbit_center=(r0.p.x, r0.p.y), orbit_t=i * DT)
    frames.close()
    if rec:
        rec.close()
    c.event("done", wall_s=round(time.time() - t_run, 1), frames=rec.frame if rec else 0)
