# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Headless Isaac Sim 6.0 check for picky_rover.usda (run with Isaac's python.sh, NOT the system python).
  /data/$USER/isaacsim/python.sh arms/sim/tools/isaac_check_rover.py \
      /data/$USER/picky-assets/robots/picky-rover/picky_rover.usda /data/$USER/picky-assets/robots/renders 0.8
Plays physics, drives the lift to <lift_m> and base_x to 0.3 m (position drive), then renders an external view +
the robot's cam_front / cam_wrist / cam_basket to JPEG and prints the shoulder height (expect ~0.119 + lift_m).
Avoids deprecated isaacsim.core.api / prims / utils / sensors.camera (pure USD + replicator, like picky/smoke/cam_smoke.py).
"""

import sys, os

usd, outdir, lift_m = sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
from isaacsim import SimulationApp

app = SimulationApp({"headless": True})
import omni.usd, omni.timeline
import omni.replicator.core as rep
from pxr import UsdGeom, UsdLux, UsdPhysics, Gf
from PIL import Image

os.makedirs(outdir, exist_ok=True)
ctx = omni.usd.get_context()
ctx.new_stage()
stage = ctx.get_stage()
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(stage, 1.0)
UsdPhysics.Scene.Define(stage, "/World/physicsScene")
g = UsdGeom.Cube.Define(stage, "/World/ground")
g.CreateSizeAttr(1.0)
gx = UsdGeom.Xformable(g)
gx.AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.0443 - 0.05))
gx.AddScaleOp().Set(Gf.Vec3f(10, 10, 0.1))
UsdPhysics.CollisionAPI.Apply(g.GetPrim())
g.CreateDisplayColorAttr([Gf.Vec3f(0.55, 0.55, 0.5)])
UsdLux.DomeLight.Define(stage, "/World/dome").CreateIntensityAttr(900)
UsdLux.DistantLight.Define(stage, "/World/sun").CreateIntensityAttr(2500)
robot = stage.DefinePrim("/World/rover", "Xform")
robot.GetReferences().AddReference(usd)
R = "/World/rover"
jn = [p.GetName() for p in stage.Traverse() if p.IsA(UsdPhysics.Joint) and str(p.GetPath()).startswith(R)]
print("JOINTS:", jn)


def tgt(j, attr, v):
    stage.GetPrimAtPath(f"{R}/joints/{j}" if stage.GetPrimAtPath(f"{R}/joints/{j}") else j).GetAttribute(attr).Set(v)


tgt("lift", "drive:linear:physics:targetPosition", lift_m)
# base joints live under their links in lekiwi.usd; make base_x a stiff position drive for this test
bx = next(p for p in stage.Traverse() if p.GetName() == "base_x" and p.IsA(UsdPhysics.Joint))
d = UsdPhysics.DriveAPI.Get(bx, "linear")
d.GetStiffnessAttr().Set(1e5)
d.GetTargetPositionAttr().Set(0.3)
for j, v in {"shoulder_lift": -30.0, "elbow_flex": 40.0, "gripper": 30.0}.items():
    tgt(j, "drive:angular:physics:targetPosition", v)
tl = omni.timeline.get_timeline_interface()
tl.play()
for _ in range(300):
    app.update()
xc = UsdGeom.XformCache()
for link in ("base", "lift_carriage", "shoulder", "gripper"):
    t = xc.GetLocalToWorldTransform(stage.GetPrimAtPath(f"{R}/{link}")).ExtractTranslation()
    print(f"LINK {link:14s} world=({t[0]:.3f},{t[1]:.3f},{t[2]:.3f})")
cams = {
    "external": None,
    "front": f"{R}/base/cam_front",
    "wrist": f"{R}/gripper/cam_wrist",
    "basket": f"{R}/base/cam_basket",
}
ext = rep.create.camera(position=(1.3, -1.5, 1.3), look_at=(0.3, 0, 0.55))
anns = {}
for name, path in cams.items():
    rp = rep.create.render_product(ext if path is None else path, (960, 960) if path is None else (640, 480))
    a = rep.AnnotatorRegistry.get_annotator("rgb")
    a.attach(rp)
    anns[name] = a
for _ in range(3):
    rep.orchestrator.step()
for name, a in anns.items():
    data = a.get_data()
    if data is None or getattr(data, "size", 0) == 0:
        print("NO DATA", name)
        continue
    p = os.path.join(outdir, f"picky_rover_{name}_lift{lift_m:.2f}.jpg")
    Image.fromarray(data[..., :3]).save(p, quality=90)
    print("SAVED", p)
tl.stop()
app.close()
