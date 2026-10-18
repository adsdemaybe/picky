"""Validate Picky's SO-101 rover v3 (PMB2 base + fixed riser + long-reach SO-101 + circular wire basket) in the Genie
Sim supermarket (Isaac Sim 6.x, headless) and record it.

Scripted test, every motion a smooth min-jerk trajectory within the actuator speed limits (position + velocity targets
on the joint drives; arm links have gravity, so the drives really hold the arm with their modelled torque):
  1. spawn in an aisle (planar base anchored at the spawn pose), arm stowed
  2. sweep every SO-101 joint through its range (arm pointing down the aisle; pan swept with the arm tucked),
     open/close the gripper
  3. drive the round base like a differential drive (turn in place, straight lines; wheels spin from v and w)
  4. park ~0.2 m off the shelf facing it, reach a product with IK (so101_ik.py): pre-grasp, straight slide in, close the
     moving jaw until it meets the product, and attach ONLY if the product is really between the jaws (product box vs.
     the jaw mesh sample points + the jaw stopped at the contact angle). It is attached where it is (no snapping).
  5. lift it off the shelf, retract, tuck, swing round to the basket, open: it falls into the basket (gravity, basket
     colliders) and rides along while the base drives away (friction, not re-posing)
Logs joint efforts (when the Isaac API exposes them) against the actuator limits. Writes metrics.json, per-camera JPEG
frames (director / chase / wrist / basket / head) and stills to --out.

    /data/advaiv2/isaacsim/python.sh validate_so101_rover.py --scene .../supermarket_06/background.usda \
        --robot picky_so101_rover.usd --out runs/val1 [--no-record] [--skip-sweep]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--scene", required=True)
ap.add_argument("--robot", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--product", default="benchmark_skincare_products_06", help="product prim name (Genie Sim instance)")
ap.add_argument("--shelf-front-y", type=float, default=1.68, help="world y of the shelf face in front of the product")
ap.add_argument("--spawn", type=float, nargs=3, default=[4.05, 1.10, 180.0], metavar=("X", "Y", "YAW_DEG"))
ap.add_argument("--standoff", type=float, default=0.40, help="pan axis -> product, horizontal (m)")
ap.add_argument("--no-record", action="store_true")
ap.add_argument("--skip-sweep", action="store_true")
ap.add_argument("--res", type=int, nargs=2, default=[1280, 720])
args = ap.parse_args()

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": args.res[0], "height": args.res[1]})

import numpy as np  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.core.experimental.prims import Articulation, RigidPrim  # noqa: E402
from pxr import Gf, Usd, UsdGeom, UsdLux, UsdPhysics  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from so101_ik import RoverKinematics, minjerk, minjerk_vel  # noqa: E402

OUT = Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)
DT = 1 / 60
G_OPEN = 1.10  # jaw opening for the approach: ~7.5 cm at the pads, clears the neighbouring stock
ROBOT = "/World/Picky"
KIN = RoverKinematics(Path(args.robot).with_suffix(".kin.json"))
K = KIN.k
METRICS: dict = {"events": [], "efforts": {}}
T0 = time.time()
SIM = {"t": 0.0, "frame": 0}


def log(msg: str) -> None:
    print(f"[val {time.time() - T0:6.1f}s] {msg}", flush=True)


def event(kind: str, **kw) -> None:
    kw = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in kw.items()}
    METRICS["events"].append({"t_sim": round(SIM["t"], 3), "kind": kind, **kw})
    log(f"{kind} {json.dumps(kw)[:400]}")


# --- the scene
def open_scene(path: str):
    ctx = omni.usd.get_context()
    ctx.open_stage(path)
    for _ in range(600):
        app.update()
        if ctx.get_stage_loading_status()[2] == 0 and ctx.get_stage() is not None:
            break
    return ctx.get_stage()


stage = open_scene(args.scene)
if not any(p.IsA(UsdPhysics.Scene) for p in stage.Traverse()):
    sc = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
    sc.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    sc.CreateGravityMagnitudeAttr(9.81)
if not any(p.IsA(UsdLux.DomeLight) or p.IsA(UsdLux.DistantLight) for p in stage.Traverse()):
    UsdLux.DomeLight.Define(stage, "/World/picky_dome").CreateIntensityAttr(1500)
# invisible ground slab: the market floor mesh answers raycasts but lets dynamic bodies through (see teleop_app.py)
g = UsdGeom.Cube.Define(stage, "/World/picky_ground")
UsdGeom.Xformable(g).AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.05))
UsdGeom.Xformable(g).AddScaleOp().Set(Gf.Vec3f(50, 50, 0.05))
g.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
UsdPhysics.CollisionAPI.Apply(g.GetPrim())

robot_prim = stage.DefinePrim(ROBOT, "Xform")
robot_prim.GetReferences().AddReference(str(Path(args.robot).resolve()))
SX, SY, SYAW = args.spawn[0], args.spawn[1], math.radians(args.spawn[2])
wf = stage.GetPrimAtPath(f"{ROBOT}/joints/world_fix")
wf.GetAttribute("physics:localPos0").Set(Gf.Vec3f(SX, SY, 0.0))
wf.GetAttribute("physics:localRot0").Set(Gf.Quatf(math.cos(SYAW / 2), 0, 0, math.sin(SYAW / 2)))


def find_product(name: str):
    for p in Usd.PrimRange(stage.GetPrimAtPath("/World/background")):
        if p.GetName() == name:
            return p.GetChild("entity") if p.GetChild("entity") else p
    raise SystemExit(f"product {name} not found")


prod = find_product(args.product)
PROD = str(prod.GetPath())
# product: kinematic rigid body (stays on the shelf until grasped) with a convex-hull collider switched on
prod.GetAttribute("physics:rigidBodyEnabled").Set(True)
prod.GetAttribute("physics:kinematicEnabled").Set(True)
for p in Usd.PrimRange(prod):
    if p.HasAPI(UsdPhysics.MeshCollisionAPI):
        UsdPhysics.MeshCollisionAPI(p).CreateApproximationAttr().Set(UsdPhysics.Tokens.convexHull)
    if p.HasAPI(UsdPhysics.CollisionAPI):
        UsdPhysics.CollisionAPI(p).CreateCollisionEnabledAttr().Set(True)


def prod_box_local():
    """product AABB in its own body frame + the body's world transform (USD, before it moves)."""
    bc = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    bb = bc.ComputeRelativeBound(prod, prod)
    r = bb.GetRange()
    lo, hi = np.array(r.GetMin()), np.array(r.GetMax())
    M = bb.GetMatrix()
    T = np.array(UsdGeom.Xformable(prod).ComputeLocalToWorldTransform(0)).T
    return lo, hi, np.array(M).T, T


