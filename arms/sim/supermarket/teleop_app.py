"""Picky Isaac Sim app: Genie Sim supermarket + rm2_car/AIRBOT robot with basket, keyboard-drivable.

Targets Isaac Sim 6.x (pip `isaacsim`, `isaacsim.core.experimental` API).

    bash arms/sim/supermarket/run_sim.sh --livestream    # wraps: python teleop_app.py --scene ... --robot ...
    bash arms/sim/supermarket/run_sim.sh --smoke         # headless self-test with screenshots
    bash arms/sim/supermarket/run_sim.sh --demo --record # Gemini-planned shopping run, recorded (see demo/)

--demo runs Picky's SO-101 rover v3 (demo/so101_demo.py; --robot = picky_so101_rover.usd from
robot/build_so101_rover.py). `--demo --demo-robot rm2` keeps the earlier rm2_car/AIRBOT autopilot (demo/autopilot.py);
teleop, --smoke and --livestream drive the rm2_car/AIRBOT robot (--robot = rm2_basket.usd) as before.

Controls (focus the viewport):
  Fly the camera      hold right mouse + W/A/S/D/Q/E (built-in viewport navigation)
  Drive robot         I/K forward/back · J/L turn · U/O strafe · SPACE stop
  Arm                 1-6 select joint · UP/DOWN move it · H home pose · N reach-front pose · B over-basket pose
  Gripper             G toggle open/close · P grab nearest product near the gripper · R release
  Camera              V toggle chase camera / free camera

Products in Genie Sim scenes ship with rigid bodies disabled (static); `--dynamic-radius` turns the ones near the
robot's spawn into real rigid bodies. Grabbing is attach-on-contact: the nearest product within reach of the gripper
is welded to it with a fixed joint until released.
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--scene", required=True, help="Genie Sim background.usda")
ap.add_argument("--robot", required=True, help="rm2_basket.usd from arms/sim/supermarket/robot/")
ap.add_argument("--spawn", type=float, nargs=3, default=[-0.4, 0.3, 0.0], metavar=("X", "Y", "YAW_DEG"))
ap.add_argument(
    "--dynamic-radius",
    type=float,
    default=0.0,
    help="make products within this many m of spawn dynamic (-1 = all). Default 0: products stay static "
    "until grabbed, because tightly packed shelf stock pushes itself apart when many turn dynamic",
)
ap.add_argument(
    "--drive",
    choices=["kinematic", "physics"],
    default="kinematic",
    help="kinematic: base pose integrated directly (robust); physics: swerve wheel drives only",
)
ap.add_argument("--headless", action="store_true")
ap.add_argument("--smoke", action="store_true", help="headless scripted self-test, saves screenshots, then exits")
ap.add_argument("--out", default="/tmp/picky_smoke", help="screenshot dir for --smoke")
ap.add_argument("--livestream", action="store_true", help="enable WebRTC livestream (view from a browser/client)")
# --demo: autopilot shopping run (demo/autopilot.py): Gemini plans the route, the rovers drive + pick + check out
ap.add_argument("--demo", action="store_true", help="LLM-planned autopilot shopping run (headless unless --livestream)")
ap.add_argument("--demo-robot", choices=["so101", "rm2"], default="so101",
                help="--demo robot: so101 = Picky SO-101 rover v3 (demo/so101_demo.py), rm2 = rm2_car/AIRBOT")  # fmt: skip # noqa: E501
ap.add_argument("--so101-config", default=str(Path(__file__).parent / "demo" / "so101_config.json"))
ap.add_argument("--light-scale", type=float, default=0.8, help="--demo so101: scale every scene light (exposure)")
ap.add_argument("--demo-config", default=str(Path(__file__).parent / "demo" / "demo_config.json"))
ap.add_argument(
    "--ik", default=str(Path(__file__).parent / "demo" / "arm_ik.json"), help="robot/solve_arm_ik.py output"
)
ap.add_argument("--robots", type=int, default=1, help="how many rovers shop at once (--demo)")
ap.add_argument("--max-items", type=int, default=12)
ap.add_argument("--record", action="store_true", help="--demo: save camera frames + per-frame log to --out")
ap.add_argument("--res", type=int, nargs=2, default=[1920, 1080], help="--demo main camera resolution")
ap.add_argument("--plan-only", action="store_true", help="--demo: build the map, ask Gemini for the plan, exit")
ap.add_argument("--intro", type=float, default=7.0, help="--demo: seconds of intro orbit")
ap.add_argument("--outro", type=float, default=7.0)
ap.add_argument("--max-sim-s", type=float, default=900.0)
ap.add_argument(
    "--rec-cams", default="director,birdseye,side,wrist,front,basket", help="--demo so101: cameras to record"
)
ap.add_argument("--capture-every", type=int, default=2, help="--demo so101: record every Nth 60 Hz step (2 = 30 fps)")
ap.add_argument("--debug", action="store_true", help="--demo: log held-product poses after each grab")
args = ap.parse_args()
if args.smoke or (args.demo and not args.livestream):
    args.headless = True

from isaacsim import SimulationApp  # noqa: E402  (must start before any omni import)

if args.livestream:
    # WebRTC stream for the Isaac Sim WebRTC Streaming Client (signaling 49100/tcp, media 47998/udp).
    import isaacsim  # noqa: E402

    experience = str(Path(isaacsim.__file__).parent / "apps" / "isaacsim.exp.full.streaming.kit")
    simulation_app = SimulationApp({"headless": True, "width": 1600, "height": 900}, experience=experience)
else:
    simulation_app = SimulationApp({"headless": args.headless, "width": 1600, "height": 900})

import carb  # noqa: E402
import carb.input  # noqa: E402
import numpy as np  # noqa: E402
import omni.appwindow  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.core.experimental.prims import Articulation, RigidPrim  # noqa: E402
from pxr import Gf, Usd, UsdGeom, UsdLux, UsdPhysics  # noqa: E402

ROBOT_PATH = "/World/Picky"
CHASE_CAM = f"{ROBOT_PATH}/chase_cam"

# rm2_car geometry (DISCOVERSE): steering module positions in the base frame, wheel radius.
STEER_XY = {1: (0.2585, 0.184), 2: (0.2585, -0.184), 3: (-0.238, -0.184), 4: (-0.238, 0.184)}
WHEEL_R = 0.1
WHEEL_SIGN = {1: 1.0, 2: -1.0, 3: -1.0, 4: 1.0}  # wheel frames are mirrored left/right in the model

ARM_JOINTS = ["arm_joint1", "arm_joint2", "arm_joint3", "arm_joint4", "arm_joint5", "gripper_base_joint"]
# Presets solved with damped-least-squares IK on the same model (MuJoCo), radians.
POSES = {
    "home": [0.0, -0.431, 0.853, 1.571, -1.859, 0.251],  # tucked above the front deck, gripper down
    "front": [0.121, -1.254, 0.694, 2.559, 0.297, -1.626],  # reach 0.75 m ahead at 1.0 m height (shelf level)
    "basket": [-0.48, -0.69, 3.16, -1.571, -1.859, -1.4],  # gripper 0.30 m above the basket floor, centred
}
FINGER_OPEN, FINGER_CLOSED = 0.04, 0.0
MAX_V, MAX_W, ARM_STEP = 0.8, 1.0, 0.03


def log(msg: str) -> None:
    print(f"[picky] {msg}", flush=True)


def find_articulation_root(stage, under: str) -> str:
    for prim in Usd.PrimRange(stage.GetPrimAtPath(under)):
        if prim.HasAPI(UsdPhysics.ArticulationRootAPI):
            return str(prim.GetPath())
    raise RuntimeError(f"no ArticulationRootAPI under {under}")


def find_prim(stage, under: str, name: str):
    return next((p for p in Usd.PrimRange(stage.GetPrimAtPath(under)) if p.GetName() == name), None)


def product_entities(stage) -> list:
    """Genie Sim product instances: /World/background/[Group_*/]<benchmark_|genie_><cat>_<id>_<n>/entity (rigid body,
    disabled). Some scenes group a shelf with its stock under Group_* prims, so this looks one level into those."""
    out = []

    def rec(parent, depth):
        for prim in parent.GetChildren():
            name = prim.GetName()
            if any(k in name for k in ("shelf", "freezer", "cart")):
                continue
            if name.startswith(("benchmark_", "genie_")):
                ent = prim.GetChild("entity")
                if ent and ent.HasAPI(UsdPhysics.RigidBodyAPI):
                    out.append(ent)
            elif depth < 2:
                rec(prim, depth + 1)

    bg = stage.GetPrimAtPath("/World/background")
    if bg:
        rec(bg, 0)
    return out


def make_dynamic(entity) -> None:
    """Enable a Genie Sim product's rigid body. Their colliders are convex *decompositions*, which PhysX cooks
    asynchronously and slowly; until cooked the body has no shape and falls through the world. Convex hulls cook
    almost instantly and are fine for bottles/boxes/cans."""
    entity.GetAttribute("physics:rigidBodyEnabled").Set(True)
    for prim in Usd.PrimRange(entity):
        if prim.HasAPI(UsdPhysics.MeshCollisionAPI):
            UsdPhysics.MeshCollisionAPI(prim).CreateApproximationAttr().Set(UsdPhysics.Tokens.convexHull)
            # the Genie Sim scenes disable most product colliders (collisionEnabled = 0); without this a released
            # product falls through the basket and the floor
            UsdPhysics.CollisionAPI(prim).CreateCollisionEnabledAttr().Set(True)


def world_pos(prim) -> np.ndarray:
    t = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0).ExtractTranslation()
    return np.array([t[0], t[1], t[2]])


def yaw_quat(yaw: float) -> list[float]:
    return [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]  # wxyz


class Picky:
    def __init__(self, stage, path: str = ROBOT_PATH, spawn=None, products=None):
        self.stage = stage
        self.path = path
        self.root_path = find_articulation_root(stage, path)
        self.robot = Articulation(self.root_path)
        self.cmd = np.zeros(3)  # vx, vy, wz in base frame
        spawn = args.spawn if spawn is None else spawn
        self.x, self.y, self.yaw = spawn[0], spawn[1], math.radians(spawn[2])
        self.z = 0.0
        self.arm = list(POSES["home"])
        self.sel = 0
        self.gripper_open = True
        self.finger_target = None  # closed-finger opening (m) for the demo's grasps; None = fully closed
        self.held = None
        self.held_rel = None
        self.held_body = None
        self.cargo = []  # (entity, body, pose relative to the base) - items riding in the basket (--demo)
        self.products = product_entities(stage) if products is None else products
        self.link6 = find_prim(stage, path, "arm_link6")

    def post_play(self):
        """Call once physics is running (tensor views valid)."""
        r = self.robot
        idx = lambda names: [int(i) for i in r.get_dof_indices(names).numpy()]  # noqa: E731
        self.arm_idx = idx(ARM_JOINTS)
        self.finger_idx = idx(["finger_left_joint", "finger_right_joint"])
        self.steer_idx = idx([f"steer_joint{i}" for i in STEER_XY])
        self.wheel_idx = idx([f"wheel_joint{i}" for i in STEER_XY])
        self.z = self.floor_z(self.x, self.y)
        UsdGeom.Xformable(self.stage.GetPrimAtPath(GROUND)).GetOrderedXformOps()[0].Set(Gf.Vec3d(0, 0, self.z - 0.05))
        self.set_base_pose()
        log(f"dofs={r.dof_names}")
        log(f"{len(self.products)} products in scene, floor z={self.z:.3f}")

    def floor_z(self, x: float, y: float) -> float:
        try:
            import omni.physx

            hit = omni.physx.get_physx_scene_query_interface().raycast_closest(
                carb.Float3(x, y, 1.0), carb.Float3(0, 0, -1), 3.0
            )
            if hit["hit"] and ROBOT_PATH not in hit["rigidBody"] and -0.5 < hit["position"][2] < 0.5:
                return float(hit["position"][2])
        except Exception as e:  # noqa: BLE001
            log(f"floor raycast failed ({e}); assuming z=0")
        return 0.0

    def set_base_pose(self):
        self.robot.set_world_poses(positions=[[self.x, self.y, self.z + 0.005]], orientations=[yaw_quat(self.yaw)])

    def step(self, dt: float):
        vx, vy, wz = self.cmd
        steer, wheel_vel = [], []
        for i, (sx, sy) in STEER_XY.items():
            wx, wy = vx - wz * sy, vy + wz * sx
            speed = math.hypot(wx, wy)
            ang = math.atan2(wy, wx) if speed > 1e-4 else 0.0
            if ang > math.pi / 2:
                ang, speed = ang - math.pi, -speed
            elif ang < -math.pi / 2:
                ang, speed = ang + math.pi, -speed
            steer.append(ang)
            wheel_vel.append(WHEEL_SIGN[i] * speed / WHEEL_R)

        closed = FINGER_CLOSED if self.finger_target is None else self.finger_target
        fingers = [-FINGER_OPEN, FINGER_OPEN] if self.gripper_open else [-closed, closed]
        self.robot.set_dof_position_targets(
            [steer + self.arm + fingers], dof_indices=self.steer_idx + self.arm_idx + self.finger_idx
        )
        self.robot.set_dof_velocity_targets([wheel_vel], dof_indices=self.wheel_idx)

        if args.drive == "kinematic":
            c, s = math.cos(self.yaw), math.sin(self.yaw)
            self.x += (c * vx - s * vy) * dt
            self.y += (s * vx + c * vy) * dt
            self.yaw += wz * dt
            self.set_base_pose()
            self.robot.set_velocities(linear_velocities=[[0.0, 0.0, 0.0]], angular_velocities=[[0.0, 0.0, 0.0]])
        self.follow_held()
        self.follow_cargo()

    # --- grasping (attach-on-contact) ----------------------------------------------------------------------------
    # Products start kinematic. On grab the product turns dynamic with gravity off and its physics body is teleported
    # onto the gripper every frame through the tensor API (USD xform edits do not move PhysX bodies here, and a fixed
    # joint to an articulation link forces an articulation rebuild that stalled the sim). Release = gravity back on.
    def gripper_pos(self) -> np.ndarray:
        return world_pos(self.link6)  # DISCOVERSE's "endpoint" site is the link6 origin

    def grab(self, max_dist: float = 0.3, target=None, keep_offset: bool = False) -> bool:
        """Attach the nearest product (or `target`) within max_dist. keep_offset=True (the --demo) keeps it exactly
        where it is relative to the gripper instead of snapping its origin onto the gripper."""
        if self.held:
            return False
        g = self.gripper_pos()
        cands = self.products if target is None else [target]
        best = min(cands, key=lambda e: np.linalg.norm(world_pos(e) - g), default=None)
        if best is None or np.linalg.norm(world_pos(best) - g) > max_dist:
            log(f"nothing within {max_dist * 100:.0f} cm of the gripper")
            return False
        if best.GetAttribute("physics:kinematicEnabled").Get():
            best.GetAttribute("physics:kinematicEnabled").Set(False)
            simulation_app.update()  # let PhysX apply the kinematic -> dynamic switch before wrapping it
        body = RigidPrim(str(best.GetPath()))
        body.set_enabled_gravities([False])
        # keep the product's current orientation relative to the gripper; snap its origin onto the gripper
        rel = (
            UsdGeom.Xformable(best).ComputeLocalToWorldTransform(0)
            * UsdGeom.Xformable(self.link6).ComputeLocalToWorldTransform(0).GetInverse()
        )
        if not keep_offset:
            rel.SetTranslateOnly(Gf.Vec3d(0, 0, 0))
        self.held, self.held_rel, self.held_body = best, rel, body
        self.gripper_open = False
        log(f"grabbed {best.GetParent().GetName()}")
        return True

    def follow_held(self):
        if not self.held:
            return
        m = self.held_rel * UsdGeom.Xformable(self.link6).ComputeLocalToWorldTransform(0)
        t, q = m.ExtractTranslation(), m.ExtractRotationQuat()
        self.held_body.set_world_poses(positions=[[t[0], t[1], t[2]]], orientations=[[q.GetReal(), *q.GetImaginary()]])
        self.held_body.set_velocities(linear_velocities=[[0.0, 0.0, 0.0]], angular_velocities=[[0.0, 0.0, 0.0]])

    def release(self):
        if self.held:
            self.held_body.set_enabled_gravities([True])
            # re-enabling gravity does not wake a sleeping body; a small downward velocity does
            self.held_body.set_velocities(linear_velocities=[[0.0, 0.0, -0.2]], angular_velocities=[[0.0, 0.0, 0.0]])
            log(f"released {self.held.GetParent().GetName()}")
        self.held = self.held_body = None
        self.gripper_open = True

    # --- basket cargo (--demo) -----------------------------------------------------------------------------------
    # Once a released product has settled in the basket it rides with the base: the kinematic base is teleported
    # every step, which friction can't follow, so settled items are re-posed with it (gravity off) like held ones.
    def base_matrix(self) -> Gf.Matrix4d:
        m = Gf.Matrix4d(1.0)
        m.SetRotateOnly(Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(self.yaw)))
        m.SetTranslateOnly(Gf.Vec3d(self.x, self.y, self.z))
        return m

    def stow(self, entity) -> None:
        body = RigidPrim(str(entity.GetPath()))
        body.set_enabled_gravities([False])
        rel = UsdGeom.Xformable(entity).ComputeLocalToWorldTransform(0) * self.base_matrix().GetInverse()
        self.cargo.append((entity, body, rel))

    def unstow(self, entity) -> None:
        self.cargo = [c for c in self.cargo if c[0] != entity]

    def follow_cargo(self):
        if not self.cargo:
            return
        base = self.base_matrix()
        for _, body, rel in self.cargo:
            m = rel * base
            t, q = m.ExtractTranslation(), m.ExtractRotationQuat()
            body.set_world_poses(positions=[[t[0], t[1], t[2]]], orientations=[[q.GetReal(), *q.GetImaginary()]])
            body.set_velocities(linear_velocities=[[0.0, 0.0, 0.0]], angular_velocities=[[0.0, 0.0, 0.0]])


def setup_products(products, center: np.ndarray, radius: float) -> int:
    """Before play: every product becomes a rigid body. Those within `radius` are dynamic; the rest are kinematic,
    so they stay put on the shelves but can be switched to dynamic at runtime (PhysX does not pick up a
    static -> rigid-body change once the simulation is running, so they must be bodies from the start)."""
    n = 0
    for ent in products:
        make_dynamic(ent)
        dynamic = radius < 0 or (radius > 0 and np.linalg.norm(world_pos(ent)[:2] - center[:2]) < radius)
        ent.GetAttribute("physics:kinematicEnabled").Set(not dynamic)
        n += dynamic
    return n


GROUND = "/World/picky_ground"


def add_ground(stage, z: float = 0.0):
    """Invisible static ground slab. The Genie Sim market floor (part_01, triangle mesh) answers raycasts but lets
    dynamic bodies fall through (verified: a dropped cube ended at z=-20), so released products need this."""
    cube = UsdGeom.Cube.Define(stage, GROUND)
    xf = UsdGeom.Xformable(cube)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, z - 0.05))
    xf.AddScaleOp().Set(Gf.Vec3f(50.0, 50.0, 0.05))  # cube size 2 -> 100 x 100 x 0.1 m, top face at z
    cube.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())


ARM_LINKS = [
    "arm_link1",
    "arm_link2",
    "arm_link3",
    "arm_link4",
    "arm_link5",
    "arm_link6",
    "finger_left_link",
    "finger_right_link",
]


def add_collision_groups(stage):
    """Arm links don't collide with the store (shelves + products): the arm reaches into packed shelves without
    snagging, and grabbing is attach-based anyway. Chassis and basket still collide, so products land in the basket."""

    def group(path, members):
        g = UsdPhysics.CollisionGroup.Define(stage, path)
        col = Usd.CollectionAPI.Apply(g.GetPrim(), "colliders")
        for m in members:
            col.CreateIncludesRel().AddTarget(m)
        return g

    arm = [
        str(p.GetPath())
        for p in stage.Traverse()
        if p.GetName() in ARM_LINKS and str(p.GetPath()).startswith(ROBOT_PATH)  # also /World/Picky_<name> (--demo)
    ]
    g_arm = group("/World/collision_groups/arm", arm)
    group("/World/collision_groups/store", ["/World/background"])
    g_arm.CreateFilteredGroupsRel().AddTarget("/World/collision_groups/store")
    log(f"collision groups: {len(arm)} arm links filtered against /World/background")


def add_chase_camera(stage, robot_path: str = ROBOT_PATH):
    cam = UsdGeom.Camera.Define(stage, f"{robot_path}/chase_cam")
    xf = UsdGeom.Xformable(cam)
    xf.ClearXformOpOrder()
    # behind and above the robot, looking forward and down (camera looks down its -Z, +Y up)
    xf.AddTranslateOp().Set(Gf.Vec3d(-1.8, 0.0, 1.9))
    xf.AddRotateXYZOp().Set(Gf.Vec3f(65.0, 0.0, -90.0))
    cam.CreateFocalLengthAttr(14.0)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.05, 200.0))


def ensure_physics_and_light(stage):
    if not any(p.IsA(UsdPhysics.Scene) for p in stage.Traverse()):
        scene = UsdPhysics.Scene.Define(stage, "/World/physicsScene")
        scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
        scene.CreateGravityMagnitudeAttr(9.81)
    if not any(p.IsA(UsdLux.DomeLight) or p.IsA(UsdLux.DistantLight) for p in stage.Traverse()):
        UsdLux.DomeLight.Define(stage, "/World/picky_dome").CreateIntensityAttr(1500)


def open_scene(path: str):
    ctx = omni.usd.get_context()
    ctx.open_stage(path)
    for _ in range(600):  # payloads (products) load asynchronously
        simulation_app.update()
        if ctx.get_stage_loading_status()[2] == 0 and ctx.get_stage() is not None:
            break
    return ctx.get_stage()


def viewport_api():
    try:
        from omni.kit.viewport.utility import get_active_viewport

        return get_active_viewport()
    except Exception:  # noqa: BLE001
        return None


def screenshot(viewport, path: str):
    from omni.kit.viewport.utility import capture_viewport_to_file

    capture_viewport_to_file(viewport, file_path=path)
    for _ in range(10):
        simulation_app.update()
    log(f"screenshot {path}")


def run_smoke(picky: Picky, viewport, step):
    """Scripted self-test: settle, drive, arm presets, grab + basket drop. Prints numbers, saves screenshots."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    viewport.camera_path = CHASE_CAM

    def run(seconds: float):
        for _ in range(int(seconds * 60)):
            step()

    probe = UsdGeom.Cube.Define(picky.stage, "/World/smoke_probe")
    probe.CreateSizeAttr(0.08)
    UsdGeom.Xformable(probe).AddTranslateOp().Set(Gf.Vec3d(picky.x, picky.y - 1.2, picky.z + 0.6))
    UsdPhysics.RigidBodyAPI.Apply(probe.GetPrim())
    UsdPhysics.CollisionAPI.Apply(probe.GetPrim())
    dyn = [e for e in picky.products if not e.GetAttribute("physics:kinematicEnabled").Get()]
    z_before = {str(e.GetPath()): world_pos(e)[2] for e in dyn}
    run(2.0)
    fallen = sum(1 for e in dyn if world_pos(e)[2] < z_before[str(e.GetPath())] - 0.1)
    log(f"dynamic products: {len(dyn)}, fell >10 cm during settle: {fallen}")
    log(f"ground probe cube z={world_pos(probe.GetPrim())[2]:.3f} (expect ~{picky.z + 0.04:.3f})")
    p0 = np.array([picky.x, picky.y])
    root = world_pos(picky.stage.GetPrimAtPath(picky.root_path))
    arm_q = picky.robot.get_dof_positions().numpy()[0][picky.arm_idx]
    log(f"settled: root={np.round(root, 3)} arm dofs={np.round(arm_q, 2)}")
    screenshot(viewport, str(out / "01_spawn.png"))

    picky.cmd = np.array([MAX_V, 0.0, 0.0])
    run(0.6)
    picky.cmd = np.zeros(3)
    run(0.5)
    log(f"drove {np.linalg.norm(np.array([picky.x, picky.y]) - p0):.2f} m forward")
    screenshot(viewport, str(out / "02_drove.png"))

    for name in ("front", "basket", "home"):
        picky.arm = list(POSES[name])
        run(2.5)
        q = picky.robot.get_dof_positions().numpy()[0][picky.arm_idx]
        log(
            f"pose {name}: tracking error {np.abs(q - np.array(POSES[name])).max():.3f} rad, "
            f"gripper at {np.round(picky.gripper_pos(), 3)}"
        )
        screenshot(viewport, str(out / f"03_pose_{name}.png"))

    # grab test: teleport the nearest product next to the gripper in the front pose, grab, move to basket, release
    picky.arm = list(POSES["front"])
    run(2.0)
    g = picky.gripper_pos()
    target = min(picky.products, key=lambda e: np.linalg.norm(world_pos(e) - g))
    log(f"nearest product {target.GetParent().GetName()} at {np.linalg.norm(world_pos(target) - g):.2f} m")
    if picky.grab(max_dist=10.0):
        run(1.0)
        log(f"held product at {np.round(world_pos(target), 3)}, gripper at {np.round(picky.gripper_pos(), 3)}")
        picky.arm = list(POSES["basket"])
        run(3.0)
        screenshot(viewport, str(out / "04_carry.png"))
        picky.release()
        run(2.0)
        p = world_pos(target)
        local = np.array(p) - np.array([picky.x, picky.y, picky.z])
        c, s = math.cos(-picky.yaw), math.sin(-picky.yaw)
        bx, by = c * local[0] - s * local[1], s * local[0] + c * local[1]
        in_basket = -0.30 < bx < 0.04 and -0.11 < by < 0.19 and 0.80 < local[2] < 1.1
        log(f"product after release: base-frame ({bx:.2f}, {by:.2f}, {local[2]:.2f}) in_basket={in_basket}")
        screenshot(viewport, str(out / "05_released.png"))
    log("SMOKE_DONE")


