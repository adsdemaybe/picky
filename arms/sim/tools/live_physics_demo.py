"""Live physics demo: store_tmobile + LeKiwi/SO-101 rover under PhysX (standalone, not the gateway worker).

The rover drives on its base joints along the routing arm's visibility-graph paths; the arm picks phones (rigid
bodies) out of static U-slot holders with a top-down pinch (a FixedJoint locks the phone to the gripper only once the
closed jaws are on it, within 3.5 cm), lowers them into a 32 x 24 x 16 cm basket and lets go softly. Every other unit
gets a static collider. Close the window to stop.

Run with Isaac Sim's Python, the repo on PYTHONPATH, PICKY_ROVER_USD -> picky_rover.usda (arms/sim/tools/
compose_picky_rover.py) and PICKY_SIM_ASSET_DIR -> the fetched visuals (python -m arms.sim.fetch_visuals):
    python arms/sim/tools/live_physics_demo.py               # GUI window, loops forever
    python arms/sim/tools/live_physics_demo.py --headless OUT  # two rounds, JPEG shots into OUT
"""

import argparse
import itertools
import math
import os
import sys
import time
from pathlib import Path

HEADLESS = "--headless" in sys.argv
OUT = Path(sys.argv[sys.argv.index("--headless") + 1]) if HEADLESS else None
sys.path.insert(0, str(Path(__file__).parent))

from arms.sim.app import make_backend  # noqa: E402

args = argparse.Namespace(
    no_headless=not HEADLESS, width=1280, height=720, label_dir="/tmp/picky-live-labels", so101_urdf=None
)
sim_app, be = make_backend("isaac", args)
import low_quality  # noqa: E402

low_quality.apply(fps_cap=60)

import numpy as np  # noqa: E402
import omni.timeline  # noqa: E402
import physics_ik as ikdown  # noqa: E402
from pxr import Gf, PhysxSchema, UsdGeom, UsdPhysics, UsdShade  # noqa: E402

from arms.routing.visibility import visibility_map  # noqa: E402
from arms.sim import rover_puppet as puppet  # noqa: E402
from arms.sim.geometry import ARM_STOW_EXT_M, look_at  # noqa: E402
from arms.sim.worker import WorkerCore  # noqa: E402
from contracts.common import Pose2D  # noqa: E402
from utils.data import load_planogram  # noqa: E402

STORE = "store_tmobile"
ROVER_USD = os.environ.get("PICKY_ROVER_USD", "")
if not os.path.isfile(ROVER_USD):
    sys.exit("set PICKY_ROVER_USD to picky_rover.usda (arms/sim/tools/compose_picky_rover.py)")
st = be.stage

# ---------------------------------------------------------------- store (the worker's own build: theme, models, signs)
core = WorkerCore(be, lambda m: None)
core.load_stores([STORE])
plan = load_planogram(STORE)
spec = core.stores[STORE].spec
SROOT = f"/World/Stores/{STORE}"
st.RemovePrim(f"{SROOT}/Robots")  # the worker's kinematic rover is replaced by the physics rover below
OFF = UsdGeom.XformCommonAPI(st.GetPrimAtPath(SROOT)).GetXformVectors(0)[0]

# ---------------------------------------------------------------- physics scene + materials
scene = UsdPhysics.Scene.Define(st, "/World/Physics")
scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
scene.CreateGravityMagnitudeAttr(9.81)
px = PhysxSchema.PhysxSceneAPI.Apply(scene.GetPrim())
px.CreateTimeStepsPerSecondAttr(120)
px.CreateSolverTypeAttr("TGS")


def material(path, sf, df):
    m = UsdShade.Material.Define(st, path)
    api = UsdPhysics.MaterialAPI.Apply(m.GetPrim())
    api.CreateStaticFrictionAttr(sf)
    api.CreateDynamicFrictionAttr(df)
    api.CreateRestitutionAttr(0.0)
    return m


GRIP = material("/World/PhysMat/grip", 1.5, 1.3)
SURF = material("/World/PhysMat/surface", 0.7, 0.6)


def bind(prim, mat):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(mat, UsdShade.Tokens.weakerThanDescendants, "physics")