BOX_LO, BOX_HI, BOX_M, PROD_T0 = prod_box_local()
c_local = BOX_M @ np.r_[(BOX_LO + BOX_HI) / 2, 1.0]
PROD_C0 = (PROD_T0 @ c_local)[:3]
event("product", path=PROD, center=PROD_C0.round(4), size=(BOX_HI - BOX_LO).round(4))


# --- cameras
def look_at(path, eye, target, focal=18.0):
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
    UsdGeom.Xformable(cam).GetOrderedXformOps()[0].Set(Gf.Matrix4d(*x, 0, *y, 0, *z, 0, *eye, 1))


look_at("/World/director", (SX + 1.5, SY - 1.6, 1.7), (SX, SY, 0.7), 16.0)


class Recorder:
    def __init__(self, cams: dict):
        import omni.replicator.core as rep

        self.pool = ThreadPoolExecutor(8)
        self.ann, self.rps = {}, {}
        for name, (path, res) in cams.items():
            (OUT / "frames" / name).mkdir(parents=True, exist_ok=True)
            rp = rep.create.render_product(path, res)
            an = rep.AnnotatorRegistry.get_annotator("rgb")
            an.attach([rp])
            self.ann[name], self.rps[name] = an, rp
        self.n = 0
        self.on = True

    def render(self, on: bool):
        """render products only update on the physics steps that get captured (every 2nd step = 30 fps)"""
        if on == self.on:
            return
        for rp in self.rps.values():
            try:
                rp.hydra_texture.set_updates_enabled(on)
            except Exception:  # noqa: BLE001
                pass
        self.on = on

    def capture(self):
        from PIL import Image

        for name, an in self.ann.items():
            d = an.get_data()
            if d is None or getattr(d, "size", 0) == 0:
                continue
            arr = np.asarray(d)[..., :3].copy()
            p = OUT / "frames" / name / f"{self.n:06d}.jpg"
            self.pool.submit(lambda a=arr, p=p: Image.fromarray(a).save(p, quality=90))
        self.n += 1

    def still(self, tag: str):
        from PIL import Image

        if not self.on:  # make sure the annotators hold a fresh frame
            self.render(True)
            app.update()
        for name, an in self.ann.items():
            d = an.get_data()
            if d is not None and getattr(d, "size", 0):
                Image.fromarray(np.asarray(d)[..., :3].copy()).save(OUT / f"still_{tag}_{name}.jpg", quality=92)

    def close(self):
        self.pool.shutdown(wait=True)


