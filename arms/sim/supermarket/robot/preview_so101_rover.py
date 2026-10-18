"""Render Picky's SO-101 rover alone (grey studio) in a few joint poses from several angles: a quick visual check of
geometry, materials, normals and joint frames after build_so101_rover.py.

    /data/advaiv2/isaacsim/python.sh preview_so101_rover.py --robot picky_so101_rover.usd --out renders/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--robot", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--res", type=int, nargs=2, default=[1280, 960])
ap.add_argument("--poses", default="stow,reach,high,basket,zero")
args = ap.parse_args()

from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": args.res[0], "height": args.res[1]})

import numpy as np  # noqa: E402
import omni.replicator.core as rep  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
from isaacsim.core.experimental.prims import Articulation  # noqa: E402
from PIL import Image  # noqa: E402
from pxr import Gf, UsdGeom, UsdLux, UsdPhysics  # noqa: E402

out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
kin = json.loads(Path(args.robot).with_suffix(".kin.json").read_text())

ctx = omni.usd.get_context()
ctx.new_stage()
for _ in range(5):
    app.update()
stage = ctx.get_stage()
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(stage, 1.0)
UsdPhysics.Scene.Define(stage, "/World/physicsScene").CreateGravityMagnitudeAttr(9.81)
g = UsdGeom.Cube.Define(stage, "/World/ground")
g.CreateSizeAttr(1.0)
UsdGeom.Xformable(g).AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.05))
UsdGeom.Xformable(g).AddScaleOp().Set(Gf.Vec3f(20, 20, 0.1))
g.CreateDisplayColorAttr([Gf.Vec3f(0.55, 0.56, 0.58)])
UsdPhysics.CollisionAPI.Apply(g.GetPrim())
UsdLux.DomeLight.Define(stage, "/World/dome").CreateIntensityAttr(900)
dl = UsdLux.DistantLight.Define(stage, "/World/sun")
dl.CreateIntensityAttr(2500)
dl.CreateAngleAttr(1.0)
UsdGeom.Xformable(dl).AddRotateXYZOp().Set(Gf.Vec3f(-50, 25, 30))
robot = stage.DefinePrim("/World/Picky", "Xform")
robot.GetReferences().AddReference(str(Path(args.robot).resolve()))


def look_at(path, eye, target, focal=24.0):
    cam = UsdGeom.Camera.Define(stage, path)
    cam.CreateFocalLengthAttr(focal)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.02, 100))
    eye, target = np.array(eye, float), np.array(target, float)
    z = eye - target
    z /= np.linalg.norm(z)
    x = np.cross([0, 0, 1.0], z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    UsdGeom.Xformable(cam).AddTransformOp().Set(Gf.Matrix4d(*x, 0, *y, 0, *z, 0, *eye, 1))
    return path


views = {
    "front34": look_at("/World/cams/front34", (2.0, -1.7, 1.45), (0.15, 0.0, 0.62)),
    "right": look_at("/World/cams/right", (0.2, -2.6, 0.85), (0.15, 0.0, 0.62)),
    "back34": look_at("/World/cams/back34", (-1.7, -1.3, 1.55), (0.0, 0.0, 0.5)),
    "top": look_at("/World/cams/top", (0.001, 0.0, 3.0), (0.1, 0.0, 0.0), 20.0),
    "basket_close": look_at("/World/cams/basket_close", (-0.75, -0.55, 1.05), (-0.05, 0.0, 0.42), 26.0),
    "arm_close": look_at("/World/cams/arm_close", (0.95, -0.75, 1.0), (0.25, 0.0, 0.75), 28.0),
}
for k in ("chase", "head", "wrist", "basket", "front"):
    views["robot_" + k] = "/World/Picky" + kin["cameras"][k][len("/Picky") :]

rps, anns = {}, {}
for name, path in views.items():
    rp = rep.create.render_product(path, tuple(args.res) if not name.startswith("robot_") else (960, 540))
    an = rep.AnnotatorRegistry.get_annotator("rgb")
    an.attach([rp])
    rps[name], anns[name] = rp, an

timeline = omni.timeline.get_timeline_interface()
timeline.play()
for _ in range(10):
    app.update()
art = Articulation("/World/Picky")
names = list(art.dof_names)
print("[preview] dofs:", names, flush=True)


def set_q(q: dict):
    idx = [names.index(k) for k in q]
    vals = [[q[k] for k in q]]
    art.set_dof_positions(vals, dof_indices=idx)
    art.set_dof_position_targets(vals, dof_indices=idx)


P = kin["poses"]
gr = kin["grasp_roll"]
poses = {
    "zero": {},
    "stow": P["stow"],
    "reach": {
        "shoulder_pan": -1.5708,
        "shoulder_lift": 0.75,
        "elbow_flex": -0.55,
        "wrist_flex": -0.2,
        "wrist_roll": gr,
        "gripper": 1.2,
    },
    "high": {
        "shoulder_pan": -1.5708,
        "shoulder_lift": -0.15,
        "elbow_flex": -0.75,
        "wrist_flex": 0.9,
        "wrist_roll": gr,
        "gripper": 0.4,
    },
    "basket": {
        "shoulder_pan": 1.5708,
        "shoulder_lift": -0.08,
        "elbow_flex": 1.09,
        "wrist_flex": 0.56,
        "wrist_roll": 0.0,
        "gripper": 1.2,
    },
}
for pname in args.poses.split(","):
    q = poses[pname]
    for _ in range(3):
        set_q(q) if q else None
        for _ in range(20):
            app.update()
    for _ in range(30):
        app.update()
    qq = art.get_dof_positions().numpy()[0]
    print(f"[preview] pose {pname}: " + ", ".join(f"{n}={v:.3f}" for n, v in zip(names, qq, strict=False)), flush=True)
    for name in views:
        if pname not in ("stow", "reach") and name.startswith("robot_") and name not in ("robot_wrist", "robot_chase"):
            continue
        d = anns[name].get_data()
        if d is None or getattr(d, "size", 0) == 0:
            print("[preview] no data", name, flush=True)
            continue
        Image.fromarray(np.asarray(d)[..., :3]).save(out / f"{pname}_{name}.jpg", quality=90)
print("[preview] DONE", flush=True)
app.close()