def main():
    if args.demo and args.demo_robot == "so101":
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from demo import so101_demo  # noqa: PLC0415

        try:
            so101_demo.run(simulation_app, args, sys.modules[__name__])
        except BaseException:  # print it: close() below exits the process before Python would
            import traceback  # noqa: PLC0415

            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            raise
        finally:
            simulation_app.close()
        return
    stage = open_scene(args.scene)
    if stage is None:
        raise SystemExit(f"could not open {args.scene}")
    ensure_physics_and_light(stage)
    robot_ref = stage.DefinePrim(ROBOT_PATH, "Xform")
    robot_ref.GetReferences().AddReference(str(Path(args.robot).resolve()))
    add_chase_camera(stage)
    add_ground(stage)

    if args.demo:  # extra rovers + checkout counter + store map, all before physics starts
        import json
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from demo import autopilot  # noqa: PLC0415
        from demo.store_map import StoreMap  # noqa: PLC0415

        cfg = json.loads(Path(args.demo_config).read_text())
        rcfg = cfg["robots"][: args.robots]
        paths = [ROBOT_PATH] + [f"{ROBOT_PATH}_{r['name']}" for r in rcfg[1:]]
        for path in paths[1:]:
            stage.DefinePrim(path, "Xform").GetReferences().AddReference(str(Path(args.robot).resolve()))
            add_chase_camera(stage, path)
        extra = autopilot.add_checkout(stage, cfg["checkout"])
        smap = StoreMap(stage, extra_fixtures=extra)
        log(f"store map: {len(smap.products)} products, {len(smap.fixtures)} fixtures, {len(smap.sections)} sections")
        products = product_entities(stage)
        pickys = [Picky(stage, path, r["spawn"], products) for path, r in zip(paths, rcfg, strict=False)]
    else:
        pickys = [Picky(stage)]
    picky = pickys[0]
    add_collision_groups(stage)
    n_dyn = setup_products(picky.products, np.array(args.spawn), args.dynamic_radius)
    log(f"{n_dyn} products made dynamic")

    timeline = omni.timeline.get_timeline_interface()
    timeline.play()
    for _ in range(5):
        simulation_app.update()
    for p in pickys:
        p.post_play()

    if args.demo:

        def step_all():
            for p in pickys:
                p.step(1 / 60)
            simulation_app.update()

        for _ in range(60):  # settle
            step_all()
        autopilot.run_demo(
            simulation_app, stage, pickys, [r["name"] for r in rcfg], smap, cfg, args, step_all, world_pos
        )
        simulation_app.close()
        return

    viewport = viewport_api()
    last = [time.perf_counter()]

    def step():
        now = time.perf_counter()
        dt = min(now - last[0], 1 / 20) if not args.smoke else 1 / 60
        last[0] = now
        picky.step(dt)
        simulation_app.update()

    if args.smoke:
        run_smoke(picky, viewport, step)
        simulation_app.close()
        return

    free_cam = viewport.camera_path if viewport else None
    held: set[str] = set()
    drive_keys = {"I": (1, 0, 0), "K": (-1, 0, 0), "U": (0, 1, 0), "O": (0, -1, 0), "J": (0, 0, 1), "L": (0, 0, -1)}

    def update_cmd():
        v = np.zeros(3)
        for k in held:
            v += np.array(drive_keys[k], dtype=float)
        picky.cmd = v * np.array([MAX_V, MAX_V * 0.6, MAX_W])

    def on_key(event, *_):
        name = event.input.name.replace("KEY_", "")
        etype = carb.input.KeyboardEventType
        if event.type == etype.KEY_PRESS:
            if name in drive_keys:
                held.add(name)
                update_cmd()
            elif name == "SPACE":
                held.clear()
                update_cmd()
            elif len(name) == 1 and name in "123456":
                picky.sel = int(name) - 1
                log(f"selected {ARM_JOINTS[picky.sel]}")
            elif name in ("UP", "DOWN"):
                picky.arm[picky.sel] += ARM_STEP * (1 if name == "UP" else -1)
            elif name in ("H", "N", "B"):
                picky.arm = list(POSES[{"H": "home", "N": "front", "B": "basket"}[name]])
            elif name == "G":
                picky.gripper_open = not picky.gripper_open
            elif name == "P":
                picky.grab()
            elif name == "R":
                picky.release()
            elif name == "V" and viewport:
                viewport.camera_path = free_cam if str(viewport.camera_path) == CHASE_CAM else CHASE_CAM
        elif event.type == etype.KEY_REPEAT and name in ("UP", "DOWN"):
            picky.arm[picky.sel] += ARM_STEP * (1 if name == "UP" else -1)
        elif event.type == etype.KEY_RELEASE and name in drive_keys:
            held.discard(name)
            update_cmd()
        return True

    sub = None
    if viewport:
        appwindow = omni.appwindow.get_default_app_window()
        sub = carb.input.acquire_input_interface().subscribe_to_keyboard_events(appwindow.get_keyboard(), on_key)  # noqa: F841
        viewport.camera_path = CHASE_CAM
    log("running — I/K/J/L/U/O drive, H/N/B arm poses, P grab, R release, V camera")
    while simulation_app.is_running():
        step()
    simulation_app.close()


if __name__ == "__main__":
    main()