rec = None
if not args.no_record:
    cam = {k: ROBOT + v[len("/Picky") :] for k, v in K["cameras"].items()}
    rec = Recorder(
        {
            "director": ("/World/director", tuple(args.res)),
            "chase": (cam["chase"], tuple(args.res)),
            "wrist": (cam["wrist"], (640, 480)),
            "basket": (cam["basket"], (640, 480)),
            "head": (cam["head"], (640, 360)),
        }
    )

# --- physics
timeline = omni.timeline.get_timeline_interface()
timeline.play()
for _ in range(10):
    app.update()
art = Articulation(ROBOT)
NAMES = list(art.dof_names)
IDX = {n: i for i, n in enumerate(NAMES)}
log(f"dofs {NAMES}")
ARM = list(KIN.joints)
BASE = ["base_x", "base_y", "base_yaw"]
WHEELS = [w["joint"] for w in K["wheels"]]
CTRL = BASE + ARM + ["gripper"]
VMAX = {n: K["actuators"][n]["vmax_rad_s"] for n in ARM + ["gripper"]}
VMAX.update({"base_x": 0.6, "base_y": 0.6, "base_yaw": 0.9})
TMAX = {n: K["actuators"][n]["stall_Nm"] for n in ARM + ["gripper"]}
cmd_q = {n: 0.0 for n in CTRL}
cmd_v = {n: 0.0 for n in CTRL}
stow = K["poses"]["stow"]
cmd_q.update({k: v for k, v in stow.items()})
art.set_dof_positions([[cmd_q[n] for n in CTRL]], dof_indices=[IDX[n] for n in CTRL])
PRODB = {"body": RigidPrim(PROD)}
HELD = {"rel": None}


def effort_reader():
    for name in (
        "get_dof_projected_joint_forces",
        "get_measured_joint_efforts",
        "get_dof_efforts",
        "get_applied_joint_efforts",
    ):
        f = getattr(art, name, None)
        if f is None:
            continue
        try:
            v = f()
            v = v.numpy() if hasattr(v, "numpy") else np.asarray(v)
            v = np.asarray(v, float).reshape(-1)
            if v.size >= len(NAMES):
                log(f"efforts via Articulation.{name}")
                return name
        except Exception as e:  # noqa: BLE001
            log(f"efforts: {name} failed: {e!r}"[:200])
    return None


EFF_FN = effort_reader()
EFF = {n: [] for n in ARM + ["gripper"]}


def sample_efforts():
    if EFF_FN is None:
        return
    try:
        v = getattr(art, EFF_FN)()
        v = np.asarray(v.numpy() if hasattr(v, "numpy") else v, float).reshape(-1)
        for n in EFF:
            EFF[n].append(abs(float(v[IDX[n]])))
    except Exception:  # noqa: BLE001
        pass


def q_meas() -> dict:
    q = art.get_dof_positions().numpy()[0]
    return {n: float(q[IDX[n]]) for n in NAMES}


def base_T(q: dict) -> np.ndarray:
    """world pose of base_link from the planar joints"""
    yaw = SYAW + q["base_yaw"]
    c0, s0 = math.cos(SYAW), math.sin(SYAW)
    x = SX + c0 * q["base_x"] - s0 * q["base_y"]
    y = SY + s0 * q["base_x"] + c0 * q["base_y"]
    T = np.eye(4)
    T[:2, :2] = [[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]]
    T[:2, 3] = [x, y]
    return T


def arm_vec(q: dict) -> np.ndarray:
    return np.array([q[n] for n in ARM])


def world_tcp(q: dict) -> np.ndarray:
    return base_T(q) @ KIN.fk(arm_vec(q))


def world_gripper(q: dict) -> np.ndarray:
    return base_T(q) @ KIN.links(arm_vec(q))["so101_gripper"]