def cube(path, center, size, color, rot_z_deg=0.0, collide=True, visible=True):
    g = UsdGeom.Cube.Define(st, path)
    g.CreateSizeAttr(1.0)
    xf = UsdGeom.XformCommonAPI(g)
    xf.SetTranslate(Gf.Vec3d(*center))
    xf.SetRotate(Gf.Vec3f(0, 0, rot_z_deg))
    xf.SetScale(Gf.Vec3f(*size))
    g.CreateDisplayColorAttr([Gf.Vec3f(*color)])
    be._bind(g.GetPrim(), be._color_material("#{:02x}{:02x}{:02x}".format(*tuple(int(c * 255) for c in color))))
    if collide:
        UsdPhysics.CollisionAPI.Apply(g.GetPrim())
        bind(g.GetPrim(), SURF)
    if not visible:
        UsdGeom.Imageable(g).MakeInvisible()
    return g


# every static fixture prim already has CollisionAPI (_define); give them surface friction
for p in st.Traverse():
    if p.HasAPI(UsdPhysics.CollisionAPI) and str(p.GetPath()).startswith(f"/World/Stores/{STORE}"):
        bind(p, SURF)

# ---------------------------------------------------------- products: colliders, phones as rigid bodies in holders
PHONES = {}  # instance_id -> unit prim
for prim in spec.prims:
    if prim.role != "product":
        continue
    unit = st.GetPrimAtPath(prim.path)
    if not unit:
        continue
    w, d, h = prim.size
    yaw_deg = math.degrees(prim.yaw)
    cx, cy, cz = prim.center
    if prim.display == "stand":
        # static holder at the phone's base (lip in front = local -y, back plate behind), phone raised onto it
        stand = st.GetPrimAtPath(f"{prim.path}/stand")
        if stand:
            st.RemovePrim(stand.GetPath())
        c, s_ = math.cos(prim.yaw), math.sin(prim.yaw)

        def loc(lx, ly, lz, cx=cx, cy=cy, c=c, s_=s_):
            return (cx + c * lx - s_ * ly, cy + s_ * lx + c * ly, lz)

        base_z = cz - h / 2
        hp = f"{SROOT}/Holders/{prim.instance_id.replace('#', '_').replace('-', '_')}"
        slot_w = d + 0.004
        cube(f"{hp}/plate", loc(0, 0, base_z + 0.006), (w + 0.03, slot_w + 0.03, 0.012), (0.96, 0.96, 0.97), yaw_deg)
        cube(
            f"{hp}/lip",
            loc(0, -(slot_w / 2 + 0.004), base_z + 0.022),
            (w * 0.8, 0.008, 0.02),
            (0.96, 0.96, 0.97),
            yaw_deg,
        )
        cube(
            f"{hp}/back",
            loc(0, slot_w / 2 + 0.004, base_z + 0.05),
            (w * 0.6, 0.008, 0.075),
            (0.96, 0.96, 0.97),
            yaw_deg,
        )
        UsdGeom.XformCommonAPI(unit).SetTranslate(Gf.Vec3d(cx, cy, cz + 0.013))
        mdl = st.GetPrimAtPath(f"{prim.path}/model")
        if mdl:  # stand the model upright inside the holder (the display tilt would clip the back plate)
            for op in UsdGeom.Xformable(mdl).GetOrderedXformOps():
                if "tilt" in op.GetOpName():
                    op.Set(0.0)
                if op.GetOpName().endswith("place"):
                    v = op.Get()
                    op.Set(Gf.Vec3d(v[0], v[1], v[2] - 0.018))  # the puck it stood on is gone
        col = cube(f"{prim.path}/collider", (0, 0, 0), (w, d, h), (0.2, 0.2, 0.2), visible=False)
        bind(col.GetPrim(), GRIP)
        UsdPhysics.RigidBodyAPI.Apply(unit)
        UsdPhysics.MassAPI.Apply(unit).CreateMassAttr(prim.mass_kg or 0.2)
        rb = PhysxSchema.PhysxRigidBodyAPI.Apply(unit)  # settles instead of bouncing / spinning when let go
        rb.CreateLinearDampingAttr(0.8)
        rb.CreateAngularDampingAttr(2.0)
        PHONES[prim.instance_id] = unit
    else:
        # everything else: a static collider box, so nothing passes through it
        cube(f"{prim.path}/collider", (0, 0, 0), (w, d, h), (0.2, 0.2, 0.2), visible=False)

