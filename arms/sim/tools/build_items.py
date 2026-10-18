# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Build normalized per-item USDs + index.yaml for Picky grocery product assets.

Run on vhagar with the products venv (usd-core, numpy, pyyaml), from the repo (arms/sim/ASSETS.md):
  $PICKY_ASSET_ROOT/.venv/bin/python -I arms/sim/tools/build_items.py [--convert] [asset_id ...]
Env: PICKY_ASSET_ROOT (asset tree, default /data/$USER/picky-assets/products), ROBOCASA_KITCHEN_OBJECTS (RoboCasa's
kitchen_objects.py for the category scales, default $PICKY_ASSET_ROOT/_tools/kitchen_objects.py).

For every source asset it writes items/<asset_id>/:
  geom.usda  /Geom: references the source asset, normalized to Z-up, meters, origin at bottom center
             (bbox of render geometry). SimReady overrides: internal rigid bodies removed, joints deactivated,
             SDF colliders -> convexHull (cheap for ~2k units).
  item.usda  /Item (defaultPrim, kind=component): PhysicsRigidBodyAPI + PhysicsMassAPI on the unit root,
             child /Item/Geom (instanceable) -> geom.usda. One shelf unit = one reference to item.usda.
and writes index.yaml {asset_id: {usd, geom, source, source_usd, license, attribution, form, bbox_m, ...}}.
"""

import glob
import json
import os
import re
import subprocess
import sys
import concurrent.futures as cf

import yaml
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

R = os.environ.get("PICKY_ASSET_ROOT") or f"/data/{os.environ.get('USER', 'advaiv2')}/picky-assets/products"
T = os.path.dirname(os.path.abspath(__file__))  # this tools dir: poses.yaml, obj2usd.py
ITEMS = f"{R}/items"
PY = os.environ.get("PICKY_ASSET_PY") or f"{R}/.venv/bin/python"
KITCHEN_OBJECTS = os.environ.get("ROBOCASA_KITCHEN_OBJECTS") or f"{R}/_tools/kitchen_objects.py"
POSES = (yaml.safe_load(open(f"{T}/poses.yaml")) or {}) if os.path.exists(f"{T}/poses.yaml") else {}

SOURCES = {
    "simready": {
        "license": "CC-BY-4.0",
        "attribution": "NVIDIA SimReady Assets (huggingface.co/datasets/nvidia/simready-assets), (c) NVIDIA, CC BY 4.0",
    },
    "robocasa": {
        "license": "CC-BY-4.0",
        "attribution": "RoboCasa365 object library, AI-generated objects (robocasa.ai; Nasiriany et al. 2024), CC BY 4.0",
    },
    "lightwheel": {
        "license": "CC-BY-4.0",
        "attribution": "Lightwheel kitchen objects via NVIDIA PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF "
        "(huggingface.co/datasets/nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF), CC BY 4.0",
    },
}

BRAND_RISK = {  # source art that resembles real brands: retexture before any public demo
    "sr_processed_boxedFood_j01": "Dr. Oetker logo",
    "sr_processed_boxed_cereal_a01": "Nesquik name",
    "sr_beverage_bottledDrink_a01": "Coca-Cola contour bottle + script",
    "sr_processed_condiment_a01": "Sweet Baby Ray's-like label",
    "sr_processed_ketchup_b01": "Heinz-like keystone label",
    "sr_processed_chips_d01": "'Lysa' (Lay's-like)",
    "sr_processed_chips_e01": "'Lysa' (Lay's-like)",
    "sr_processed_chips_j01": "'Lysa' (Lay's-like)",
    "sr_processed_chips_m01": "'Lysa' (Lay's-like)",
    "sr_processed_chips_o01": "'Doradoss' (Doritos-like)",
    "sr_canned_beans_a01": "'Monte Delight' (Del Monte-like)",
    "sr_canned_beans_a02": "'Monte Delight' (Del Monte-like)",
    "sr_canned_beans_a03": "'Monte Delight' (Del Monte-like)",
}

FORMS = [  # (regex on source id, form) first match wins
    (r"canned|cannedFood|tomatoSoup|beverage_can_", "can"),
    (r"peanut_butter_jar|jam_|salsa|_jar|^jar", "jar"),
    (
        r"bottledDrink|bottledWater|beerBottle|champagne|ginBottle|vodka|whisky|rum|tequila|olive_oil|vinegar|canola|honey|syrup|condiment|ketchup|mayonnaise|mustard|soapDispenser|dishSoap",
        "bottle",
    ),
    (r"milk|boxedDrink|juice", "carton"),
    (r"chips|bagged_food|pasta_spaghetti|flour_bag|bread_loaf|bread_rolls|sandwich_bread", "bag"),
    (r"boxedFood|boxed_cereal|boxed_food|spaghetti_box|mix_brownie|mix_cake|aluminum_foil", "box"),
    (r"yogurt|ice_cream|tupperware", "tub"),
    (r"box_close|box_open|tray", "clamshell_tray"),
    (r"banana_bunch|lettuce|celery|bocChoy|broccoli", "produce_bunch_or_head"),
    (
        r"avocado|lime|lemon|onion|tomato|bellPepper|apple|potato|garlic|carrot|strawberry|banana|chili_pepper|cucumber",
        "produce",
    ),
    (r"chicken_breast|steak|bacon|pork_loin|sausage|salami|ham_|shrimp|fish_|turkey_slice", "meat_seafood"),
    (r"cheese|butter_stick|cream_cheese|egg_|egg$|^egg|tofu", "dairy_deli"),
    (r"bread|bagel|baguette|cupcake|hotdog_bun|danish", "bakery"),
    (r"paperTowel", "paper_roll"),
    (r"shaker|paprika|cinnamon|turmeric|salt_and_pepper", "shaker"),
    (r"chocolate|_bar_|prepared_bar", "bar"),
]


def form_of(src_id):
    for rx, f in FORMS:
        if re.search(rx, src_id):
            return f
    return "other"


def rc_category_scales():
    src = open(KITCHEN_OBJECTS).read()
    cats = re.findall(r"^    ([a-z0-9_]+)=dict\(", src, re.M)
    out = {}
    for i, c in enumerate(cats):
        a = src.index(f"\n    {c}=dict(")
        b = src.index(f"\n    {cats[i + 1]}=dict(") if i + 1 < len(cats) else len(src)
        m = re.search(r"aigen=dict\((.*?)\)", src[a:b], re.S)
        s = re.search(r"scale=([0-9.]+)", m.group(1)) if m else None
        out[c] = float(s.group(1)) if s else 1.0
    out["peanut_butter_jar"] = out.get("peanut_butter", 1.0)
    return out


def convert_all():
    jobs = []
    scales = rc_category_scales()
    for x in sorted(glob.glob(f"{R}/robocasa/aigen_objs/*/*/model.xml")):
        cat, obj = x.split("/")[-3], x.split("/")[-2]
        jobs.append((x, f"{R}/converted/rc_{obj}", scales.get(cat, 1.0)))
    for x in sorted(glob.glob(f"{R}/lightwheel/*/*/model.xml")):
        cat, obj = x.split("/")[-3], x.split("/")[-2]
        jobs.append((x, f"{R}/converted/lw_{cat}_{obj.lower()}", 1.0))

    def run(j):
        x, out, s = j
        if os.path.isfile(f"{out}/geom.usd"):
            return f"skip {out}"
        p = subprocess.run([PY, "-I", f"{T}/obj2usd.py", x, out, "--scale", str(s)], capture_output=True, text=True)
        return p.stdout.strip() or p.stderr.strip()[-400:]

    with cf.ThreadPoolExecutor(8) as ex:
        for line in ex.map(run, jobs):
            print(line, flush=True)


def discover():
    assets = {}
    for d in sorted(glob.glob(f"{R}/simready/*/simready_usd")):
        sid = d.split("/")[-2]
        roots = sorted(glob.glob(f"{d}/sm_{sid}_01.usd*")) or sorted(glob.glob(f"{d}/sm_*.usd*"))
        if not roots:
            continue
        aid = "sr_" + re.sub(r"^food_", "", sid)
        assets[aid] = {"source": "simready", "source_id": sid, "source_usd": roots[0]}
    for g in sorted(glob.glob(f"{R}/converted/*/geom.usd")):
        name = g.split("/")[-2]
        src = "robocasa" if name.startswith("rc_") else "lightwheel"
        sid = name[3:]
        assets[name] = {"source": src, "source_id": sid, "source_usd": g}
    return assets


def stage_bbox(stage, path):
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    rng = bc.ComputeWorldBound(stage.GetPrimAtPath(path)).ComputeAlignedRange()
    return rng


def build_one(aid, a):
    out = f"{ITEMS}/{aid}"
    os.makedirs(out, exist_ok=True)
    src = a["source_usd"]
    rel = os.path.relpath(src, out)
    s0 = Usd.Stage.Open(src, load=Usd.Stage.LoadAll)
    up = UsdGeom.GetStageUpAxis(s0)
    mpu = UsdGeom.GetStageMetersPerUnit(s0)
    del s0

    gpath = f"{out}/geom.usda"
    if os.path.exists(gpath):
        os.remove(gpath)
    st = Usd.Stage.CreateNew(gpath)
    UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(st, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(st, 1.0)
    geom = UsdGeom.Xform.Define(st, "/Geom")
    st.SetDefaultPrim(geom.GetPrim())
    srcp = UsdGeom.Xform.Define(st, "/Geom/Src")
    srcp.GetPrim().GetReferences().AddReference(rel)
    if a["source"] == "simready":
        vs = srcp.GetPrim().GetVariantSets()
        if vs.HasVariantSet("PhysX"):
            vs.GetVariantSet("PhysX").SetVariantSelection("Enabled")
    # unit/axis conversion on /Geom/Src (does not touch the referenced root's own xformOps: separate prim)
    n_removed_rb = n_hull = n_joints = 0
    if a["source"] == "simready":
        # expose instance internals so collider approximations can be overridden in this layer
        for p in list(st.Traverse()):
            pass
        changed = True
        while changed:
            changed = False
            for p in st.Traverse():
                if p.IsInstance():
                    p.SetInstanceable(False)
                    changed = True
        for p in st.Traverse():
            if p.HasAPI(UsdPhysics.RigidBodyAPI):
                p.RemoveAPI(UsdPhysics.RigidBodyAPI)
                n_removed_rb += 1
            if p.IsA(UsdPhysics.Joint):
                p.SetActive(False)
                n_joints += 1
            if p.HasAPI(UsdPhysics.MeshCollisionAPI):
                UsdPhysics.MeshCollisionAPI(p).GetApproximationAttr().Set("convexHull")
                if "PhysxSDFMeshCollisionAPI" in p.GetAppliedSchemas():
                    p.RemoveAppliedSchema("PhysxSDFMeshCollisionAPI")
                n_hull += 1
            elif p.HasAPI(UsdPhysics.CollisionAPI) and p.IsA(UsdGeom.Mesh):
                UsdPhysics.MeshCollisionAPI.Apply(p).CreateApproximationAttr("convexHull")
                n_hull += 1
    xf = UsdGeom.Xformable(srcp)
    pose = POSES.get(aid) or []
    for k, (ax, deg) in enumerate(reversed(pose)):  # outermost first: points get pose[0] first
        getattr(xf, f"AddRotate{ax.upper()}Op")(opSuffix=f"pose{len(pose) - 1 - k}").Set(float(deg))
    if up == UsdGeom.Tokens.y:
        xf.AddRotateXOp().Set(90.0)
    if abs(mpu - 1.0) > 1e-9:
        xf.AddScaleOp().Set(Gf.Vec3f(mpu, mpu, mpu))
    rng = stage_bbox(st, "/Geom")
    lo, hi = rng.GetMin(), rng.GetMax()
    t = Gf.Vec3d(-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2])
    UsdGeom.Xformable(geom).AddTranslateOp().Set(t)
    st.GetRootLayer().Save()
    rng2 = stage_bbox(st, "/Geom")
    size = rng2.GetMax() - rng2.GetMin()

    ipath = f"{out}/item.usda"
    if os.path.exists(ipath):
        os.remove(ipath)
    it = Usd.Stage.CreateNew(ipath)
    UsdGeom.SetStageUpAxis(it, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(it, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(it, 1.0)
    item = UsdGeom.Xform.Define(it, "/Item")
    it.SetDefaultPrim(item.GetPrim())
    Usd.ModelAPI(item.GetPrim()).SetKind("component")
    UsdPhysics.RigidBodyAPI.Apply(item.GetPrim())
    vol = max(size[0] * size[1] * size[2], 1e-6)
    mass = float(min(max(vol * 350.0, 0.02), 10.0))  # placeholder; scene builder sets products.yaml mass_kg
    UsdPhysics.MassAPI.Apply(item.GetPrim()).CreateMassAttr(mass)
    g = it.DefinePrim("/Item/Geom", "Xform")
    g.GetReferences().AddReference("./geom.usda")
    g.SetInstanceable(True)
    item.GetPrim().SetCustomDataByKey("picky:asset_id", aid)
    item.GetPrim().SetCustomDataByKey("picky:source", a["source"])
    it.GetRootLayer().Save()

    return {
        "usd": ipath,
        "geom": gpath,
        "source": a["source"],
        "source_id": a["source_id"],
        "source_usd": src,
        "license": SOURCES[a["source"]]["license"],
        "attribution": SOURCES[a["source"]]["attribution"],
        "form": form_of(a["source_id"]),
        "bbox_m": [round(float(size[0]), 4), round(float(size[1]), 4), round(float(size[2]), 4)],
        "default_mass_kg": round(mass, 3),
        "rigid_body": "/Item",
        "src_up_axis": str(up),
        "src_meters_per_unit": float(mpu),
        "src_offset_m": [round(float(x), 4) for x in t],
        "pose": [[ax, float(deg)] for ax, deg in pose],
        "physics_fixups": {
            "removed_rigid_bodies": n_removed_rb,
            "convex_hull_colliders": n_hull,
            "deactivated_joints": n_joints,
        },
    }


def main():
    if "--convert" in sys.argv:
        convert_all()
    assets = discover()
    index, errors = {}, {}
    only = [x for x in sys.argv[1:] if not x.startswith("--")]
    for aid, a in assets.items():
        if only and aid not in only:
            continue
        try:
            index[aid] = build_one(aid, a)
            print(aid, index[aid]["form"], index[aid]["bbox_m"], index[aid]["physics_fixups"], flush=True)
        except Exception as ex:
            errors[aid] = repr(ex)[:300]
            print("ERR", aid, errors[aid], flush=True)
    sku_path = f"{R}/sku_map.yaml"
    if os.path.exists(sku_path) and not only:
        sku_map = yaml.safe_load(open(sku_path)) or {}
        for sku, e in sku_map.items():
            a = index.get(e["asset"])
            if a is None:
                continue
            a.setdefault("skus", []).append(sku)
            if e.get("mass_kg") is not None:
                a.setdefault("_masses", []).append(float(e["mass_kg"]))
        for a in index.values():
            m = a.pop("_masses", None)
            if m:
                a["mass_kg"] = round(sorted(m)[len(m) // 2], 3)
            if "skus" in a:
                a["front_checked"] = True  # front view rendered + inspected (_renders/sheet_*.jpg)
    for aid, note in BRAND_RISK.items():
        if aid in index:
            index[aid]["brand_risk"] = note
    doc = {
        "schema": "picky grocery product assets v1",
        "root": R,
        "conventions": "Z-up, meters, kg; origin at bottom center of the render bbox; shelf pose baked in (upright, front label toward -Y "
        "for assets with front_checked: true, i.e. every asset used in sku_map.yaml). One shelf unit = one prim referencing <usd> "
        "(defaultPrim /Item = PhysicsRigidBodyAPI + MassAPI; /Item/Geom instanceable). bbox_m is at scale 1; sku_map.yaml gives per-SKU scale + mass. "
        "default_mass_kg is a volume guess; mass_kg (when present) is the products.yaml mass of the SKUs mapped to the asset.",
        "assets": index,
    }
    if errors:
        doc["errors"] = errors
    with open(f"{R}/index.yaml" if not only else f"{R}/_logs/index_partial.yaml", "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, width=160)
    print(f"wrote {len(index)} assets, {len(errors)} errors")


if __name__ == "__main__":
    main()