def quat_wxyz(R):
    q = Gf.Matrix3d(R.T.tolist()).ExtractRotation().GetQuat()
    return [q.GetReal(), *q.GetImaginary()]


DIRECTOR = {"eye": None, "tgt": None, "mode": "follow"}


def update_director(q):
    if rec is None:
        return
    Tb = base_T(q)
    tcp = world_tcp(q)[:3, 3]
    tgt = 0.55 * Tb[:3, 3] + 0.45 * tcp
    tgt[2] = 0.35 * 0.65 + 0.65 * tcp[2]
    fwd = Tb[:3, 0]
    right = -Tb[:3, 1]
    if DIRECTOR["mode"] == "pick":  # from the aisle, behind-right of the robot: arm, riser and basket in view
        eye = tgt + 1.25 * right - 1.05 * fwd + np.array([0, 0, 0.55])
    else:  # behind and to the right
        eye = tgt - 1.8 * fwd + 1.0 * right + np.array([0, 0, 0.95])
    if DIRECTOR["eye"] is None:
        DIRECTOR["eye"], DIRECTOR["tgt"] = eye, tgt
    a = 0.04
    DIRECTOR["eye"] = (1 - a) * DIRECTOR["eye"] + a * eye
    DIRECTOR["tgt"] = (1 - 0.08) * DIRECTOR["tgt"] + 0.08 * tgt
    look_at("/World/director", DIRECTOR["eye"], DIRECTOR["tgt"])


def wheel_speeds() -> list:
    """differential drive: wheel spin from the commanded base twist (v forward, w yaw rate), rolling without slip"""
    yawb = cmd_q["base_yaw"]
    c, s = math.cos(-yawb), math.sin(-yawb)
    vx_w, vy_w = cmd_v["base_x"], cmd_v["base_y"]
    vx, vy, wz = c * vx_w - s * vy_w, s * vx_w + c * vy_w, cmd_v["base_yaw"]
    out = []
    for w in K["wheels"]:
        px, py = w["center"][0], w["center"][1]
        ax = np.array(w["axis"])
        roll_dir = np.cross(ax, [0, 0, 1.0])
        v_hub = np.array([vx - wz * py, vy + wz * px, 0.0])
        out.append(float(np.dot(v_hub, roll_dir)) / w["radius"])
    return out


def step():
    """one physics + render step: push targets, wheel spin, carry a held product"""
    idx = [IDX[n] for n in CTRL]
    art.set_dof_position_targets([[cmd_q[n] for n in CTRL]], dof_indices=idx)
    art.set_dof_velocity_targets([[cmd_v[n] for n in CTRL]], dof_indices=idx)
    art.set_dof_velocity_targets([wheel_speeds()], dof_indices=[IDX[n] for n in WHEELS])
    if rec is not None:
        rec.render((SIM["frame"] + 1) % 2 == 0)
    app.update()
    SIM["t"] += DT
    SIM["frame"] += 1
    q = q_meas()
    if HELD["rel"] is not None:
        M = world_gripper(q) @ HELD["rel"]
        PRODB["body"].set_world_poses(positions=[M[:3, 3].tolist()], orientations=[quat_wxyz(M[:3, :3])])
        PRODB["body"].set_velocities(linear_velocities=[[0.0, 0.0, 0.0]], angular_velocities=[[0.0, 0.0, 0.0]])
    update_director(q)
    sample_efforts()
    if rec is not None and SIM["frame"] % 2 == 0:
        rec.capture()
    return q


def hold(seconds: float):
    for n in CTRL:
        cmd_v[n] = 0.0
    for _ in range(int(seconds / DT)):
        step()


def move(target: dict, T: float | None = None, vscale: float = 0.8, label: str | None = None):
    """min-jerk joint-space move of the named joints (others hold). Sets position AND velocity targets; the
    duration keeps every joint under vscale * its speed limit (min-jerk peak speed = 1.875 dq / T)."""
    names = list(target)
    q0 = np.array([cmd_q[n] for n in names])
    q1 = np.array([target[n] for n in names])
    if T is None:
        vmax = np.array([VMAX[n] for n in names])
        T = max(0.5, float(np.max(1.875 * np.abs(q1 - q0) / (vscale * vmax))))
    n_steps = max(1, int(T / DT))
    track = []
    tracked = [n for n in names if n not in BASE] or names
    for i in range(1, n_steps + 1):
        s = i / n_steps
        a, da = minjerk(s), minjerk_vel(s) / T
        for k, n in enumerate(names):
            cmd_q[n] = float(q0[k] + (q1[k] - q0[k]) * a)
            cmd_v[n] = float((q1[k] - q0[k]) * da)
        q = step()
        track.append(max(abs(q[n] - cmd_q[n]) for n in tracked))
    for n in names:
        cmd_v[n] = 0.0
    if label:
        event("move", label=label, T=round(T, 2), max_track_err=round(float(max(track)), 4) if track else 0.0)
    return T


