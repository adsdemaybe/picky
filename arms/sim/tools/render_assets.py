# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Picky grocery assets: headless Isaac Sim 6.0 renders (copied from the proven cam_smoke pattern).

  python.sh render_assets.py sheets [asset_id ...]   one tile per asset (front view from -Y) -> contact sheets
  python.sh render_assets.py shelf                   test shelf: ~24 SKUs, every unit its own rigid-body prim,
                                                     physics settle, front camera (1 m / 1.1 m) + close-ups
Outputs in $PICKY_ASSET_ROOT/_renders/ (default /data/$USER/picky-assets/products).
"""

import json
import math
import os
import sys
import time

R = os.environ.get("PICKY_ASSET_ROOT") or f"/data/{os.environ.get('USER', 'user')}/picky-assets/products"
OUT = f"{R}/_renders"
MODE = sys.argv[1] if len(sys.argv) > 1 else "shelf"
VIEWS = MODE == "views"
ONLY = sys.argv[2:]

t0 = time.time()
from isaacsim import SimulationApp  # noqa: E402

app = SimulationApp({"headless": True, "width": 1280, "height": 960})
print(f"[render] SimulationApp up in {time.time() - t0:.1f}s", flush=True)

import numpy as np  # noqa: E402
import omni.replicator.core as rep  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
import yaml  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from pxr import Gf, Sdf, UsdGeom, UsdLux, UsdPhysics, UsdShade  # noqa: E402

os.makedirs(OUT, exist_ok=True)
index = yaml.safe_load(open(f"{R}/index.yaml"))["assets"]
sku_map = yaml.safe_load(open(f"{R}/sku_map.yaml")) if os.path.exists(f"{R}/sku_map.yaml") else {}
stage = omni.usd.get_context().get_stage()
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
UsdGeom.SetStageMetersPerUnit(stage, 1.0)
world = UsdGeom.Xform.Define(stage, "/World")
stage.SetDefaultPrim(world.GetPrim())


def lights(store=False):
    dome = UsdLux.DomeLight.Define(stage, "/World/Dome")
    dome.CreateIntensityAttr(700 if store else 900)
    sun = UsdLux.DistantLight.Define(stage, "/World/Key")
    sun.CreateIntensityAttr(2500)
    sun.CreateAngleAttr(2.0)
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(-55, 0, -25))
    if store:
        for i, x in enumerate((-0.6, 0.6)):
            rl = UsdLux.RectLight.Define(stage, f"/World/Ceiling{i}")
            rl.CreateIntensityAttr(9000)
            rl.CreateWidthAttr(0.4)
            rl.CreateHeightAttr(1.6)
            UsdGeom.Xformable(rl).AddTranslateOp().Set(Gf.Vec3d(x, -1.0, 2.8))


def material(path, rgb, rough=0.7):
    m = UsdShade.Material.Define(stage, path)
    s = UsdShade.Shader.Define(stage, path + "/S")
    s.CreateIdAttr("UsdPreviewSurface")
    s.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
    s.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
    m.CreateSurfaceOutput().ConnectToSource(s.ConnectableAPI(), "surface")
    return m


def box(path, center, size, mat=None, collide=True):
    c = UsdGeom.Cube.Define(stage, path)
    c.CreateSizeAttr(1.0)
    xf = UsdGeom.Xformable(c)
    xf.AddTranslateOp().Set(Gf.Vec3d(*center))
    xf.AddScaleOp().Set(Gf.Vec3f(*size))
    if mat is not None:
        UsdShade.MaterialBindingAPI.Apply(c.GetPrim()).Bind(mat)
    if collide:
        UsdPhysics.CollisionAPI.Apply(c.GetPrim())
    return c


def camera(path, eye, target, focal=18.0):
    cam = UsdGeom.Camera.Define(stage, path)
    cam.CreateFocalLengthAttr(focal)
    cam.CreateHorizontalApertureAttr(20.955)
    cam.CreateVerticalApertureAttr(20.955 * 0.75)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100))
    xf = UsdGeom.Xformable(cam)
    op = xf.AddTransformOp()
    set_cam(op, eye, target)
    return cam, op


def set_cam(op, eye, target):
    m = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(0, 0, 1))
    op.Set(m.GetInverse())


def place(path, aid, pos, yaw=0.0, scale=1.0, kinematic=False):
    p = stage.DefinePrim(path, "Xform")
    p.GetReferences().AddReference(index[aid]["usd"])
    xf = UsdGeom.Xformable(p)
    xf.ClearXformOpOrder()
    xf.AddTranslateOp().Set(Gf.Vec3d(*pos))
    xf.AddRotateZOp().Set(float(yaw))
    s = scale if isinstance(scale, (list, tuple)) else (scale, scale, scale)
    xf.AddScaleOp().Set(Gf.Vec3f(*s))
    if kinematic:
        UsdPhysics.RigidBodyAPI(p).CreateKinematicEnabledAttr(True)
    return p


def grab(ann, steps=1, sub=8):
    for _ in range(steps):
        rep.orchestrator.step(rt_subframes=sub, delta_time=None)
    return np.asarray(ann.get_data())[..., :3].copy()


def font(sz):
    for f in ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, sz)
    return ImageFont.load_default()


# ------------------------------------------------------------------------------------------------ sheets
def sheets():
    lights()
    floor_m = material("/World/Looks/Floor", (0.35, 0.35, 0.37))
    ids = ONLY or sorted(index)
    spacing = 4.0
    cols = int(math.ceil(math.sqrt(len(ids))))
    box(
        "/World/Floor",
        (cols * spacing / 2, cols * spacing / 2, -0.005),
        (cols * spacing + 8, cols * spacing + 8, 0.01),
        floor_m,
        False,
    )
    pos = {}
    for i, aid in enumerate(ids):
        x, y = (i % cols) * spacing, (i // cols) * spacing
        place(f"/World/A_{i:03d}", aid, (x, y, 0), kinematic=True)
        pos[aid] = (x, y)
    cam, op = camera("/World/Cam", (0, -1, 0.5), (0, 0, 0), focal=24.0)
    rp = rep.create.render_product(str(cam.GetPath()), (400, 400))
    ann = rep.AnnotatorRegistry.get_annotator("rgb")
    ann.attach([rp])
    t1 = time.time()
    for _ in range(60):
        app.update()
    print(f"[render] warmup {time.time() - t1:.1f}s", flush=True)
    tiles = []
    hfov = 2 * math.atan(20.955 / 2 / 24.0)
    for aid in ids:
        bx, by, bz = index[aid]["bbox_m"]
        x, y = pos[aid]
        r = 0.5 * math.sqrt(bx * bx + by * by + bz * bz)
        d = r / math.sin(hfov / 2) * 1.05
        el = math.radians(12)
        eye = (x, y - d * math.cos(el), bz / 2 + d * math.sin(el))
        views = (0, 90, 180, 270) if VIEWS else (0,)
        for az in views:  # az=0: camera at -Y (front); 90: at +X; 180: at +Y; 270: at -X
            a = math.radians(az)
            dx, dy = math.sin(a), -math.cos(a)
            eye = (x + dx * d * math.cos(el), y + dy * d * math.cos(el), bz / 2 + d * math.sin(el))
            set_cam(op, eye, (x, y, bz / 2))
            for _ in range(2):
                app.update()
            img = grab(ann, steps=1, sub=8)
            if VIEWS:
                tiles.append((f"{aid} @{['-Y front', '+X', '+Y back', '-X'][az // 90]}", img))
        if VIEWS:
            continue
        if len(tiles) < 0:
            from pxr import Usd

            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
            prim = stage.GetPrimAtPath(f"/World/A_{ids.index(aid):03d}")
            print("[render] dbg", aid, eye, bc.ComputeWorldBound(prim).ComputeAlignedRange(), img.mean(), flush=True)
        tiles.append((aid, img))
    per = 32 if VIEWS else 30
    f = font(15)
    for s in range(0, len(tiles), per):
        chunk = tiles[s : s + per]
        c = 8 if VIEWS else 6
        rows = int(math.ceil(len(chunk) / c))
        sheet = Image.new("RGB", (c * 400, rows * 440), (20, 20, 20))
        d = ImageDraw.Draw(sheet)
        for k, (aid, img) in enumerate(chunk):
            X, Y = (k % c) * 400, (k // c) * 440
            sheet.paste(Image.fromarray(img), (X, Y))
            bb = index[aid.split(" @")[0]]["bbox_m"]
            d.text((X + 6, Y + 403), aid[:52], fill=(255, 255, 255), font=f)
            d.text(
                (X + 6, Y + 421),
                f"{index[aid.split(' @')[0]]['form']}  {bb[0] * 100:.1f}x{bb[1] * 100:.1f}x{bb[2] * 100:.1f} cm",
                fill=(180, 220, 255),
                font=f,
            )
        fn = f"{OUT}/{'views' if VIEWS else 'sheet'}_{s // per:02d}.jpg"
        sheet.save(fn, quality=82)
        print(f"[render] wrote {fn}", flush=True)


# ------------------------------------------------------------------------------------------------ shelf
SHELF_SKUS = [  # (sku, facings, rows deep) by shelf level (0 = bottom)
    [("FLOUR-AP", 2, 2), ("COLA", 2, 2), ("DISH-SOAP", 2, 2), ("BANANAS", 1, 2), ("LETTUCE-ROMAINE", 2, 2)],
    [("CEREAL-OATS", 2, 2), ("MILK-2PCT", 2, 2), ("OJ", 2, 2), ("CHIPS-TORTILLA", 1, 2), ("BREAD-WHEAT", 1, 2)],
    [
        ("SALSA-MILD", 3, 2),
        ("PEANUT-BUTTER", 2, 2),
        ("OLIVE-OIL", 2, 2),
        ("HOTSAUCE-RED", 3, 2),
        ("MAC-CHEESE", 2, 2),
        ("YOGURT-GREEK", 2, 2),
        ("CHOC-DARK", 3, 2),
    ],
    [
        ("BEANS-BLACK", 3, 2),
        ("TUNA", 3, 2),
        ("AVOCADO", 2, 2),
        ("LIMES", 3, 2),
        ("ONION-WHITE", 2, 2),
        ("PEPPER-RED", 2, 2),
        ("JALAPENO", 2, 2),
    ],
]
SHELF_W, SHELF_D, BOARD_T = 1.6, 0.45, 0.025
LEVELS = [0.10, 0.48, 0.86, 1.24]


def unit_scale(sku, aid):
    e = sku_map.get(sku, {})
    if "scale" in e:
        return e["scale"], e.get("yaw_deg", 0.0)
    return 1.0, 0.0


def shelf():
    lights(store=True)
    wood = material("/World/Looks/Shelf", (0.82, 0.82, 0.80), 0.5)
    back = material("/World/Looks/Back", (0.55, 0.60, 0.65), 0.8)
    floor_m = material("/World/Looks/Floor", (0.62, 0.60, 0.56), 0.6)
    UsdPhysics.Scene.Define(stage, "/World/PhysicsScene").CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    box("/World/Floor", (0, -1, -0.01), (8, 8, 0.02), floor_m)
    box("/World/ShelfUnit/Back", (0, SHELF_D / 2 + 0.01, 0.85), (SHELF_W + 0.05, 0.02, 1.7), back)
    for side, x in (("L", -SHELF_W / 2 - 0.0125), ("R", SHELF_W / 2 + 0.0125)):
        box(f"/World/ShelfUnit/Side{side}", (x, 0, 0.85), (0.025, SHELF_D + 0.02, 1.7), wood)
    for i, z in enumerate(LEVELS):
        box(f"/World/ShelfUnit/Board{i}", (0, 0, z - BOARD_T / 2), (SHELF_W, SHELF_D, BOARD_T), wood)
        box(
            f"/World/ShelfUnit/Lip{i}", (0, -SHELF_D / 2 + 0.005, z + 0.012), (SHELF_W, 0.01, 0.024), wood
        )  # price rail
    units = {}
    report = []
    for lvl, row in enumerate(SHELF_SKUS):
        z = LEVELS[lvl]
        plan = []
        for sku, fac, deep in row:
            e = sku_map.get(sku)
            if not e or e.get("asset") not in index:
                report.append(f"{sku}: no asset")
                continue
            aid = e["asset"]
            s = e.get("scale", 1.0)
            yaw = e.get("yaw_deg", 0.0)
            bx, by, bz = index[aid]["bbox_m"]
            if int(round(yaw)) % 180 == 90:
                bx, by = by, bx
            w, dpt = bx * s, by * s
            plan.append((sku, aid, fac, deep, s, yaw, w, dpt, bz * s))
        total = sum(p[2] * (p[6] + 0.008) for p in plan)
        if total > SHELF_W - 0.04:
            report.append(f"WARN L{lvl} overfull: {total:.2f} m > {SHELF_W - 0.04:.2f} m")
        gap = max(0.01, (SHELF_W - 0.04 - total) / max(1, len(plan)))
        x = -SHELF_W / 2 + 0.02 + gap / 2
        for sku, aid, fac, deep, s, yaw, w, dpt, h in plan:
            for f in range(fac):
                cx = x + (w + 0.008) * (f + 0.5)
                for r in range(deep):
                    cy = -SHELF_D / 2 + 0.03 + dpt / 2 + r * (dpt + 0.01)
                    if cy + dpt / 2 > SHELF_D / 2:
                        break
                    path = f"/World/ShelfUnit/L{lvl}/{sku.replace('-', '_')}_f{f}_r{r}"
                    up = place(path, aid, (cx, cy, z + 0.002), yaw, s)
                    if sku_map[sku].get("mass_kg"):
                        UsdPhysics.MassAPI(up).CreateMassAttr(float(sku_map[sku]["mass_kg"]))
                    units[path] = (sku, aid, (cx, cy, z + 0.002))
            report.append(
                f"L{lvl} {sku:18s} {aid:44s} scale={s:.3f} yaw={yaw:5.1f} unit={w * 100:.1f}x{dpt * 100:.1f}x{h * 100:.1f}cm facings={fac} deep={deep}"
            )
            x += fac * (w + 0.008) + gap
    print("\n".join(report), flush=True)
    print(f"[render] {len(units)} unit prims", flush=True)

    front_y = -SHELF_D / 2
    cams = {
        "front": ((0, front_y - 1.0, 1.1), (0, 0, 0.78), 9.5),
        "closeup_L3": ((-0.35, front_y - 0.45, 1.40), (-0.35, 0, 1.30), 14.0),
        "closeup_L2": ((0.25, front_y - 0.45, 1.02), (0.25, 0, 0.93), 14.0),
        "closeup_L1": ((-0.3, front_y - 0.55, 0.68), (-0.3, 0, 0.62), 14.0),
        "angle_L2": ((-0.75, front_y - 0.35, 1.05), (-0.2, 0, 0.92), 14.0),
    }
    rps, anns = {}, {}
    for name, (eye, tgt, foc) in cams.items():
        cam, _ = camera(f"/World/Cam_{name}", eye, tgt, focal=foc)
        rps[name] = rep.create.render_product(str(cam.GetPath()), (1280, 960))
        anns[name] = rep.AnnotatorRegistry.get_annotator("rgb")
        anns[name].attach([rps[name]])
    t1 = time.time()
    for _ in range(40):
        app.update()
    print(f"[render] warmup {time.time() - t1:.1f}s", flush=True)
    for name in cams:
        Image.fromarray(grab(anns[name], steps=2, sub=12)).save(f"{OUT}/shelf_{name}_t0.jpg", quality=90)

    # physics settle: every unit is a dynamic rigid body; they must stay put on the boards
    tl = omni.timeline.get_timeline_interface()
    tl.play()
    t1 = time.time()
    for _ in range(180):
        app.update()
    print(f"[render] physics 180 updates in {time.time() - t1:.1f}s", flush=True)
    moved = []
    try:
        from omni.physx import get_physx_interface

        px = get_physx_interface()
        for path, (sku, aid, p0) in units.items():
            tr = px.get_rigidbody_transformation(path)
            if not tr or not tr.get("ret_val", True):
                continue
            p = tr["position"]
            dz, dxy = p[2] - p0[2], math.hypot(p[0] - p0[0], p[1] - p0[1])
            if abs(dz) > 0.03 or dxy > 0.05:
                moved.append((path, round(dz, 3), round(dxy, 3)))
    except Exception as ex:  # noqa: BLE001
        print(f"[render] physx readback failed: {ex!r}", flush=True)
    tl.pause()
    print(f"[render] units moved >3cm/5cm after settle: {len(moved)} / {len(units)}", flush=True)
    for m in moved[:40]:
        print("   ", m, flush=True)
    for name in cams:
        Image.fromarray(grab(anns[name], steps=2, sub=12)).save(f"{OUT}/shelf_{name}.jpg", quality=90)
        print(f"[render] wrote {OUT}/shelf_{name}.jpg", flush=True)
    json.dump({"units": len(units), "moved": moved, "report": report}, open(f"{OUT}/shelf_report.json", "w"), indent=1)
    stage.GetRootLayer().Export(f"{OUT}/test_shelf.usda")


if MODE in ("sheets", "views"):
    sheets()
else:
    shelf()
print(f"[render] total {time.time() - t0:.1f}s", flush=True)
app.close()
