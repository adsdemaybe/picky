"""Build Picky's shopping robot USD (robot v3): PAL PMB2 round base + short fixed riser + LONG-REACH Hiwonder
SO-ARM101 (SO-101) + circular wire-mesh basket + cameras, as ONE PhysX articulation for Isaac Sim 6.x.

Design (robot frame: +x forward, +y left, +z up, origin on the floor under the base centre):
  * Base: PAL Robotics PMB2 (the TIAGo base; pal-robotics/pmb2_robot `pmb2_description`, Apache-2.0): its body shell,
    bumper ring, docking contacts, both drive wheels and four casters, exact poses from the xacros. Round, 0.54 m
    diameter (ring), differential drive (wheel separation 0.4044 m, wheel radius 0.0985 m). Recoloured white + black
    with a thin green accent line so the base, riser, arm and basket read as one product.
  * Riser: a short FIXED white column (no lift, no prismatic joint) at x = +0.175 m on the base top, black flanges,
    a head camera on its front and a basket camera on its back. The SO-101 base sits on the top flange.
  * Arm: the SO-101 follower exactly as TheRobotStudio/SO-ARM100 `so101_new_calib_camera.urdf` (Apache-2.0): every STL
    and joint axis/limit, coloured like the Hiwonder SO-ARM101 follower kit (white PLA + black servos). LONG REACH:
    the stock upper arm and forearm are cut at a plain section (x = -0.072 / -0.058 m in the link frames) and lengthened
    by DL1 / DL2 with twin 16 mm carbon-fibre tubes between printed white couplers (the usual hobbyist SO-100/101
    extension); the elbow_flex / wrist_flex joint origins move by the same amounts, so the URDF chain, the USD joints
    and the IK all agree. Motors (see README / kin.json "actuators"): shoulder_pan STS3250, shoulder_lift STS3250 + 3:1
    printed cycloidal stage, elbow_flex STS3250 + 2:1, wrist/roll/gripper stock STS3215-class (Hiwonder HX-30HM, 30
    kg*cm). The reducers are the black drums on the pivot side of those two joints.
    The arm faces the robot's right at pan 0, so pan -90 deg reaches FORWARD (shelves) and +90 deg reaches BACK into the
    basket (SO-101 pan is limited to +-110 deg).
  * Basket: a CIRCULAR, slightly tapered wire-mesh basket (shopping-cart style: 40 vertical wires, 4 rings, a heavier
    rolled rim with a black trim, a wire grid on a solid floor panel) sitting in a black collar on the rear half of the
    base, its back following the base's curve. Powder-coat white wire, black trim. Invisible box colliders (floor + 16
    wall segments) on base_link, so released products land in it and ride along.
  * Cameras: cam_head (riser front, 20 deg down), cam_front (base front), cam_wrist (SO-101 wrist camera lens),
    cam_basket (riser back, looking into the basket), cam_chase (behind/above, for videos).

Rendering robustness (the rm2 robot lost parts at some angles): every STL is cleaned (merged vertices, degenerate faces
removed), its winding made consistent and outward (per connected body, signed volume), normals are authored with a
35-degree crease angle, `doubleSided = true`, `subdivisionScheme = none`, UsdPreviewSurface materials, no LODs.

Physics: base = 3 virtual planar joints (base_x, base_y prismatic + base_yaw revolute) from a world anchor (drive it
like a differential drive: yaw + forward speed; `joints/world_fix` sets the spawn pose) or `--base floating`. Arm links
have gravity ON; every arm joint has a position drive whose max torque is the actuator's stall torque at the joint
and whose stiffness saturates that torque at ~1.7 deg of error (a stiff bus servo), damping for a critically damped
joint at the reference pose (link inertia + reflected rotor inertia as armature), max joint speed 1.0-1.5 rad/s.

Also writes <out>.kin.json: the kinematic chain (for so101_ik.py), actuators + torque budget, jaw sample points (grasp
contact check), basket interior, wheel layout, camera paths, named poses.

    python build_so101_rover.py --so101 SO-ARM100/Simulation/SO101 --pmb2 pmb2_robot/pmb2_description --out picky.usd
Needs numpy, trimesh, scipy, networkx, shapely, mapbox_earcut, rtree, fast-simplification, usd-core (25.x, as Isaac 6).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import box as sbox

sys.path.insert(0, str(Path(__file__).parent))
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt  # noqa: E402
from urdf_tree import Urdf, axis_angle, rpy_matrix  # noqa: E402

ROOT = "/Picky"
DEG = 180.0 / math.pi
G = 9.81

# --- layout
# PMB2 (pmb2_description/urdf/base/base.urdf.xacro + wheels/*.xacro + objects/cover.urdf.xacro)
PMB2_Z = 0.0985  # base_link above the floor (= wheel radius, base_footprint_joint)
PMB2_TOP = 0.198  # top of base.stl in base_link
WHEEL_R, WHEEL_SEP = 0.0985, 0.4044
CASTER_SEP_X, CASTER_SEP_Y, CASTER_OFF = 0.343, 0.204, (-0.002, 0.0, -0.0335)
RING_Z = 0.13  # cover_joint: bumper ring above base_footprint
BASE_R = 0.269  # ring outer radius (footprint radius)
BASE_TOP = PMB2_Z + PMB2_TOP  # 0.2965 m

X_PAN = 0.175  # SO-101 pan axis (robot frame), on the riser axis
RISER_R = 0.045
BASKET_C = (-0.070, 0.0)
BASKET_R_BOT, BASKET_R_TOP, BASKET_H = 0.160, 0.185, 0.200
COLLAR_H = 0.016
N_WIRES = 40

# long-reach SO-101 (see README "Long-reach SO-101" for the reach + torque study)
CUT1, CUT2 = (
    -0.072,
    -0.058,
)  # cut planes (link frame x) in upper_arm_so101_v1 / under_arm_so101_v1: plain solid sections
TUBE_R, TUBE_RI = 0.008, 0.007  # 16/14 mm pultruded carbon-fibre tubes
COUPLER_L = 0.014

# actuators: name, gear ratio, stall torque at the servo (N*m, 12 V), no-load speed (rad/s), mass (kg), rotor armature
STS3215 = {"servo": "Feetech STS3215 / Hiwonder HX-30HM (stock SO-101 follower)", "stall": 2.94, "w0": 4.71,
           "rated": 0.98, "mass": 0.055}  # fmt: skip  # 30 kg*cm, 0.222 s/60 deg @12 V
STS3250 = {"servo": "Feetech STS3250 (same 45x25x35 mm case)", "stall": 4.90, "w0": 7.87, "rated": 1.57,
           "mass": 0.0745}  # fmt: skip  # 50 kg*cm, 0.133 s/60 deg @12 V, rated 16 kg*cm
ACTUATORS = {  # joint: (servo, ratio, efficiency, sim speed limit rad/s)
    "shoulder_pan": (STS3250, 1, 1.0, 1.2),
    "shoulder_lift": (STS3250, 3, 0.9, 1.0),
    "elbow_flex": (STS3250, 2, 0.9, 1.3),
    "wrist_flex": (STS3215, 1, 1.0, 1.5),
    "wrist_roll": (STS3215, 1, 1.0, 1.5),
    "gripper": (STS3215, 1, 1.0, 1.5),
}
ROTOR_ARMATURE = 0.028  # kg*m^2 reflected through the servo's own 1:345 gear train (LeIsaac SO-101 value)
SAT_ERR = 0.03  # rad of position error that saturates the drive (stiff bus-servo position loop)
PAYLOAD = 0.5  # kg grocery item for the torque budget

MATS = {  # name: (rgb, roughness, metallic)
    "pla_white": ((0.88, 0.88, 0.86), 0.55, 0.0),
    "shell_white": ((0.90, 0.90, 0.89), 0.35, 0.0),
    "servo_black": ((0.035, 0.035, 0.04), 0.38, 0.0),
    "trim_black": ((0.03, 0.03, 0.035), 0.45, 0.0),
    "carbon": ((0.06, 0.06, 0.065), 0.22, 0.35),
    "alu_black": ((0.06, 0.06, 0.07), 0.40, 0.6),
    "accent": ((0.05, 0.62, 0.36), 0.40, 0.0),
    "wire_white": ((0.86, 0.87, 0.87), 0.30, 0.15),
    "rubber": ((0.02, 0.02, 0.02), 0.85, 0.0),
    "hub_grey": ((0.35, 0.36, 0.38), 0.45, 0.4),
    "camera_black": ((0.02, 0.02, 0.025), 0.30, 0.0),
    "glass": ((0.01, 0.01, 0.02), 0.05, 0.0),
    "steel": ((0.62, 0.63, 0.65), 0.25, 1.0),
}


# --- math utils
def T_from(R=None, p=(0, 0, 0)) -> np.ndarray:
    T = np.eye(4)
    if R is not None:
        T[:3, :3] = R
    T[:3, 3] = p
    return T


def Rz(a):
    return rpy_matrix(0, 0, a)


def Rx(a):
    return rpy_matrix(a, 0, 0)


def Ry(a):
    return rpy_matrix(0, a, 0)


def gf_matrix(T: np.ndarray) -> Gf.Matrix4d:
    return Gf.Matrix4d(T.T.tolist())  # USD is row-vector


def gf_quat(R: np.ndarray) -> Gf.Quatf:
    q = Gf.Matrix3d(R.T.tolist()).ExtractRotation().GetQuat()
    return Gf.Quatf(float(q.GetReal()), *[float(v) for v in q.GetImaginary()])


def rot_x_to(axis) -> np.ndarray:
    """Rotation mapping +X onto `axis` (USD joints only take X/Y/Z axes)."""
    a = np.asarray(axis, float)
    a /= np.linalg.norm(a)
    x = np.array([1.0, 0, 0])
    v = np.cross(x, a)
    c = float(np.dot(x, a))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else Rz(math.pi)
    return axis_angle(v / np.linalg.norm(v), math.acos(max(-1.0, min(1.0, c))))[:3, :3]


def prim_name(s: str) -> str:
    import re

    n = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")
    return n if n[:1].isalpha() else "p_" + n


# --- mesh utils
def clean(m: trimesh.Trimesh, max_faces: int | None = None) -> trimesh.Trimesh:
    """Merge, drop degenerate faces, orient every connected body outward, optional decimation."""
    m = trimesh.Trimesh(m.vertices, m.faces, process=True)
    m.update_faces(m.nondegenerate_faces())
    m.remove_unreferenced_vertices()
    if max_faces and len(m.faces) > max_faces:
        import fast_simplification

        v, f = fast_simplification.simplify(
            m.vertices.astype(np.float32), m.faces.astype(np.int32), target_reduction=1 - max_faces / len(m.faces)
        )
        m = trimesh.Trimesh(v, f, process=True)
    parts = m.split(only_watertight=False) if m.body_count > 1 else [m]
    out = []
    for p in parts:
        trimesh.repair.fix_winding(p)  # consistent winding inside the body
        if p.is_watertight:
            if p.volume < 0:
                p.invert()
        else:  # open shell: orient by the majority of face normals pointing away from the body centre
            d = p.triangles_center - p.centroid
            if np.sum(np.einsum("ij,ij->i", p.face_normals, d)) < 0:
                p.invert()
        out.append(p)
    return trimesh.util.concatenate(out) if len(out) > 1 else out[0]


def shaded(m: trimesh.Trimesh, crease_deg: float = 35.0):
    """Vertices split along creases -> (points, faces, vertex normals) for smooth-but-crisp CAD shading."""
    s = trimesh.graph.smooth_shade(m, angle=math.radians(crease_deg))
    return np.asarray(s.vertices), np.asarray(s.faces), np.asarray(s.vertex_normals)


def load_stl(path: Path, scale=(1, 1, 1), max_faces=None) -> trimesh.Trimesh:
    m = trimesh.load(str(path), force="mesh")
    m.apply_scale(scale) if np.allclose(scale, scale[0]) else m.apply_transform(np.diag([*scale, 1.0]))
    return clean(m, max_faces)


def boxm(size, center=(0, 0, 0)) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=size, transform=T_from(p=center))


def cyl(r, h, center=(0, 0, 0), axis="z", sections=32) -> trimesh.Trimesh:
    m = trimesh.creation.cylinder(radius=r, height=h, sections=sections)
    R = {"z": np.eye(3), "x": Ry(math.pi / 2), "y": Rx(math.pi / 2)}[axis]
    m.apply_transform(T_from(R, center))
    return m


def tube(r_out, r_in, h, center=(0, 0, 0), sections=48) -> trimesh.Trimesh:
    """annulus along z"""
    m = trimesh.creation.annulus(r_min=r_in, r_max=r_out, height=h, sections=sections)
    m.apply_translation(center)
    return m


def segment(p0, p1, r, sections=8) -> trimesh.Trimesh:
    """cylinder (wire) from p0 to p1"""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    return trimesh.creation.cylinder(radius=r, segment=np.array([p0, p1]), sections=sections)


def ring(R, r, z, center=(0, 0), n_major=96, n_minor=10) -> trimesh.Trimesh:
    """torus of major radius R and wire radius r in the plane z (a wire ring)"""
    u = np.linspace(0, 2 * math.pi, n_major, endpoint=False)
    v = np.linspace(0, 2 * math.pi, n_minor, endpoint=False)
    U, V = np.meshgrid(u, v, indexing="ij")
    X = (R + r * np.cos(V)) * np.cos(U) + center[0]
    Y = (R + r * np.cos(V)) * np.sin(U) + center[1]
    Z = r * np.sin(V) + z
    verts = np.c_[X.ravel(), Y.ravel(), Z.ravel()]
    F = []
    for i in range(n_major):
        for j in range(n_minor):
            a = i * n_minor + j
            b = ((i + 1) % n_major) * n_minor + j
            c = ((i + 1) % n_major) * n_minor + (j + 1) % n_minor
            d = i * n_minor + (j + 1) % n_minor
            F += [[a, b, c], [a, c, d]]
    m = trimesh.Trimesh(verts, np.array(F), process=True)
    if m.volume < 0:
        m.invert()
    return m


def rounded_rect_prism(lo, hi, axis_lo, axis_hi, radius=0.004):
    """box in (y, z) section lo..hi with rounded edges, extruded along x from axis_lo to axis_hi (link frame)."""
    poly = sbox(lo[0], lo[1], hi[0], hi[1]).buffer(-radius).buffer(radius, resolution=6)
    m = trimesh.creation.extrude_polygon(poly, axis_hi - axis_lo)  # polygon in (y, z), extruded along +"x"
    # extrude_polygon builds in xy and extrudes along z -> map (x, y, z) -> (z, x, y) i.e. section y->y, z->z
    M = np.array([[0, 0, 1, axis_lo], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float)
    m.apply_transform(M)
    return m


# --- USD author
class Builder:
    def __init__(self, out: Path):
        self.out = out
        self.stage = Usd.Stage.CreateNew(str(out))
        UsdGeom.SetStageUpAxis(self.stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(self.stage, 1.0)
        UsdPhysics.SetStageKilogramsPerUnit(self.stage, 1.0)
        root = UsdGeom.Xform.Define(self.stage, ROOT)
        self.stage.SetDefaultPrim(root.GetPrim())
        UsdGeom.Scope.Define(self.stage, f"{ROOT}/Looks")
        UsdGeom.Scope.Define(self.stage, f"{ROOT}/joints")
        self.mats = {}
        for name, (rgb, rough, metal) in MATS.items():
            mat = UsdShade.Material.Define(self.stage, f"{ROOT}/Looks/{name}")
            sh = UsdShade.Shader.Define(self.stage, f"{ROOT}/Looks/{name}/surface")
            sh.CreateIdAttr("UsdPreviewSurface")
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
            sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
            sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)
            sh.CreateInput("specular", Sdf.ValueTypeNames.Float).Set(0.5)
            mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
            self.mats[name] = (mat, rgb)
        self.links: dict[str, np.ndarray] = {}  # link -> pose in robot frame (at all joints = 0)
        self.n_mesh = 0
        self.n_faces = 0

    # links ----------------------------------------------------------------------------------------------------------
    def link(self, name: str, T: np.ndarray, mass: float, com=None, inertia=None, gravity=True):
        x = UsdGeom.Xform.Define(self.stage, f"{ROOT}/{name}")
        x.AddTransformOp().Set(gf_matrix(T))
        p = x.GetPrim()
        UsdPhysics.RigidBodyAPI.Apply(p)
        ma = UsdPhysics.MassAPI.Apply(p)
        ma.CreateMassAttr(float(mass))
        if com is not None:
            ma.CreateCenterOfMassAttr(Gf.Vec3f(*[float(v) for v in com]))
        if inertia is not None:
            Iner = np.array(inertia, float)
            if Iner.shape == (6,):
                Iner = np.array([[Iner[0], Iner[3], Iner[4]], [Iner[3], Iner[1], Iner[5]], [Iner[4], Iner[5], Iner[2]]])
            w, V = np.linalg.eigh(Iner)
            if np.linalg.det(V) < 0:
                V[:, 0] *= -1
            ma.CreateDiagonalInertiaAttr(Gf.Vec3f(*[float(max(v, 1e-7)) for v in w]))
            ma.CreatePrincipalAxesAttr(gf_quat(V))
        else:
            d = max(1e-5, 0.4 * mass * 0.05**2)
            ma.CreateDiagonalInertiaAttr(Gf.Vec3f(d, d, d))
        p.AddAppliedSchema("PhysxRigidBodyAPI")
        if not gravity:
            p.CreateAttribute("physxRigidBody:disableGravity", Sdf.ValueTypeNames.Bool).Set(True)
        p.CreateAttribute("physxRigidBody:sleepThreshold", Sdf.ValueTypeNames.Float).Set(0.0)
        self.links[name] = T
        return p

    def mesh(self, path: str, m: trimesh.Trimesh, mat: str, T_local: np.ndarray | None = None, crease=35.0):
        V, F, N = shaded(m, crease)
        g = UsdGeom.Mesh.Define(self.stage, path)
        g.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(V.astype(np.float32)))
        g.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(F), 3, dtype=np.int32)))
        g.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(F.astype(np.int32).ravel()))
        g.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(N.astype(np.float32)))
        g.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        g.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        g.CreateDoubleSidedAttr(True)
        g.CreateOrientationAttr(UsdGeom.Tokens.rightHanded)
        g.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*V.min(0).tolist()), Gf.Vec3f(*V.max(0).tolist())]))
        g.CreateDisplayColorAttr([Gf.Vec3f(*self.mats[mat][1])])
        if T_local is not None and not np.allclose(T_local, np.eye(4)):
            g.AddTransformOp().Set(gf_matrix(T_local))
        UsdShade.MaterialBindingAPI.Apply(g.GetPrim()).Bind(self.mats[mat][0])
        self.n_mesh += 1
        self.n_faces += len(F)
        return g

    def collider_box(self, path: str, center, size, R=None):
        c = UsdGeom.Cube.Define(self.stage, path)
        c.CreateSizeAttr(1.0)
        x = UsdGeom.Xformable(c)
        x.AddTranslateOp().Set(Gf.Vec3d(*[float(v) for v in center]))
        if R is not None:
            x.AddOrientOp().Set(gf_quat(R))
        x.AddScaleOp().Set(Gf.Vec3f(*[float(v) for v in size]))
        c.CreatePurposeAttr(UsdGeom.Tokens.guide)  # never rendered
        UsdPhysics.CollisionAPI.Apply(c.GetPrim())
        c.GetPrim().AddAppliedSchema("PhysxCollisionAPI")
        c.GetPrim().CreateAttribute("physxCollision:contactOffset", Sdf.ValueTypeNames.Float).Set(0.004)
        c.GetPrim().CreateAttribute("physxCollision:restOffset", Sdf.ValueTypeNames.Float).Set(0.0)
        return c

    # joints ---------------------------------------------------------------------------------------------------------
    def joint(self, kind, name, parent, child, T_joint, axis="X", lower=None, upper=None, drive=None, extra=None):
        """T_joint: joint frame in the robot frame at q = 0 (axis along its local `axis`). parent None = world."""
        J = {
            "revolute": UsdPhysics.RevoluteJoint,
            "prismatic": UsdPhysics.PrismaticJoint,
            "fixed": UsdPhysics.FixedJoint,
        }[kind]
        j = J.Define(self.stage, f"{ROOT}/joints/{name}")
        if parent:
            j.CreateBody0Rel().SetTargets([f"{ROOT}/{parent}"])
            L0 = np.linalg.inv(self.links[parent]) @ T_joint
        else:
            L0 = T_joint
        j.CreateBody1Rel().SetTargets([f"{ROOT}/{child}"])
        L1 = np.linalg.inv(self.links[child]) @ T_joint
        j.CreateLocalPos0Attr(Gf.Vec3f(*L0[:3, 3].tolist()))
        j.CreateLocalRot0Attr(gf_quat(L0[:3, :3]))
        j.CreateLocalPos1Attr(Gf.Vec3f(*L1[:3, 3].tolist()))
        j.CreateLocalRot1Attr(gf_quat(L1[:3, :3]))
        p = j.GetPrim()
        if kind != "fixed":
            j.CreateAxisAttr(axis)
            if lower is not None:
                j.CreateLowerLimitAttr(float(lower))
                j.CreateUpperLimitAttr(float(upper))
        if drive:
            dt = "angular" if kind == "revolute" else "linear"
            d = UsdPhysics.DriveAPI.Apply(p, dt)
            d.CreateTypeAttr("force")
            d.CreateStiffnessAttr(float(drive["kp"]))
            d.CreateDampingAttr(float(drive["kd"]))
            d.CreateMaxForceAttr(float(drive["fmax"]))
            d.CreateTargetPositionAttr(0.0)
            d.CreateTargetVelocityAttr(0.0)
        if extra:
            p.AddAppliedSchema("PhysxJointAPI")
        for k, (tn, v) in (extra or {}).items():
            p.CreateAttribute(k, tn).Set(v)
        return j


# --- long-reach SO-101
def split_link_meshes(meshes, cut: float, dL: float):
    """meshes: [(mesh in link frame, material, tag)]. Everything beyond the cut plane x = cut (towards -x) moves by -dL;
    meshes crossing the plane are sliced and capped. Returns the new list + the section box at the cut."""
    out, sec = [], None
    shift = T_from(p=(-dL, 0, 0))
    for m, mat, tag in meshes:
        lo, hi = m.bounds
        if lo[0] >= cut:
            out.append((m, mat, tag))
        elif hi[0] <= cut:
            mm = m.copy()
            mm.apply_transform(shift)
            out.append((mm, mat, tag))
        else:
            s = m.section(plane_origin=[cut, 0, 0], plane_normal=[1, 0, 0])
            v = s.vertices
            sec = (v[:, 1].min(), v[:, 2].min(), v[:, 1].max(), v[:, 2].max())
            near = clean(m.slice_plane([cut, 0, 0], [1, 0, 0], cap=True))
            far = clean(m.slice_plane([cut, 0, 0], [-1, 0, 0], cap=True))
            far.apply_transform(shift)
            out += [(near, mat, tag + "_near"), (far, mat, tag + "_far")]
    return out, sec


def extension_parts(cut: float, dL: float, sec):
    """printed couplers (white) over both cut faces + twin carbon tubes between them, in the link frame."""
    y0, z0, y1, z1 = sec
    m = 0.0025  # coupler wall over the section
    lo, hi = (y0 - m, z0 - m), (y1 + m, z1 + m)
    c_near = rounded_rect_prism(lo, hi, cut - COUPLER_L, cut + 0.004)
    c_far = rounded_rect_prism(lo, hi, cut - dL - 0.004, cut - dL + COUPLER_L)
    yc = 0.5 * (y0 + y1)
    zs = (z0 + 0.25 * (z1 - z0), z0 + 0.75 * (z1 - z0))
    tubes = []
    for z in zs:
        t = tube(TUBE_R, TUBE_RI, dL - 2 * COUPLER_L + 0.012, sections=40)
        t.apply_transform(T_from(Ry(math.pi / 2), (cut - dL / 2, yc, z)))
        tubes.append(t)
    # thin accent band on each coupler (one-product look)
    bands = [
        rounded_rect_prism(
            (lo[0] - 0.0004, lo[1] - 0.0004), (hi[0] + 0.0004, hi[1] + 0.0004), x - 0.0015, x + 0.0015, 0.004
        )
        for x in (cut - COUPLER_L + 0.004, cut - dL + COUPLER_L - 0.004)
    ]
    return [
        (c_near, "pla_white", "coupler_near"),
        (c_far, "pla_white", "coupler_far"),
        (trimesh.util.concatenate(tubes), "carbon", "cf_tubes"),
        (trimesh.util.concatenate(bands), "accent", "coupler_bands"),
    ], (len(zs), zs)


def mass_props(parts):
    """parts: [(mass, com(3), I_about_com(3x3) or None)] -> mass, com, inertia about com"""
    M = sum(p[0] for p in parts)
    c = sum(p[0] * np.asarray(p[1], float) for p in parts) / M
    Iner = np.zeros((3, 3))
    for m, p, Ic in parts:
        d = np.asarray(p, float) - c
        Iner += (Ic if Ic is not None else np.zeros((3, 3))) + m * (np.dot(d, d) * np.eye(3) - np.outer(d, d))
    return M, c, Iner


def I_from6(v):
    return np.array([[v[0], v[3], v[4]], [v[3], v[1], v[5]], [v[4], v[5], v[2]]])


def rod_I(m, L, r, axis=0):
    Iner = np.full(3, m * (3 * r * r + L * L) / 12.0)
    Iner[axis] = m * r * r
    return np.diag(Iner)


# --- the robot
def build(args):
    out = Path(args.out).resolve()
    so = Urdf(Path(args.so101) / "so101_new_calib_camera.urdf")
    pm = Path(args.pmb2)
    DL1, DL2 = args.dl1, args.dl2
    Z_ARM = args.shoulder_z - 0.1166  # SO-101 base_link on the riser top flange
    # lengthen the chain: elbow / wrist joint origins move along the stock link axis (-x of upper_arm / lower_arm)
    so.joints["elbow_flex"]["T"][0, 3] -= DL1
    so.joints["wrist_flex"]["T"][0, 3] -= DL2
    b = Builder(out)
    kin: dict = {
        "robot": "picky_so101_rover_v3",
        "frame": "x forward, y left, z up; origin on the floor at the base centre",
    }

    # ---- base links (planar virtual joints or floating)
    if args.base == "planar":
        b.link("base_anchor", np.eye(4), 1.0)
        b.link("base_x_link", np.eye(4), 1.0)
        b.link("base_y_link", np.eye(4), 1.0)
    base_I = I_from6([0.465408937, 0.483193291, 0.550939703, 0.002160024, -0.001760255, -0.000655952])
    b.link(
        "base_link", np.eye(4), 28.27 + 2.6, com=(0.0, 0.0, PMB2_Z + 0.05), inertia=base_I + np.diag([0.1, 0.1, 0.05])
    )

    # ---- PMB2 body (pmb2_description, exact poses from the xacros)
    Tb = T_from(p=(0, 0, PMB2_Z))
    body = load_stl(pm / "meshes/base/base.stl")
    # skirt bodies (below the bumper) dark, the shell white
    parts = body.split(only_watertight=False)
    shell = [p for p in parts if p.bounds[1][2] > -0.02]
    skirt = [p for p in parts if p.bounds[1][2] <= -0.02]
    b.mesh(f"{ROOT}/base_link/pmb2/shell", trimesh.util.concatenate(shell), "shell_white", Tb)
    if skirt:
        b.mesh(f"{ROOT}/base_link/pmb2/skirt", trimesh.util.concatenate(skirt), "trim_black", Tb)
    ringm = load_stl(pm / "meshes/base/base_ring.stl")
    b.mesh(f"{ROOT}/base_link/pmb2/bumper_ring", ringm, "trim_black", T_from(p=(0, 0, RING_Z)))
    # thin accent line around the bumper (one 5 mm band, just proud of the ring)
    b.mesh(
        f"{ROOT}/base_link/pmb2/accent_line",
        tube(BASE_R + 0.0012, BASE_R - 0.004, 0.005, (0, 0, RING_Z + 0.060), 128),
        "accent",
    )
    dock = load_stl(pm / "meshes/base/base_dock_link.stl")
    b.mesh(f"{ROOT}/base_link/pmb2/dock", dock, "steel", T_from(p=(0.2675, 0, PMB2_Z + 0.061)))
    # top deck: black ring trim where the shell meets the top + the riser / basket sit on it
    b.mesh(f"{ROOT}/base_link/deck/top_trim", tube(0.262, 0.236, 0.004, (0, 0, BASE_TOP + 0.002), 128), "trim_black")
    # casters (fixed; caster_1 = fork, caster_2 = wheel)
    c1 = load_stl(pm / "meshes/wheels/caster_1.stl")
    c2 = load_stl(pm / "meshes/wheels/caster_2.stl")
    for nm, sx, sy in (("front_right", 1, 1), ("front_left", 1, -1), ("back_right", -1, 1), ("back_left", -1, -1)):
        p1 = np.array([CASTER_OFF[0] + sx * CASTER_SEP_X / 2, CASTER_OFF[1] - sy * CASTER_SEP_Y / 2, CASTER_OFF[2]])
        T1 = Tb @ T_from(p=p1)
        T2 = T1 @ T_from(Rx(-math.pi / 2), (-0.016, 0, -0.040))
        b.mesh(f"{ROOT}/base_link/pmb2/caster_{nm}_fork", c1, "hub_grey", T1)
        b.mesh(f"{ROOT}/base_link/pmb2/caster_{nm}_wheel", c2, "rubber", T2)
    # drive wheels: links + continuous joints (spin from the base twist; no collision)
    wm = load_stl(pm / "meshes/wheels/wheel.stl", max_faces=40000)
    kin["wheels"] = []
    for side, refl in (("right", 1.0), ("left", -1.0)):
        Tw = Tb @ T_from(Rx(-math.pi / 2), (0, -WHEEL_SEP / 2 * refl, 0))  # wheel link frame, spin axis = local z
        axis_w = Tw[:3, :3] @ np.array([0, 0, 1.0])
        Tj = T_from(rot_x_to(axis_w), Tw[:3, 3])
        wl = f"wheel_{side}"
        b.link(wl, Tj, 1.82)
        Tvis = np.linalg.inv(Tj) @ Tw @ T_from(Rx(-math.pi / 2 * (1 - refl)))
        b.mesh(f"{ROOT}/{wl}/wheel", wm, "rubber", Tvis)
        b.mesh(f"{ROOT}/{wl}/hub", cyl(0.045, 0.036, (0, 0, 0), sections=40), "hub_grey", Tvis)
        b.joint("revolute", f"wheel_{side}_joint", "base_link", wl, Tj, "X", drive={"kp": 0.0, "kd": 0.5, "fmax": 50.0})
        kin["wheels"].append(
            {
                "joint": f"wheel_{side}_joint",
                "link": wl,
                "center": [float(v) for v in Tj[:3, 3]],
                "axis": [float(v) for v in axis_w],
                "radius": WHEEL_R,
            }
        )
    kin["drive"] = {"type": "differential", "wheel_radius": WHEEL_R, "wheel_separation": WHEEL_SEP}
    kin["footprint_radius"] = BASE_R

    # ---- riser (fixed column, no lift)
    r_top = Z_ARM
    flange_t = 0.010
    col = cyl(RISER_R, r_top - BASE_TOP - 2 * flange_t, (X_PAN, 0, (BASE_TOP + r_top) / 2), sections=64)
    b.mesh(f"{ROOT}/base_link/riser/column", col, "shell_white")
    b.mesh(
        f"{ROOT}/base_link/riser/foot",
        cyl(0.068, flange_t, (X_PAN, 0, BASE_TOP + flange_t / 2), sections=64),
        "trim_black",
    )
    b.mesh(
        f"{ROOT}/base_link/riser/foot_bolts",
        trimesh.util.concatenate(
            [
                cyl(
                    0.0045,
                    0.004,
                    (X_PAN + 0.056 * math.cos(a), 0.056 * math.sin(a), BASE_TOP + flange_t + 0.002),
                    sections=12,
                )
                for a in np.linspace(0, 2 * math.pi, 6, endpoint=False) + 0.5236
            ]
        ),
        "steel",
    )
    # top flange: rounded plate under the SO-101 base (base footprint x 0.12..0.23, y -0.026..0.061)
    plate = sbox(X_PAN - 0.062, -0.034, X_PAN + 0.062, 0.069).buffer(-0.012).buffer(0.012, resolution=8)
    pmesh = trimesh.creation.extrude_polygon(plate, flange_t)
    pmesh.apply_translation((0, 0, r_top - flange_t))
    b.mesh(f"{ROOT}/base_link/riser/top_plate", pmesh, "trim_black")
    b.mesh(
        f"{ROOT}/base_link/riser/top_collar",
        cyl(RISER_R + 0.004, 0.018, (X_PAN, 0, r_top - flange_t - 0.009), sections=64),
        "trim_black",
    )
    b.mesh(
        f"{ROOT}/base_link/riser/accent_line",
        tube(RISER_R + 0.0008, RISER_R - 0.002, 0.005, (X_PAN, 0, r_top - 0.050), 64),
        "accent",
    )
    # head camera (RealSense-like bar) on the riser front, 20 deg down
    hc = np.array([X_PAN + RISER_R + 0.016, 0.0, r_top - 0.085])
    Th = T_from(Ry(math.radians(20)), hc)
    b.mesh(
        f"{ROOT}/base_link/head/bracket", boxm((0.024, 0.03, 0.04), (X_PAN + RISER_R + 0.002, 0, hc[2])), "alu_black"
    )
    b.mesh(f"{ROOT}/base_link/head/body", boxm((0.025, 0.09, 0.025)), "camera_black", Th)
    b.mesh(f"{ROOT}/base_link/head/lens", boxm((0.002, 0.07, 0.012), (0.0125, 0, 0)), "glass", Th)
    # front camera on the base front (above the dock)
    fc = np.array([0.252, 0.0, BASE_TOP - 0.020])
    b.mesh(f"{ROOT}/base_link/front_cam/body", boxm((0.020, 0.05, 0.022), fc - [0.006, 0, 0]), "camera_black")
    # basket camera on the riser back, looking down into the basket
    bcam = np.array([X_PAN - RISER_R - 0.012, 0.0, r_top - 0.03])
    b.mesh(f"{ROOT}/base_link/basket_cam/body", boxm((0.02, 0.035, 0.025), bcam + [0.004, 0, 0]), "camera_black")

    # ---- circular wire basket (rear half; its back follows the base curve)
    bx, by = BASKET_C
    z0 = BASE_TOP + COLLAR_H  # basket floor (top of the collar)
    b.mesh(
        f"{ROOT}/base_link/basket/collar",
        tube(BASKET_R_BOT + 0.012, BASKET_R_BOT - 0.03, COLLAR_H, (bx, by, BASE_TOP + COLLAR_H / 2), 128),
        "trim_black",
    )
    b.mesh(
        f"{ROOT}/base_link/basket/floor_panel",
        cyl(BASKET_R_BOT - 0.002, 0.004, (bx, by, z0 + 0.002), sections=96),
        "wire_white",
    )

    def r_at(z):  # basket radius at height z above the floor (taper)
        return BASKET_R_BOT + (BASKET_R_TOP - BASKET_R_BOT) * z / BASKET_H

    wires = []
    rim_z = BASKET_H - 0.006
    for k in range(N_WIRES):
        a = 2 * math.pi * k / N_WIRES
        p0 = (bx + r_at(0.004) * math.cos(a), by + r_at(0.004) * math.sin(a), z0 + 0.004)
        p1 = (bx + r_at(rim_z) * math.cos(a), by + r_at(rim_z) * math.sin(a), z0 + rim_z)
        wires.append(segment(p0, p1, 0.0021))
    for zz in (0.006, 0.055, 0.105, 0.150):
        wires.append(ring(r_at(zz) + 0.0035, 0.0022, z0 + zz, (bx, by)))
    # wire grid on the floor panel
    gp = 0.03
    rr = BASKET_R_BOT - 0.008
    for t in np.arange(-rr + gp / 2, rr, gp):
        h = math.sqrt(max(rr * rr - t * t, 0.0))
        if h > 0.01:
            wires.append(segment((bx + t, by - h, z0 + 0.006), (bx + t, by + h, z0 + 0.006), 0.0018))
            wires.append(segment((bx - h, by + t, z0 + 0.0095), (bx + h, by + t, z0 + 0.0095), 0.0018))
    b.mesh(f"{ROOT}/base_link/basket/wire_mesh", trimesh.util.concatenate(wires), "wire_white", crease=60)
    # rolled rim + black trim cover (the one-product look: same black as the base trim, accent line under it)
    b.mesh(
        f"{ROOT}/base_link/basket/rim", ring(r_at(rim_z) + 0.001, 0.0060, z0 + rim_z, (bx, by), 160, 14), "trim_black"
    )
    b.mesh(
        f"{ROOT}/base_link/basket/rim_accent",
        ring(r_at(rim_z - 0.014), 0.0016, z0 + rim_z - 0.014, (bx, by), 160, 8),
        "accent",
    )
    # colliders: floor + 16 wall segments
    b.collider_box(
        f"{ROOT}/base_link/basket/col_floor", (bx, by, z0 - 0.004), (2 * BASKET_R_BOT, 2 * BASKET_R_BOT, 0.016)
    )
    nseg = 16
    for k in range(nseg):
        a = 2 * math.pi * (k + 0.5) / nseg
        rm = 0.5 * (BASKET_R_BOT + BASKET_R_TOP) + 0.006
        chord = 2 * rm * math.tan(math.pi / nseg) + 0.012
        c = (bx + rm * math.cos(a), by + rm * math.sin(a), z0 + BASKET_H / 2)
        b.collider_box(f"{ROOT}/base_link/basket/col_wall_{k:02d}", c, (0.012, chord, BASKET_H), Rz(a))
    b.collider_box(f"{ROOT}/base_link/col_body", (0, 0, 0.18), (0.40, 0.40, 0.22))
    kin["basket"] = {
        "center": [bx, by, z0 + 0.006],
        "r_floor": BASKET_R_BOT - 0.005,
        "r_top": BASKET_R_TOP - 0.005,
        "floor_z": z0 + 0.006,
        "rim_z": z0 + BASKET_H,
        "lo": [bx - BASKET_R_BOT, by - BASKET_R_BOT, z0],
        "hi": [bx + BASKET_R_BOT, by + BASKET_R_BOT, z0 + BASKET_H],
    }

    # ---- long-reach SO-101 arm
    T_armbase = T_from(Rz(-math.pi / 2), (X_PAN, 0.0388, Z_ARM))  # SO-101 base_link: +x -> robot -y (pan 0 = right)
    T_so = so.fk({})
    so_links = [
        "shoulder_link",
        "upper_arm_link",
        "lower_arm_link",
        "wrist_link",
        "gripper_link",
        "moving_jaw_so101_v1_link",
    ]
    merged = {
        "base_link": "base_link",
        "wrist_camera_mount_link": "so101_gripper",
        "wrist_camera_link": "so101_gripper",
        "gripper_frame_link": "so101_gripper",
    }
    usd_name = {
        "shoulder_link": "so101_shoulder",
        "upper_arm_link": "so101_upper_arm",
        "lower_arm_link": "so101_lower_arm",
        "wrist_link": "so101_wrist",
        "gripper_link": "so101_gripper",
        "moving_jaw_so101_v1_link": "so101_jaw",
    }
    mesh_cache = {}

    def so_mesh(v):
        key = (v["file"], tuple(v["scale"]))
        if key not in mesh_cache:
            mesh_cache[key] = load_stl(so.mesh_path(v["file"]), scale=v["scale"])
        return mesh_cache[key]

    def so_mat(v):
        if Path(v["file"]).stem.startswith("wrist_camera_so101"):
            return "camera_black"
        return "servo_black" if v["material"] == "sts3215" else "pla_white"

    # link visuals in their own link frames (extensions applied to upper / lower arm)
    vis = {}
    for ln in so.links:
        vis[ln] = [
            (so_mesh(v).copy().apply_transform(v["T"]), so_mat(v), f"{Path(v['file']).stem}_{i}")
            for i, v in enumerate(so.visuals.get(ln, []))
        ]
    ext_info = {}
    for ln, cut, dL in (("upper_arm_link", CUT1, DL1), ("lower_arm_link", CUT2, DL2)):
        if dL <= 0:
            continue
        vis[ln], sec = split_link_meshes(vis[ln], cut, dL)
        extra, (n_t, zs) = extension_parts(cut, dL, sec)
        vis[ln] += extra
        ext_info[ln] = {
            "cut_x": cut,
            "dL": dL,
            "section_yz": [round(float(v), 4) for v in sec],
            "tube_z": [round(float(z), 4) for z in zs],
        }

    # reducer drums (black, accent ring) on the pivot side of shoulder_lift and elbow_flex: in the CHILD's parent frame
    def drum(z_side: float, r=0.024, h=0.016):
        d = cyl(r, h, (0, 0, z_side + h / 2), sections=56)
        cap = cyl(r * 0.55, 0.003, (0, 0, z_side + h + 0.0015), sections=40)
        band = tube(r + 0.0006, r - 0.002, 0.003, (0, 0, z_side + h * 0.55), 56)
        return d, cap, band

    # masses / inertias (stock URDF inertials + servo upgrades + extension parts)
    inert = {}
    for ln in so_links:
        inn = so.inertial.get(ln, {"mass": 0.05, "com": np.zeros(3), "Iner": None})
        parts = [(inn["mass"], np.asarray(inn["com"], float), I_from6(inn["Iner"]) if inn.get("Iner") else None)]
        if ln in ext_info:
            e = ext_info[ln]
            dL, cut = e["dL"], e["cut_x"]
            # stock mass beyond the cut moves out with the far half: approximate by moving the stock COM by the far
            # fraction (the far half holds the next servo: ~70 % of the link mass)
            parts[0] = (parts[0][0], parts[0][1] + np.array([-0.7 * dL, 0, 0]), parts[0][2])
            m_t = 2 * math.pi * (TUBE_R**2 - TUBE_RI**2) * (dL - 2 * COUPLER_L) * 1550.0
            yc = 0.5 * (e["section_yz"][0] + e["section_yz"][2])
            zc = 0.5 * (e["section_yz"][1] + e["section_yz"][3])
            parts.append((m_t, (cut - dL / 2, yc, zc), rod_I(m_t, dL, 0.02)))
            parts.append((0.012, (cut - COUPLER_L / 2, yc, zc), None))
            parts.append((0.012, (cut - dL + COUPLER_L / 2, yc, zc), None))
        if ln == "upper_arm_link":  # elbow servo STS3215 -> STS3250 (+19.5 g at the elbow) + 2:1 drum
            parts.append((STS3250["mass"] - STS3215["mass"], (-0.1126 - DL1, -0.016, 0.018), None))
        if ln == "shoulder_link":  # shoulder_lift STS3250 + 3:1 drum
            parts.append((STS3250["mass"] - STS3215["mass"] + 0.045, (-0.03, -0.0, -0.04), None))
        if ln == "upper_arm_link":
            parts.append((0.035, (-0.1126 - DL1, -0.028, 0.06), None))  # elbow reducer drum
        M, c, Iner = mass_props(parts)
        inert[ln] = (M, c, Iner)

    for ln in so_links:
        M, c, Iner = inert[ln]
        b.link(usd_name[ln], T_armbase @ T_so[ln], M, com=c, inertia=Iner, gravity=True)
    for ln in so.links:
        target = usd_name.get(ln) or merged.get(ln)
        if target is None:
            continue
        T_tgt = b.links[target]
        T_rel = np.linalg.inv(T_tgt) @ T_armbase @ T_so[ln]
        for m, mat, tag in vis[ln]:
            b.mesh(f"{ROOT}/{target}/so101_{ln.replace('_link', '')}_{prim_name(tag)}", m, mat, T_rel)
    # drums: shoulder_lift pivot side is +z of the upper_arm joint frame (servo spans z -0.0015..0.038, fork to 0.054)
    for jn, parent in (("shoulder_lift", "shoulder_link"), ("elbow_flex", "upper_arm_link")):
        Tj_link = so.joints[jn]["T"]  # joint frame in the parent link (joint axis = z)
        T_rel = np.linalg.inv(b.links[usd_name[parent]]) @ T_armbase @ T_so[parent] @ Tj_link
        d, cap, band = drum(0.0545)
        b.mesh(f"{ROOT}/{usd_name[parent]}/reducer_{jn}", d, "alu_black", T_rel)
        b.mesh(f"{ROOT}/{usd_name[parent]}/reducer_{jn}_cap", cap, "steel", T_rel)
        b.mesh(f"{ROOT}/{usd_name[parent]}/reducer_{jn}_band", band, "accent", T_rel)

    # ---- joints with actuator models
    chain = []
    act_out = {}
    ref_q = {
        "shoulder_pan": -math.pi / 2,
        "shoulder_lift": 0.9,
        "elbow_flex": -0.4,
        "wrist_flex": -0.5,
        "wrist_roll": 0.0,
        "gripper": 0.0,
    }
    T_ref = so.fk(ref_q)
    for jn in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"):
        j = so.joints[jn]
        par = usd_name.get(j["parent"]) or merged[j["parent"]]
        Tj = T_armbase @ T_so[j["parent"]] @ j["T"]
        R_al = rot_x_to(j["axis"])
        servo, ratio, eff, vmax = ACTUATORS[jn]
        tmax = servo["stall"] * ratio * eff
        # inertia of everything outboard of this joint about its axis at the reference pose (+ reflected rotor)
        sub = so.subtree(j["child"])
        Tja = T_ref[j["parent"]] @ j["T"]
        ax = Tja[:3, :3] @ np.asarray(j["axis"], float)
        I_ax = 0.0
        for ln in sub:
            if ln not in inert:
                continue
            M, c, Iner = inert[ln]
            Rl = T_ref[ln][:3, :3]
            cw = (T_ref[ln] @ np.r_[c, 1.0])[:3]
            r = cw - Tja[:3, 3]
            r_perp = r - ax * np.dot(r, ax)
            I_ax += float(ax @ (Rl @ Iner @ Rl.T) @ ax) + M * float(np.dot(r_perp, r_perp))
        armature = ROTOR_ARMATURE * ratio * ratio
        kp = tmax / SAT_ERR
        kd = 2.0 * math.sqrt(kp * (I_ax + armature))
        drive = {"kp": kp / DEG, "kd": kd / DEG, "fmax": tmax}
        extra = {
            "physxJoint:maxJointVelocity": (Sdf.ValueTypeNames.Float, vmax * DEG),
            "physxJoint:armature": (Sdf.ValueTypeNames.Float, armature),
            "physxJoint:jointFriction": (Sdf.ValueTypeNames.Float, 0.02),
        }
        b.joint(
            "revolute",
            jn,
            par,
            usd_name[j["child"]],
            T_from(Tj[:3, :3] @ R_al, Tj[:3, 3]),
            "X",
            j["lower"] * DEG,
            j["upper"] * DEG,
            drive=drive,
            extra=extra,
        )
        chain.append(
            {
                "name": jn,
                "parent": j["parent"],
                "child": j["child"],
                "T": j["T"].tolist(),
                "axis": j["axis"],
                "lower": j["lower"],
                "upper": j["upper"],
            }
        )
        act_out[jn] = {
            "servo": servo["servo"],
            "ratio": ratio,
            "efficiency": eff,
            "stall_Nm": round(tmax, 3),
            "rated_Nm": round(servo["rated"] * ratio * eff, 3),
            "no_load_rad_s": round(servo["w0"] / ratio, 2),
            "vmax_rad_s": vmax,
            "kp_Nm_per_rad": round(kp, 1),
            "kd_Nms_per_rad": round(kd, 2),
            "armature": round(armature, 4),
            "I_link_ref": round(I_ax, 4),
        }

    # ---- planar base joints
    if args.base == "planar":
        b.joint("fixed", "world_fix", None, "base_anchor", np.eye(4))
        lin = {"kp": 2.0e5, "kd": 2.0e4, "fmax": 6000.0}
        b.joint("prismatic", "base_x", "base_anchor", "base_x_link", np.eye(4), "X", -100.0, 100.0, drive=lin)
        b.joint("prismatic", "base_y", "base_x_link", "base_y_link", np.eye(4), "Y", -100.0, 100.0, drive=lin)
        b.joint(
            "revolute",
            "base_yaw",
            "base_y_link",
            "base_link",
            np.eye(4),
            "Z",
            drive={"kp": 2.0e4 / DEG, "kd": 2.0e3 / DEG, "fmax": 4000.0},
        )

    # ---- articulation root
    rp = b.stage.GetPrimAtPath(ROOT)
    UsdPhysics.ArticulationRootAPI.Apply(rp)
    for k, (tn, v) in {
        "physxArticulation:enabledSelfCollisions": (Sdf.ValueTypeNames.Bool, False),
        "physxArticulation:solverPositionIterationCount": (Sdf.ValueTypeNames.Int, 32),
        "physxArticulation:solverVelocityIterationCount": (Sdf.ValueTypeNames.Int, 4),
        "physxArticulation:sleepThreshold": (Sdf.ValueTypeNames.Float, 0.0),
        "physxArticulation:stabilizationThreshold": (Sdf.ValueTypeNames.Float, 0.0),
    }.items():
        rp.CreateAttribute(k, tn).Set(v)
    rp.AddAppliedSchema("PhysxArticulationAPI")

    # ---- cameras (USD cameras look down -Z with +Y up)
    def camera(path, T, focal, near=0.01, haperture=20.955, vaperture=None):
        c = UsdGeom.Camera.Define(b.stage, path)
        c.CreateFocalLengthAttr(focal)
        c.CreateHorizontalApertureAttr(haperture)
        c.CreateVerticalApertureAttr(vaperture or haperture * 9 / 16)
        c.CreateClippingRangeAttr(Gf.Vec2f(near, 100.0))
        UsdGeom.Xformable(c).AddTransformOp().Set(gf_matrix(T))
        return path

    def look_T(eye, target, up=(0, 0, 1)):
        eye, target = np.asarray(eye, float), np.asarray(target, float)
        z = eye - target
        z /= np.linalg.norm(z)
        x = np.cross(up, z)
        x /= np.linalg.norm(x)
        return T_from(np.c_[x, np.cross(z, x), z], eye)

    def cam_R(forward, up):
        f = np.asarray(forward, float) / np.linalg.norm(forward)
        r = np.cross(f, up)
        r /= np.linalg.norm(r)
        return np.c_[r, np.cross(r, f), -f]

    cams = {}
    cams["head"] = camera(
        f"{ROOT}/base_link/cam_head", Th @ T_from(cam_R((1, 0, 0), (0, 0, 1)), (0.013, 0, 0)), 11.0, 0.03
    )
    cams["front"] = camera(f"{ROOT}/base_link/cam_front", look_T(fc + [0.006, 0, 0], fc + [1.0, 0, 0.15]), 9.0, 0.02)
    cams["basket"] = camera(
        f"{ROOT}/base_link/cam_basket", look_T(bcam - [0.008, 0, 0.012], (bx, by, z0), up=(1, 0, 0)), 7.5, 0.02
    )
    cams["chase"] = camera(f"{ROOT}/base_link/cam_chase", look_T((-1.45, -1.05, 1.75), (0.05, 0.0, 0.60)), 14.0, 0.05)
    # wrist: the SO-101 wrist_camera_link looks along its +z; grasp roll puts it ABOVE the jaws for a shelf grasp
    best = None
    for roll in (math.pi / 2, -math.pi / 2):
        Tq = so.fk({"wrist_roll": roll})
        dz = Tq["wrist_camera_link"][2, 3] - Tq["gripper_frame_link"][2, 3]
        if best is None or dz > best[1]:
            best = (roll, dz, Tq)
    grasp_roll, _, Tq = best
    up_g = np.linalg.inv(Tq["gripper_link"])[:3, :3] @ np.array([0, 0, 1.0])
    T_rel = np.linalg.inv(T_so["gripper_link"]) @ T_so["wrist_camera_link"]
    wv = so.visuals["wrist_camera_link"][0]
    cm = so_mesh(wv)
    cv = (wv["T"] @ np.c_[cm.vertices, np.ones(len(cm.vertices))].T).T[:, :3]
    sel = np.hypot(cv[:, 0], cv[:, 1]) < 0.006
    lens_z = float(cv[sel, 2].max()) if sel.any() else 0.02
    T_rel = T_rel @ T_from(p=(0, 0, lens_z + 0.003))
    fwd = T_rel[:3, 2]
    up_g = up_g - fwd * np.dot(up_g, fwd)
    cams["wrist"] = camera(f"{ROOT}/so101_gripper/cam_wrist", T_from(cam_R(fwd, up_g), T_rel[:3, 3]), 4.0, 0.005)
    kin["grasp_roll"] = float(grasp_roll)
    kin["wrist_cam_lens_z"] = lens_z
    kin["cameras"] = cams

    # ---- kinematics + grasp data for so101_ik.py / the apps
    tcp_in_gripper = np.linalg.inv(T_so["gripper_link"]) @ T_so["gripper_frame_link"] @ T_from(p=(0, 0, -0.022))
    torque = torque_budget(so, inert, T_armbase, tcp_in_gripper)
    stretch_pose = torque.pop("_pose")
    for jn, t in torque.items():
        t["available_Nm"] = act_out[jn]["stall_Nm"]
        t["safety_factor"] = round(t["available_Nm"] / max(t["required_Nm"], 1e-6), 2)
    kin.update(
        {
            "base": {"mode": args.base, "joints": ["base_x", "base_y", "base_yaw"] if args.base == "planar" else []},
            "arm_base": T_armbase.tolist(),
            "shoulder_z": args.shoulder_z,
            "pan_axis_xy": [X_PAN, 0.0],
            "riser": {"x": X_PAN, "radius": RISER_R, "top_z": Z_ARM, "height": Z_ARM - BASE_TOP},
            "extension": {
                "dl1": DL1,
                "dl2": DL2,
                "links": ext_info,
                "upper_arm_m": float(np.linalg.norm(so.joints["elbow_flex"]["T"][:2, 3])),
                "forearm_m": float(np.linalg.norm(so.joints["wrist_flex"]["T"][:2, 3])),
            },
            "arm_chain": chain[:5],
            "gripper": chain[5],
            "tcp_in_gripper_link": tcp_in_gripper.tolist(),
            "links": {k: f"{ROOT}/{v}" for k, v in usd_name.items()},
            "arm_joints": ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"],
            "actuators": act_out,
            "torque_budget": torque,
            "torque_budget_pose": stretch_pose,
            "payload_kg": PAYLOAD,
            "link_mass_kg": {usd_name[k]: round(v[0], 4) for k, v in inert.items()},
            "poses": {
                "stow": {
                    "shoulder_pan": -1.5708,
                    "shoulder_lift": -0.25,
                    "elbow_flex": 1.62,
                    "wrist_flex": 1.0,
                    "wrist_roll": 0.0,
                    "gripper": 0.0,
                },
                "ready": {
                    "shoulder_pan": -1.5708,
                    "shoulder_lift": 0.5,
                    "elbow_flex": 0.2,
                    "wrist_flex": -0.6,
                    "wrist_roll": 1.5708,
                    "gripper": 0.0,
                },
            },
            "gripper_open": 1.2,
            "gripper_closed": 0.0,
        }
    )
    # jaw sample points (link frames): grasp check = the product box is between these two point sets
    rng = np.random.default_rng(0)
    fj = so_mesh(so.visuals["gripper_link"][1])  # wrist_roll_follower (fixed jaw)
    Tfj = so.visuals["gripper_link"][1]["T"]
    pts, _ = trimesh.sample.sample_surface(fj, 4000, seed=1)
    pts = (Tfj @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
    tcp_p = tcp_in_gripper[:3, 3]
    near = np.linalg.norm(pts - tcp_p, axis=1) < 0.05
    kin["fixed_jaw_points"] = pts[near][rng.permutation(near.sum())[:400]].round(5).tolist()
    mj = so_mesh(so.visuals["moving_jaw_so101_v1_link"][0])
    Tmj = so.visuals["moving_jaw_so101_v1_link"][0]["T"]
    pts, _ = trimesh.sample.sample_surface(mj, 4000, seed=2)
    pts = (Tmj @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
    T_jaw_in_gr = np.linalg.inv(T_so["gripper_link"]) @ T_so["moving_jaw_so101_v1_link"]
    pg = (T_jaw_in_gr @ np.c_[pts, np.ones(len(pts))].T).T[:, :3]
    near = np.linalg.norm(pg - tcp_p, axis=1) < 0.05
    kin["moving_jaw_points"] = pts[near][rng.permutation(near.sum())[:400]].round(5).tolist()

    b.stage.GetRootLayer().customLayerData = {
        "picky": {
            "robot_kind": "pmb2_riser_longreach_so101_wire_basket",
            "sources": "SO-101: TheRobotStudio/SO-ARM100 Simulation/SO101 (Apache-2.0); "
            "base: pal-robotics/pmb2_robot pmb2_description (Apache-2.0); riser/extensions/reducers/basket generated",
            "base_mode": args.base,
        }
    }
    b.stage.GetRootLayer().Save()
    kp_ = out.with_suffix(".kin.json")
    kp_.write_text(json.dumps(kin, indent=1))
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {b.n_mesh} meshes, {b.n_faces} tris) + {kp_}")
    for jn, t in torque.items():
        print(
            f"  {jn:14s} required {t['required_Nm']:.2f} N*m  available {t['available_Nm']:.2f}"
            f"  SF {t['safety_factor']:.2f}"
        )


def torque_budget(so: Urdf, inert: dict, T_armbase, tcp_in_gripper) -> dict:
    """Static holding torque at shoulder_lift / elbow_flex / wrist_flex with the arm stretched out horizontally
    (forward, the worst case for every pitch joint) and PAYLOAD kg at the tool point, from the link masses/COMs."""

    # find the horizontal stretch: upper arm, forearm and tool all horizontal and pointing AWAY from the shoulder
    def pts(q):
        Tq = so.fk(q)
        return (
            Tq["upper_arm_link"][:3, 3],
            Tq["lower_arm_link"][:3, 3],
            Tq["wrist_link"][:3, 3],
            (Tq["gripper_link"] @ tcp_in_gripper)[:3, 3],
        )

    sl = min(
        np.linspace(0.5, 1.75, 251),
        key=lambda v: abs(pts({"shoulder_lift": v})[1][2] - pts({"shoulder_lift": v})[0][2]),
    )
    s0, e0, _, _ = pts({"shoulder_lift": sl})
    d = (e0 - s0) * np.array([1, 1, 0])
    d /= np.linalg.norm(d)

    def score(p0, p1):
        return abs(p1[2] - p0[2]) if np.dot(p1 - p0, d) > 0 else 9.0

    el = min(np.linspace(-1.69, 1.69, 339), key=lambda v: score(*pts({"shoulder_lift": sl, "elbow_flex": v})[1:3]))
    wf = min(
        np.linspace(-1.65, 1.65, 331),
        key=lambda v: score(*pts({"shoulder_lift": sl, "elbow_flex": el, "wrist_flex": v})[2:4]),
    )
    best = (0.0, wf)
    q = {"shoulder_lift": sl, "elbow_flex": el, "wrist_flex": best[1]}
    Tq = so.fk(q)
    tcp = (Tq["gripper_link"] @ tcp_in_gripper)[:3, 3]
    out = {}
    for jn in ("shoulder_lift", "elbow_flex", "wrist_flex"):
        j = so.joints[jn]
        Tj = Tq[j["parent"]] @ j["T"]
        ax = Tj[:3, :3] @ np.asarray(j["axis"], float)
        o = Tj[:3, 3]
        tau = 0.0
        for ln in so.subtree(j["child"]):
            if ln not in inert:
                continue
            M, c, _ = inert[ln]
            cw = (Tq[ln] @ np.r_[c, 1.0])[:3]
            tau += float(np.dot(np.cross(cw - o, np.array([0, 0, -M * G])), ax))
        tau_p = float(np.dot(np.cross(tcp - o, np.array([0, 0, -PAYLOAD * G])), ax))
        out[jn] = {
            "required_Nm": round(abs(tau + tau_p), 3),
            "arm_only_Nm": round(abs(tau), 3),
            "payload_Nm": round(abs(tau_p), 3),
            "lever_to_tcp_m": round(float(np.linalg.norm((tcp - o)[:2])), 3),
        }
    out["_pose"] = {k: round(v, 3) for k, v in q.items()}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--so101", required=True, help="SO-ARM100/Simulation/SO101")
    ap.add_argument("--pmb2", required=True, help="pmb2_robot/pmb2_description")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", choices=["planar", "floating"], default="planar")
    ap.add_argument("--dl1", type=float, default=0.25, help="upper-arm extension (m)")
    ap.add_argument("--dl2", type=float, default=0.275, help="forearm extension (m)")
    ap.add_argument("--shoulder-z", type=float, default=0.75, help="shoulder_lift axis height (m); sets the riser")
    build(ap.parse_args())