def move_path(waypoints: list, T: float, label: str):
    """follow joint-space waypoints (dicts over ARM) with one min-jerk time scaling (straight tool lines)."""
    W = np.array([[w[n] for n in ARM] for w in waypoints])
    seg = np.r_[0, np.cumsum(np.linalg.norm(np.diff(W, axis=0), axis=1))]
    seg = seg / seg[-1] if seg[-1] > 1e-9 else np.linspace(0, 1, len(W))
    n_steps = max(1, int(T / DT))
    prev = W[0]
    for i in range(1, n_steps + 1):
        u = minjerk(i / n_steps)
        qv = np.array([np.interp(u, seg, W[:, j]) for j in range(len(ARM))])
        for j, n in enumerate(ARM):
            cmd_q[n] = float(qv[j])
            cmd_v[n] = float((qv[j] - prev[j]) / DT)
        prev = qv
        step()
    for n in ARM:
        cmd_v[n] = 0.0
    event("path", label=label, T=T, n=len(waypoints))


def arm_dict(qv) -> dict:
    return {n: float(v) for n, v in zip(ARM, qv, strict=False)}


# --- differential-drive moves
c0_, s0_ = math.cos(SYAW), math.sin(SYAW)


def to_joint(xw, yw, yaw):
    dx, dy = xw - SX, yw - SY
    return {"base_x": c0_ * dx + s0_ * dy, "base_y": -s0_ * dx + c0_ * dy, "base_yaw": yaw - SYAW}


def base_pose_cmd():
    q = {n: cmd_q[n] for n in BASE}
    T = base_T(q)
    return T[0, 3], T[1, 3], SYAW + q["base_yaw"]


def turn_to(yaw: float, label: str, vscale=0.7):
    x, y, yaw0 = base_pose_cmd()
    yaw = yaw0 + ((yaw - yaw0 + math.pi) % (2 * math.pi) - math.pi)
    move(to_joint(x, y, yaw), vscale=vscale, label=label)


def drive_to(xw: float, yw: float, label: str, reverse=False, vscale=0.7):
    """turn in place to face the goal (or face away when reversing), then a straight min-jerk line"""
    x, y, yaw0 = base_pose_cmd()
    h = math.atan2(yw - y, xw - x) + (math.pi if reverse else 0.0)
    if abs((h - yaw0 + math.pi) % (2 * math.pi) - math.pi) > 0.02:
        turn_to(h, label + "_turn", vscale)
    _, _, yaw1 = base_pose_cmd()
    move(to_joint(xw, yw, yaw1), vscale=vscale, label=label)


# --- jaw check
FJ = np.array(K["fixed_jaw_points"])  # gripper-link frame
MJ = np.array(K["moving_jaw_points"])  # moving-jaw link frame


def jaw_points_world(q: dict, g: float):
    Tg = world_gripper(q)
    Tj = base_T(q) @ KIN.jaw(arm_vec(q), g)
    f = (Tg @ np.c_[FJ, np.ones(len(FJ))].T).T[:, :3]
    m = (Tj @ np.c_[MJ, np.ones(len(MJ))].T).T[:, :3]
    return f, m


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


def prod_world_T() -> np.ndarray:
    p, o = PRODB["body"].get_world_poses()
    p, o = p.numpy()[0], o.numpy()[0]
    T = np.eye(4)
    T[:3, :3] = quat_to_R(*[float(v) for v in o])
    T[:3, 3] = p
    return T


def in_box(points_w, T_body, margin=0.0):
    """mask: which world points are inside the product's AABB (body frame, grown by margin)"""
    T = T_body @ BOX_M
    P = (np.linalg.inv(T) @ np.c_[points_w, np.ones(len(points_w))].T).T[:, :3]
    return np.all((P >= BOX_LO - margin) & (P <= BOX_HI + margin), axis=1)