# ---------------------------------------------------------------- rover: physics articulation (LeKiwi + SO-101 + lift)
from utils.data import load_robots  # noqa: E402

DOCK = load_robots()["rover-tmo-1"].dock
RV = f"{SROOT}/PhysRover"
model = UsdGeom.Xform.Define(st, f"{RV}/Model")
model.AddTranslateOp().Set(Gf.Vec3d(0.0, 0.0, puppet.FLOOR_OFFSET_M))
model.AddRotateZOp().Set(math.degrees(puppet.HEADING_OFFSET_RAD))
LK = f"{RV}/Model/LeKiwi"
ref = st.DefinePrim(LK, "Xform")
ref.GetReferences().AddReference(ROVER_USD)
for _ in range(5):
    sim_app.update()
geo = be._build_puppet_geometry(ref)
CHAIN, M, ZREF = geo["chain"], geo["M"], geo["z_ref"]
art_api = PhysxSchema.PhysxArticulationAPI.Apply(ref)
art_api.CreateEnabledSelfCollisionsAttr(False)
art_api.CreateSleepThresholdAttr(0.0)
art_api.CreateSolverPositionIterationCountAttr(32)
for link in ("gripper", "jaw"):
    bind(st.GetPrimAtPath(f"{LK}/{link}"), GRIP)
# bigger basket: 32 x 24 x 16 cm (was 20 x 15 x 10), moved up against the mast and to the left, where the folded
# SO-101 reaches over it (pan -110 deg; the rear of the deck is out of reach). Base frame: +y forward, +x right.
B = f"{LK}/base/picky_basket"
BW, BD, BH, T, BX, BY, BZ = 0.32, 0.24, 0.16, 0.006, -0.05, -0.06, 0.115
for name, center, size in (
    ("floor", (BX, BY, BZ), (BW, BD, T)),
    ("wall_l", (BX - BW / 2, BY, BZ + BH / 2), (T, BD, BH)),
    ("wall_r", (BX + BW / 2, BY, BZ + BH / 2), (T, BD, BH)),
    ("wall_f", (BX, BY + BD / 2, BZ + BH / 2), (BW, T, BH)),
    ("wall_b", (BX, BY - BD / 2, BZ + BH / 2), (BW, T, BH)),
):
    pr = st.GetPrimAtPath(f"{B}/{name}")
    if pr:
        xf = UsdGeom.Xformable(pr)
        for op in xf.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeTranslate:
                op.Set(Gf.Vec3d(*center))
            elif op.GetOpType() == UsdGeom.XformOp.TypeScale:
                op.Set(Gf.Vec3f(*size))
SOFT = material("/World/PhysMat/basket_liner", 1.2, 1.0)
for name in ("floor", "wall_l", "wall_r", "wall_f", "wall_b"):
    if st.GetPrimAtPath(f"{B}/{name}"):
        bind(st.GetPrimAtPath(f"{B}/{name}"), SOFT)
JOINTS = {p.GetName(): p for p in st.Traverse() if p.IsA(UsdPhysics.Joint) and str(p.GetPath()).startswith(RV)}
for n, (kp, kd, fmax) in {
    "gripper": (5.0, 0.3, 80.0),
    "wrist_roll": (10.0, 0.6, 100.0),
    "wrist_flex": (40.0, 2.0, 400.0),
    "elbow_flex": (80.0, 4.0, 800.0),
    "shoulder_lift": (120.0, 6.0, 1200.0),
    "shoulder_pan": (80.0, 4.0, 800.0),
}.items():
    dr = UsdPhysics.DriveAPI.Get(JOINTS[n], "angular")
    dr.GetStiffnessAttr().Set(kp)
    dr.GetDampingAttr().Set(kd)
    dr.CreateMaxForceAttr(fmax)
dl = UsdPhysics.DriveAPI.Get(JOINTS["lift"], "linear")
dl.GetStiffnessAttr().Set(40000.0)
dl.GetDampingAttr().Set(4000.0)
dl.GetMaxForceAttr().Set(20000.0)

tl = omni.timeline.get_timeline_interface()
tl.play()
for _ in range(5):
    sim_app.update()
from isaacsim.core.experimental.prims import Articulation  # noqa: E402

ART = Articulation(LK)
DOFS = ART.dof_names
IDX = {n: i for i, n in enumerate(DOFS)}


