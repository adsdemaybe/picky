# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Compose Picky's robot (rover + vertical lift + SO-101 + basket + 3 cameras) on top of LeIsaac's lekiwi.usd.

Writes <out>.usda that SUBLAYERS lekiwi.usd (original untouched) and authors overrides:
  - /LeKiwi/lift_carriage   new rigid link; PrismaticJoint 'lift' (base -> carriage, +Z, 0..1.08 m)
  - shoulder_pan re-parented: body0 = lift_carriage (arm rides the lift)
  - /LeKiwi/base/picky_mast (visual), /LeKiwi/base/picky_basket (5 slabs, visual + collision)
  - Cameras: /LeKiwi/base/cam_front (mast top), /LeKiwi/base/cam_basket, /LeKiwi/gripper/cam_wrist
LeKiwi conventions: base frame is rotated 180 deg about world Z; arm 'forward' = base +Y.
Usage: python compose_picky_rover.py lekiwi.usd out.usda    (usd-core or Isaac's python.sh; arms/sim/ASSETS.md)
The worker poses the result as a kinematic puppet (arms/sim/rover_puppet.py): it hides lift_carriage, picky_mast and
picky_basket and draws its own mast / carriage / basket at the contracts geometry.
"""

import math, sys, os
from pxr import Usd, UsdGeom, UsdPhysics, Gf, Sdf, UsdShade

src, out = sys.argv[1], sys.argv[2]
layer = Sdf.Layer.CreateNew(out)
layer.subLayerPaths.append(os.path.relpath(src, os.path.dirname(os.path.abspath(out))))
st = Usd.Stage.Open(layer)
st.SetEditTarget(Usd.EditTarget(layer))
st.SetDefaultPrim(st.GetPrimAtPath("/LeKiwi"))
UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(st, 1.0)

R = "/LeKiwi"
sp = st.GetPrimAtPath(f"{R}/joints/shoulder_pan")
anchor = sp.GetAttribute("physics:localPos0").Get()  # in base frame, ~(0, 0.067, 0.119)
rot0 = sp.GetAttribute("physics:localRot0").Get()
xc = UsdGeom.XformCache()
base_w = xc.GetLocalToWorldTransform(st.GetPrimAtPath(f"{R}/base"))
base_q = base_w.ExtractRotationQuat()

LIFT_TRAVEL = 1.08  # shoulder 0.119 m -> ~1.2 m (contracts.common LIFT_MIN/MAX_M = 0.2/1.2)


def color(prim, rgb):
    UsdGeom.Gprim(prim).CreateDisplayColorAttr([Gf.Vec3f(*rgb)])


def cube(path, center, size, rgb, collide=False):
    c = UsdGeom.Cube.Define(st, path)
    c.CreateSizeAttr(1.0)
    x = UsdGeom.Xformable(c)
    x.ClearXformOpOrder()
    x.AddTranslateOp().Set(Gf.Vec3d(*center))
    x.AddScaleOp().Set(Gf.Vec3f(*size))
    color(c.GetPrim(), rgb)
    if collide:
        UsdPhysics.CollisionAPI.Apply(c.GetPrim())
    return c


# 1) lift carriage link (flat under /LeKiwi like the other links), world pose = base * T(anchor)
car = UsdGeom.Xform.Define(st, f"{R}/lift_carriage")
cw = Gf.Matrix4d().SetTranslate(Gf.Vec3d(anchor)) * base_w
x = UsdGeom.Xformable(car)
x.ClearXformOpOrder()
x.AddTranslateOp().Set(cw.ExtractTranslation())
x.AddOrientOp().Set(Gf.Quatf(base_q))
UsdPhysics.RigidBodyAPI.Apply(car.GetPrim())
UsdPhysics.MassAPI.Apply(car.GetPrim()).CreateMassAttr(0.3)
cube(f"{R}/lift_carriage/visual_plate", (0, -0.025, 0.0), (0.07, 0.012, 0.07), (0.85, 0.45, 0.1))

# 2) prismatic lift joint base -> carriage (+Z of base == world up)
lj = UsdPhysics.PrismaticJoint.Define(st, f"{R}/joints/lift")
lj.CreateBody0Rel().SetTargets([f"{R}/base"])
lj.CreateBody1Rel().SetTargets([f"{R}/lift_carriage"])
lj.CreateLocalPos0Attr(Gf.Vec3f(anchor))
lj.CreateLocalRot0Attr(Gf.Quatf(1, 0, 0, 0))
lj.CreateLocalPos1Attr(Gf.Vec3f(0, 0, 0))
lj.CreateLocalRot1Attr(Gf.Quatf(1, 0, 0, 0))
lj.CreateAxisAttr("Z")
lj.CreateLowerLimitAttr(0.0)
lj.CreateUpperLimitAttr(LIFT_TRAVEL)
d = UsdPhysics.DriveAPI.Apply(lj.GetPrim(), "linear")
d.CreateTypeAttr("force")
d.CreateStiffnessAttr(2.0e4)
d.CreateDampingAttr(2.0e3)
d.CreateMaxForceAttr(5.0e3)
d.CreateTargetPositionAttr(0.0)

# 3) arm rides the carriage
sp.GetRelationship("physics:body0").SetTargets([f"{R}/lift_carriage"])
sp.GetAttribute("physics:localPos0").Set(Gf.Vec3f(0, 0, 0))
sp.GetAttribute("physics:localRot0").Set(rot0)

# 4) mast (visual only, on base, just behind the shoulder), basket (rear, collision), base frame coords
ax, ay, az = anchor
cube(f"{R}/base/picky_mast", (ax, ay - 0.045, 0.08 + 1.30 / 2), (0.035, 0.025, 1.30), (0.6, 0.6, 0.62))
B = f"{R}/base/picky_basket"
UsdGeom.Xform.Define(st, B)
bx, by, bz = 0.0, -0.075, 0.115  # floor of basket, rear of base (base -Y = rear)
W, Dp, H, t = 0.20, 0.15, 0.10, 0.006
blue = (0.1, 0.35, 0.8)
cube(f"{B}/floor", (bx, by, bz), (W, Dp, t), blue, True)
cube(f"{B}/wall_l", (bx - W / 2, by, bz + H / 2), (t, Dp, H), blue, True)
cube(f"{B}/wall_r", (bx + W / 2, by, bz + H / 2), (t, Dp, H), blue, True)
cube(f"{B}/wall_f", (bx, by + Dp / 2, bz + H / 2), (W, t, H), blue, True)
cube(f"{B}/wall_b", (bx, by - Dp / 2, bz + H / 2), (W, t, H), blue, True)


# 5) cameras (USD camera looks down its -Z, up = +Y)
def cam(path, pos, rx_deg, rz_deg=0.0, focal=12.0):
    c = UsdGeom.Camera.Define(st, path)
    c.CreateFocalLengthAttr(focal)
    c.CreateHorizontalApertureAttr(20.955)
    c.CreateClippingRangeAttr(Gf.Vec2f(0.01, 50.0))
    x = UsdGeom.Xformable(c)
    x.ClearXformOpOrder()
    x.AddTranslateOp().Set(Gf.Vec3d(*pos))
    x.AddRotateXYZOp().Set(Gf.Vec3f(rx_deg, 0, rz_deg))
    return c


cam(f"{R}/base/cam_front", (ax, ay - 0.045, 1.42), 60.0)  # mast top, looks forward (+Y base), 30 deg down
cam(f"{R}/base/cam_basket", (ax, ay - 0.03, 0.55), -35.0)  # on mast, looks back/down into basket
# wrist: SO-101 URDF wrist_camera_mount_joint origin in gripper_link (orientation unverified)
wc = UsdGeom.Camera.Define(st, f"{R}/gripper/cam_wrist")
wc.CreateFocalLengthAttr(8.0)
wc.CreateClippingRangeAttr(Gf.Vec2f(0.005, 10.0))
x = UsdGeom.Xformable(wc)
x.ClearXformOpOrder()
x.AddTranslateOp().Set(Gf.Vec3d(-0.015, 0.024, -0.0315))
x.AddRotateXYZOp().Set(Gf.Vec3f(-90.0, 0.0, -90.0))

layer.customLayerData = {
    "picky": {
        "robot_kind": "rover_so101",
        "lift_joint": "lift",
        "lift_travel_m": LIFT_TRAVEL,
        "shoulder_height_at_lift0_m": float(az),
        "base_joints": "base_x,base_y,base_theta (world-frame virtual planar joints)",
        "heading_offset_rad": math.pi / 2,
        "arm_joints": "shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper",
        "source": "LightwheelAI/leisaac_env assets/robots/lekiwi.usd (Apache-2.0)",
    }
}
layer.Save()
print("wrote", out)