def contact_angle(q: dict, g_open: float) -> float:
    """largest jaw angle (closing from g_open) where the moving jaw points first touch the product box"""
    Tb = prod_world_T()
    for g in np.arange(g_open, -0.18, -0.005):
        _, m = jaw_points_world(q, g)
        if in_box(m, Tb, margin=0.0005).any():
            return float(g)
    return -0.17


def between_jaws(q: dict, g: float) -> dict:
    """the product is between the jaws iff: moving jaw touches it (within 3 mm), fixed jaw is within 6 mm of the
    opposite face, and the product centre projects inside the jaw pad region (TCP frame)"""
    Tb = prod_world_T()
    f, m = jaw_points_world(q, g)
    touch_m = bool(in_box(m, Tb, margin=0.003).any())
    touch_f = bool(in_box(f, Tb, margin=0.006).any())
    Ttcp = world_tcp(q)
    c = (Tb @ BOX_M @ np.r_[(BOX_LO + BOX_HI) / 2, 1.0])[:3]
    c_t = np.linalg.inv(Ttcp) @ np.r_[c, 1.0]
    centred = bool(abs(c_t[1]) < 0.025 and -0.045 < c_t[2] < 0.03)
    return {
        "moving_jaw_touch": touch_m,
        "fixed_jaw_touch": touch_f,
        "centre_in_pads": centred,
        "centre_tcp": np.round(c_t[:3], 4).tolist(),
        "ok": touch_m and touch_f and centred,
    }