def dof_read():
    q = ART.get_dof_positions()
    return np.array(q.numpy() if hasattr(q, "numpy") else q, dtype=np.float32)


TARGET = dof_read()


def base_dofs(x, y, theta):
    """World pose -> base joints (LeKiwi turned +90 deg: forward x = base_y, left y = -base_x)."""
    return {"base_y": x, "base_x": -y, "base_theta": theta}


start = base_dofs(DOCK.x, DOCK.y, DOCK.theta)
q0 = dof_read()
for n, v in start.items():
    q0[0][IDX[n]] = v
    TARGET[0][IDX[n]] = v
ART.set_dof_positions(q0)
ART.set_dof_position_targets(TARGET)


def set_targets(values):
    """values: {joint: rad | m}."""
    for n, v in values.items():
        if n in IDX:
            TARGET[0][IDX[n]] = v
    ART.set_dof_position_targets(TARGET)


def step(n=1):
    for _ in range(n):
        if not tl.is_playing():
            tl.play()
        sim_app.update()
        if not sim_app.is_running():
            raise SystemExit(0)


def settle(names, tol=0.02, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        step(3)
        q = dof_read()[0]
        if all(abs(q[IDX[n]] - TARGET[0][IDX[n]]) < tol for n in names if n in IDX):
            break
    step(10)


def robot_pose():
    q = dof_read()[0]
    return Pose2D(x=float(q[IDX["base_y"]]), y=float(-q[IDX["base_x"]]), theta=float(q[IDX["base_theta"]]))


# ---------------------------------------------------------------- motion: drive, arm, grip
VMAP = visibility_map(plan, clearance_m=0.35)
SPEED, TURN = 0.45, 1.6  # m/s, rad/s


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def drive_to(target: Pose2D):
    here = robot_pose()
    legs = VMAP.paths([here, target]).leg(0, 1, here, target)[1:]
    for wp in legs:
        cur = robot_pose()
        heading = math.atan2(wp.y - cur.y, wp.x - cur.x) if math.dist((cur.x, cur.y), (wp.x, wp.y)) > 0.02 else wp.theta
        turn_to(heading)
        dist = math.dist((cur.x, cur.y), (wp.x, wp.y))
        n = max(1, int(dist / SPEED * 60))
        for i in range(1, n + 1):
            f = i / n
            set_targets(base_dofs(cur.x + (wp.x - cur.x) * f, cur.y + (wp.y - cur.y) * f, heading))
            step()
    turn_to(target.theta)
    settle(["base_x", "base_y", "base_theta"], tol=0.01, timeout=2.0)


def turn_to(theta):
    cur = robot_pose()
    d = wrap(theta - cur.theta)
    n = max(1, int(abs(d) / TURN * 60))
    for i in range(1, n + 1):
        set_targets(base_dofs(cur.x, cur.y, cur.theta + d * i / n))
        step()


def lift_dz(h):
    return puppet.lift_offset(h, ZREF)


def stow(lift_h):
    q = geo["table"].config(ARM_STOW_EXT_M, max(0.0, ZREF - lift_h))
    q["wrist_roll"] = 0.0
    q["gripper"] = math.radians(25.0)
    q["lift"] = lift_dz(lift_h)
    set_targets(q)
    settle(["lift", "shoulder_lift", "elbow_flex", "wrist_flex"], timeout=6.0)


LAST_Q = {}


def to_robot(p):
    """World point -> robot frame of the current base pose."""
    r = robot_pose()
    dx, dy = p[0] - r.x, p[1] - r.y
    c, s_ = math.cos(-r.theta), math.sin(-r.theta)
    return (dx * c - dy * s_, dx * s_ + dy * c, p[2])


def arm_down(tcp_world, lift_h, grip_deg, wait=("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "lift")):
    global LAST_Q
    dz = lift_dz(lift_h)
    q, err = ikdown.solve_down(CHAIN, M, to_robot(tcp_world), dz, 0.0, 0.0, math.radians(grip_deg), prev=LAST_Q or None)
    LAST_Q = dict(q)
    set_targets(dict(q, lift=dz))
    settle(list(wait), timeout=5.0)
    return err


GRIP_JOINT = f"{RV}/GripAttach"


def grip_attach(item):
    xc = UsdGeom.XformCache()
    g = st.GetPrimAtPath(f"{LK}/gripper")
    gw, iw = xc.GetLocalToWorldTransform(g), xc.GetLocalToWorldTransform(item)
    tcp_w = gw.Transform(Gf.Vec3d(*CHAIN.tcp))
    d = iw.ExtractTranslation() - tcp_w
    if math.hypot(d[0], d[1]) > 0.035:
        print(f"LIVE grip missed by {math.hypot(d[0], d[1]) * 1000:.0f} mm", flush=True)
        return False
    rel = iw * gw.GetInverse()
    j = UsdPhysics.FixedJoint.Define(st, GRIP_JOINT)
    j.CreateBody0Rel().SetTargets([g.GetPath()])
    j.CreateBody1Rel().SetTargets([item.GetPath()])
    t, q = rel.ExtractTranslation(), rel.ExtractRotationQuat()
    j.CreateLocalPos0Attr(Gf.Vec3f(*t))
    j.CreateLocalRot0Attr(Gf.Quatf(q.GetReal(), *q.GetImaginary()))
    j.CreateLocalPos1Attr(Gf.Vec3f(0, 0, 0))
    j.CreateLocalRot1Attr(Gf.Quatf(1, 0, 0, 0))
    return True


def grip_release():
    if st.GetPrimAtPath(GRIP_JOINT):
        st.RemovePrim(GRIP_JOINT)


def world_of(prim):
    """Prim position in the store frame (the rover's frame of reference)."""
    return UsdGeom.XformCache().GetLocalToWorldTransform(prim).ExtractTranslation() - Gf.Vec3d(*OFF)


PHONE_HELD = [None]
DROP_LIFT = 0.20  # lift joint (m) while over the basket: the hanging phone clears the rim
DROP_LOW = 0.03  # lift joint (m) at release: phone inside the basket, gripper still above the rim
DROP_Q = {"shoulder_pan": -110.0, "shoulder_lift": -60.0, "elbow_flex": -10.0, "wrist_flex": 105.0}  # deg, FK-searched


def drop_in_basket():
    """Lift high, swing the folded arm over the basket (pan first so the phone stays clear of the mast), let go."""
    set_targets({"lift": DROP_LIFT})
    settle(["lift"], timeout=4.0)
    cur = {n: float(dof_read()[0][IDX[n]]) for n in DROP_Q}
    for i in range(1, 91):  # 1.5 s joint-space sweep
        f = 0.5 - 0.5 * math.cos(math.pi * i / 90)
        set_targets({n: cur[n] + (math.radians(v) - cur[n]) * f for n, v in DROP_Q.items()})
        step()
    settle(list(DROP_Q), timeout=3.0)
    # soft drop: ease the phone down into the basket (its bottom ~10 cm above the floor, gripper above the rim),
    # wait for the swing to die out, then let go and open the jaws slowly
    for i in range(1, 121):
        f = 0.5 - 0.5 * math.cos(math.pi * i / 120)
        set_targets({"lift": DROP_LIFT + (DROP_LOW - DROP_LIFT) * f})
        step()
    settle(["lift"], tol=0.005, timeout=3.0)
    step(30)
    held = PHONE_HELD[0]
    z_rel = world_of(held)[2] if held else None
    grip_release()
    g0 = float(dof_read()[0][IDX["gripper"]])
    for i in range(1, 46):
        set_targets({"gripper": g0 + (math.radians(30.0) - g0) * i / 45})
        step()
    step(60)
    if held is not None:
        print(f"LIVE soft drop: released at z {z_rel:.3f}, resting at z {world_of(held)[2]:.3f}", flush=True)
    set_targets({"lift": DROP_LIFT})  # back up out of the basket before the arm swings away
    settle(["lift"], timeout=3.0)


def pick_phone(slot_id):
    slot = plan.slot(slot_id)
    drive_to(slot.approach_pose)
    ids = [i for i in PHONES if i.startswith(slot_id + "#")]
    if not ids:
        return False
    r = robot_pose()

    # the phone straight ahead of the arm (nearest to the heading line)
    def lateral(i):
        p = world_of(PHONES[i])
        return abs(-(p[0] - r.x) * math.sin(r.theta) + (p[1] - r.y) * math.cos(r.theta))

    iid = min(ids, key=lateral)
    phone = PHONES[iid]
    p = world_of(phone)
    w, d, h = spec.item_dims.get(iid, (0.072, 0.0085, 0.15))
    top = p[2] + h / 2
    lift_h = top + 0.15
    stow(lift_h)
    # creep toward the table so the phone is ~0.20 m ahead of the shoulder (LeKiwi stays clear of the edge)
    fwd = (math.cos(r.theta), math.sin(r.theta))
    ahead = (p[0] - r.x) * fwd[0] + (p[1] - r.y) * fwd[1]
    creep = max(0.0, min(0.14, ahead - 0.30))
    set_targets(base_dofs(r.x + fwd[0] * creep, r.y + fwd[1] * creep, r.theta))
    settle(["base_x", "base_y"], tol=0.005, timeout=3.0)
    face = (p[0] - fwd[0] * (d / 2 + 0.003), p[1] - fwd[1] * (d / 2 + 0.003))
    arm_down((face[0], face[1], top + 0.07), lift_h, 25.0)
    arm_down((face[0], face[1], top - 0.04), lift_h, 25.0)
    set_targets({"gripper": math.radians(puppet.GRIPPER_CLOSED_DEG)})
    step(45)
    ok = grip_attach(phone)
    set_targets({"lift": lift_dz(lift_h + 0.14)})  # the lift raises the phone out of its holder
    settle(["lift"], timeout=4.0)
    arm_down((face[0] - fwd[0] * 0.10, face[1] - fwd[1] * 0.10, top + 0.12), lift_h + 0.14, puppet.GRIPPER_CLOSED_DEG)
    set_targets(base_dofs(r.x, r.y, r.theta))  # back off to the approach pose
    settle(["base_x", "base_y"], tol=0.005, timeout=3.0)
    if ok:
        PHONE_HELD[0] = phone
        drop_in_basket()
        BASKET.append(PHONES.pop(iid).GetPath())
        z = world_of(phone)[2]
        print(f"LIVE picked {iid} into the basket (now at z {z:.2f})", flush=True)
    stow(0.45)
    return ok


BASKET = []  # picked phone prims riding in the basket; checkout takes them (like the worker's unload_basket)
ROUNDS = [
    "TMO-TABLE-A-W0-00",
    "TMO-TABLE-B-W0-00",
    "TMO-TABLE-B-E0-02",
    "TMO-TABLE-A-W0-01",
    "TMO-TABLE-B-W0-03",
    "TMO-TABLE-A-E0-00",
    "TMO-TABLE-B-E0-00",
]


def shot(name, eye, target):
    if OUT is None:
        return
    jpg = be.render(f"dbg-{name}", "", look_at(eye, target, hfov=math.radians(65), w=960, h=600))
    if jpg:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}.jpg").write_bytes(jpg[0])
    tl.play()


if not HEADLESS:
    try:
        from isaacsim.core.rendering_manager import ViewportManager

        ViewportManager.set_camera_view("/OmniverseKit_Persp", eye=[1.0, 0.3, 2.7], target=[5.5, 5.0, 0.6])
    except Exception:  # noqa: BLE001
        pass
print("LIVE physics store up", flush=True)
stow(0.45)
_xc = UsdGeom.XformCache()
_bm = _xc.GetLocalToWorldTransform(st.GetPrimAtPath(f"{LK}/base"))
_g = world_of(st.GetPrimAtPath(f"{LK}/gripper"))
print(
    f"LIVE check: dock {DOCK}, pose {robot_pose()}, base at {_bm.ExtractTranslation() - Gf.Vec3d(*OFF)}, "
    f"gripper at {_g} (should be ahead along theta)",
    flush=True,
)
for k, slot_id in enumerate(itertools.islice(itertools.cycle(ROUNDS), 2 if HEADLESS else 10_000)):
    ok = pick_phone(slot_id)
    print(f"LIVE round {k}: {slot_id} {'ok' if ok else 'missed'}", flush=True)
    if HEADLESS:
        r = robot_pose()
        shot(f"r{k}_robot", (r.x - 1.0, r.y - 1.2, 1.6), (r.x, r.y, 0.6))
        shot(f"r{k}_basket", (r.x + 0.1, r.y - 0.4, 1.0), (r.x - 0.1, r.y, 0.25))
    drive_to(plan.checkout)
    for path in BASKET:
        st.RemovePrim(path)
    BASKET.clear()
    drive_to(DOCK)
sim_app.close()