# --- the test
def run():
    hold(1.0)
    q = q_meas()
    event("spawned", base=base_T(q)[:3, 3].round(3), tcp=world_tcp(q)[:3, 3].round(3))
    if rec:
        rec.still("00_spawn")

    if not args.skip_sweep:
        # arm forward (pan -90 deg = down the aisle), raised: the pitch joints sweep in the vertical plane, clear of
        # the base, riser and basket (checked offline with so101_ik FK)
        home = {
            "shoulder_pan": -1.5708,
            "shoulder_lift": 0.2,
            "elbow_flex": -0.6,
            "wrist_flex": 0.0,
            "wrist_roll": 0.0,
            "gripper": 0.0,
        }
        move(home, label="unstow")
        sweeps = [
            ("shoulder_lift", [-1.0, 1.35, 0.2]),
            ("elbow_flex", [-1.6, 1.2, -0.6]),
            ("wrist_flex", [-1.6, 1.6, 0.0]),
            ("wrist_roll", [-2.6, 2.7, 0.0]),
            ("gripper", [1.70, -0.15, 0.0]),
        ]
        for jn, pts in sweeps:
            errs = []
            for p in pts:
                move({jn: p}, vscale=0.9)
                hold(0.15)
                errs.append(abs(q_meas()[jn] - p))
            event("sweep", joint=jn, points=pts, final_abs_err=[round(e, 4) for e in errs])
            if rec and jn == "elbow_flex":
                rec.still("01_sweep")
        # pan sweep with the arm tucked (radius ~0.25 m round the riser)
        tuck = {
            "shoulder_pan": -1.5708,
            "shoulder_lift": -0.08,
            "elbow_flex": 1.09,
            "wrist_flex": 0.56,
            "wrist_roll": 0.0,
        }
        move(tuck, label="tuck")
        errs = []
        for p in (-1.85, 1.85, -1.5708):
            move({"shoulder_pan": p}, vscale=0.9)
            hold(0.15)
            errs.append(abs(q_meas()["shoulder_pan"] - p))
        event("sweep", joint="shoulder_pan", points=[-1.85, 1.85, -1.5708], final_abs_err=[round(e, 4) for e in errs])
        move(stow, label="stow")

    # ---- drive (differential): down the aisle, turn to the shelf, park at the grasp standoff
    pick_c = PROD_C0
    approach_w = np.array([0.0, 1.0, 0.0])  # the shelf face is perpendicular to +y
    yaw_goal = math.atan2(approach_w[1], approach_w[0])
    # park so the pan axis is `standoff` short of the product (product straight ahead of the arm)
    pan_x = K["pan_axis_xy"][0]
    base_goal = pick_c[:2] - approach_w[:2] * (args.standoff + pan_x)
    gap = args.shelf_front_y - (base_goal[1] + K["footprint_radius"])
    DIRECTOR["mode"] = "follow"
    drive_to(base_goal[0], SY, "drive_down_aisle")
    q = q_meas()
    event("drove", base=base_T(q)[:3, 3].round(3), target=[float(base_goal[0]), SY])
    drive_to(base_goal[0], base_goal[1], "approach_shelf", reverse=base_goal[1] < SY)
    turn_to(yaw_goal, "face_shelf")
    hold(0.3)
    q = q_meas()
    Tb = base_T(q)
    event(
        "at_shelf",
        base=Tb[:3, 3].round(3),
        yaw_deg=round(math.degrees(SYAW + q["base_yaw"]), 2),
        goal=base_goal.round(3),
        prod=pick_c.round(3),
        gap_to_shelf=round(float(gap), 3),
    )
    if rec:
        rec.still("03_at_shelf")

    # ---- pick
    DIRECTOR["mode"] = "pick"
    p_b = (np.linalg.inv(Tb) @ np.r_[pick_c, 1.0])[:3]
    appr_b = np.linalg.inv(Tb)[:3, :3] @ approach_w
    R_goal = KIN.target_rotation(appr_b, KIN.grasp_roll)
    corners = np.array(
        [[x, y, z] for x in (BOX_LO[0], BOX_HI[0]) for y in (BOX_LO[1], BOX_HI[1]) for z in (BOX_LO[2], BOX_HI[2])]
    )
    cw = (PROD_T0 @ BOX_M @ np.c_[corners, np.ones(8)].T).T[:, :3]
    close_w = Tb[:3, :3] @ R_goal[:, 0]
    width = float(np.ptp(cw @ close_w))
    # SO-101 grasps against its fixed jaw: the fixed jaw pad face is at tool x = +0.002 (jaw mesh samples), so the
    # product centre goes to tool x = 0.002 - width/2 - 1.5 mm clearance; the moving jaw then closes onto it
    off_x = 0.002 - width / 2 - 0.0015
    tcp_goal = p_b - R_goal[:, 0] * off_x
    pre = tcp_goal - appr_b * 0.12
    q_pre, e_pre = KIN.solve(pre, R=R_goal, w_rot=0.1)
    event("ik_pregrasp", q=np.round(q_pre, 3), err=e_pre, width=round(width, 4))
    # unfold from the stow: raise the folded arm first (clear of the shelf edge), then to the pre-grasp
    move(
        {
            "shoulder_pan": float(q_pre[0]),
            "shoulder_lift": -0.3,
            "elbow_flex": 0.9,
            "wrist_flex": 0.4,
            "wrist_roll": float(q_pre[4]),
        },
        vscale=0.85,
        label="raise_ready",
    )
    move({**arm_dict(q_pre), "gripper": G_OPEN}, vscale=0.8, label="to_pregrasp")
    hold(0.3)
    line = KIN.line(q_pre, pre, tcp_goal, R=R_goal, n=14, w_rot=0.1)
    event("ik_line", max_pos_err=round(max(e[0] for _, e in line), 4))
    move_path([arm_dict(qq) for qq, _ in line], T=2.0, label="slide_in")
    hold(0.4)
    q = q_meas()
    tcp_err = np.linalg.norm(world_tcp(q)[:3, 3] - (Tb @ np.r_[tcp_goal, 1])[:3])
    g_c = contact_angle(q, G_OPEN)
    event("jaw_contact_angle", g=round(g_c, 3), tcp_err=round(float(tcp_err), 4))
    move({"gripper": g_c}, T=1.0, label="close_jaw")
    hold(0.4)
    q = q_meas()
    chk = between_jaws(q, q["gripper"])
    chk["jaw_measured"] = round(q["gripper"], 4)
    chk["jaw_target"] = round(g_c, 4)
    event("grasp_check", **chk)
    if rec:
        rec.still("04_grasp")
    if not chk["ok"]:
        event("grasp_failed")
        return False
    # attach exactly where it is relative to the gripper (no snapping): dynamic body, gravity off while held
    prod.GetAttribute("physics:kinematicEnabled").Set(False)
    step()
    PRODB["body"] = RigidPrim(PROD)
    PRODB["body"].set_enabled_gravities([False])
    q = q_meas()
    HELD["rel"] = np.linalg.inv(world_gripper(q)) @ prod_world_T()
    event("attached", rel_pos=HELD["rel"][:3, 3].round(4))
    up = tcp_goal + np.array([0, 0, 0.025])
    line_up = KIN.line(arm_vec(q), tcp_goal, up, R=R_goal, n=6, w_rot=0.1)
    move_path([arm_dict(qq) for qq, _ in line_up], T=1.0, label="lift_off")
    back = up - appr_b * 0.16
    line_bk = KIN.line(line_up[-1][0], up, back, R=R_goal, n=10, w_rot=0.1)
    move_path([arm_dict(qq) for qq, _ in line_bk], T=1.6, label="retract")
    if rec:
        rec.still("05_retract")
    # ---- to the basket: tuck (gripper down, ~0.25 m round the riser), swing the pan round, open
    bk = K["basket"]
    drop = np.array([bk["center"][0], bk["center"][1], bk["rim_z"] + 0.12])
    best = None
    for s in ([1.5708, -0.08, 1.09, 0.56, 0.0], [1.5708, -0.4, 1.4, 0.4, 0.0], [1.5708, 0.2, 0.8, 0.8, 0.0]):
        qd, ed = KIN.solve(drop, approach=(0, 0, -1), ori_mode="axis", seeds=[s])
        if best is None or ed[0] < best[1][0]:
            best = (qd, ed)
    q_drop, e_drop = best
    event("ik_drop", q=np.round(q_drop, 3), err=e_drop)
    carry = q_drop.copy()
    carry[0] = -1.5708
    move(arm_dict(carry), vscale=0.85, label="tuck_with_item")
    move({"shoulder_pan": float(q_drop[0])}, vscale=0.9, label="swing_to_basket")
    hold(0.3)
    if rec:
        rec.still("06_over_basket")
    rel_before = HELD["rel"]
    HELD["rel"] = None
    PRODB["body"].set_enabled_gravities([True])
    PRODB["body"].set_velocities(linear_velocities=[[0.0, 0.0, -0.05]], angular_velocities=[[0.0, 0.0, 0.0]])
    move({"gripper": 1.2}, T=0.6, label="open_release")
    hold(1.5)
    q = q_meas()

    def in_basket(q):
        Tp = prod_world_T()
        c = (Tp @ BOX_M @ np.r_[(BOX_LO + BOX_HI) / 2, 1.0])[:3]
        cb = (np.linalg.inv(base_T(q)) @ np.r_[c, 1.0])[:3]
        r = math.hypot(cb[0] - bk["center"][0], cb[1] - bk["center"][1])
        ok = r < bk["r_top"] and bk["floor_z"] - 0.02 < cb[2] < bk["rim_z"]
        return bool(ok), cb

    ok, cb = in_basket(q)
    event("released", in_basket=ok, product_in_base=cb.round(4), rel_before=rel_before[:3, 3].round(4))
    if rec:
        rec.still("07_in_basket")
    move(stow, vscale=0.85, label="stow_after_drop")
    # ---- drive away with the product in the basket (reverse out, turn down the aisle, drive)
    DIRECTOR["mode"] = "follow"
    x, y, _ = base_pose_cmd()
    drive_to(x, y - 0.25, "back_off", reverse=True)
    drive_to(x - 1.0, y - 0.25, "drive_away")
    hold(0.8)
    q = q_meas()
    ok2, cb2 = in_basket(q)
    event("after_drive", in_basket=ok2, product_in_base=cb2.round(4), base=base_T(q)[:3, 3].round(3))
    if rec:
        rec.still("08_drive_away")
    return ok and ok2


try:
    result = run()
    METRICS["result"] = bool(result)
except Exception as e:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    METRICS["result"] = False
    METRICS["error"] = repr(e)
finally:
    METRICS["frames"] = rec.n if rec else 0
    METRICS["sim_seconds"] = round(SIM["t"], 2)
    METRICS["effort_api"] = EFF_FN
    for n, v in EFF.items():
        if v:
            METRICS["efforts"][n] = {
                "max_Nm": round(float(np.max(v)), 3),
                "p95_Nm": round(float(np.percentile(v, 95)), 3),
                "limit_Nm": TMAX[n],
            }
    (OUT / "metrics.json").write_text(json.dumps(METRICS, indent=1))
    log(
        f"RESULT {METRICS.get('result')} frames={METRICS['frames']} sim_s={METRICS['sim_seconds']} "
        f"efforts={METRICS['efforts']}"
    )
    if rec:
        rec.close()
    app.close()
