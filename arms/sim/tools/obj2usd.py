# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""MJCF (+ OBJ meshes) -> one USD per object (RoboCasa aigen / Lightwheel kitchen objects).

Usage (usd-core + numpy venv, run with python -I):
  python -I obj2usd.py <model.xml> <out_dir> [--scale S]

Writes <out_dir>/geom.usda (+ <out_dir>/textures/*):
  /Geom (Xform, defaultPrim)  Z-up, meters, origin at the bottom center of the visual bbox
    /Geom/Visual/<mesh>       UsdGeom.Mesh + UsdPreviewSurface (diffuse texture from the MJCF material)
    /Geom/Collision/part_<i>  convex-decomposition parts shipped with the object, purpose=guide,
                              PhysicsCollisionAPI + PhysicsMeshCollisionAPI(approximation=convexHull)
No rigid body here: the item wrapper (build_items.py) puts RigidBodyAPI + MassAPI on the unit root.
Vertices are baked: mesh scale (MJCF) * --scale (RoboCasa category scale), geom pos/quat, then recentered.
"""

import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

MAX_COLLISION_PARTS = 12  # more parts than this -> one convex hull of the visual mesh (cheap for ~2k units)


def parse_obj(path, want_uv=True):
    v, vt, vn = [], [], []
    fv, ft, fn, counts = [], [], [], []
    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("v "):
                v.append(line.split()[1:4])
            elif want_uv and line.startswith("vt "):
                vt.append(line.split()[1:3])
            elif want_uv and line.startswith("vn "):
                vn.append(line.split()[1:4])
            elif line.startswith("f "):
                toks = line.split()[1:]
                counts.append(len(toks))
                for t in toks:
                    parts = t.split("/")
                    fv.append(int(parts[0]))
                    ft.append(int(parts[1]) if len(parts) > 1 and parts[1] else 0)
                    fn.append(int(parts[2]) if len(parts) > 2 and parts[2] else 0)
    V = np.asarray(v, dtype=np.float64).reshape(-1, 3)
    T = np.asarray(vt, dtype=np.float64).reshape(-1, 2) if vt else None
    N = np.asarray(vn, dtype=np.float64).reshape(-1, 3) if vn else None

    def fix(idx, n):  # OBJ 1-based, negatives are relative
        a = np.asarray(idx, dtype=np.int64)
        return np.where(a > 0, a - 1, n + a)

    FV = fix(fv, len(V))
    FT = fix(ft, len(T)) if T is not None and all(ft) else None
    FN = fix(fn, len(N)) if N is not None and all(fn) else None
    return V, T, N, np.asarray(counts, dtype=np.int32), FV, FT, FN


def quat_mat(q):  # MJCF quat = w x y z
    w, x, y, z = q
    n = (w * w + x * x + y * y + z * z) ** 0.5
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def floats(s, n, default):
    if not s:
        return default
    vals = [float(x) for x in s.split()]
    return vals if len(vals) == n else default


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s if s and not s[0].isdigit() else "m_" + s


def main():
    xml_path = sys.argv[1]
    out_dir = sys.argv[2]
    extra = float(sys.argv[sys.argv.index("--scale") + 1]) if "--scale" in sys.argv else 1.0
    base = os.path.dirname(os.path.abspath(xml_path))
    root = ET.parse(xml_path).getroot()
    compiler = root.find("compiler")
    meshdir = compiler.get("meshdir", "") if compiler is not None else ""
    texdir = compiler.get("texturedir", "") if compiler is not None else ""

    meshes = {m.get("name"): m for m in root.iter("mesh")}
    textures = {t.get("name"): t for t in root.iter("texture")}
    materials = {m.get("name"): m for m in root.iter("material")}

    vis, col = [], []
    for g in root.iter("geom"):
        if g.get("type") != "mesh" or g.get("mesh") not in meshes:
            continue
        group = g.get("group")
        cls = g.get("class", "")
        if group == "1" or cls == "visual" or g.get("material"):
            vis.append(g)
        elif group == "0" or cls == "collision":
            col.append(g)

    os.makedirs(os.path.join(out_dir, "textures"), exist_ok=True)

    def load(g, uv):
        m = meshes[g.get("mesh")]
        p = os.path.join(base, meshdir, m.get("file"))
        V, T, N, C, FV, FT, FN = parse_obj(p, want_uv=uv)
        s = np.asarray(floats(m.get("scale"), 3, [1, 1, 1])) * extra
        R = quat_mat(floats(g.get("quat"), 4, [1, 0, 0, 0]))
        t = np.asarray(floats(g.get("pos"), 3, [0, 0, 0])) * extra
        V = (V * s) @ R.T + t
        if N is not None:
            N = (N / np.where(s == 0, 1, s)) @ R.T
            N /= np.linalg.norm(N, axis=1, keepdims=True).clip(1e-12)
        return V, T, N, C, FV, FT, FN

    vis_data = [(g, load(g, True)) for g in vis]
    if not vis_data:
        raise SystemExit(f"no visual mesh in {xml_path}")
    allV = np.concatenate([d[1][0][np.unique(d[1][4])] for d in vis_data])
    lo, hi = allV.min(0), allV.max(0)
    off = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])

    stage = Usd.Stage.CreateNew(os.path.join(out_dir, "geom.usd"))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(stage, 1.0)
    geom = UsdGeom.Xform.Define(stage, "/Geom")
    stage.SetDefaultPrim(geom.GetPrim())
    UsdGeom.Scope.Define(stage, "/Geom/Looks")
    UsdGeom.Xform.Define(stage, "/Geom/Visual")
    mat_cache = {}

    def material_for(g):
        mname = g.get("material")
        key = mname or "_default"
        if key in mat_cache:
            return mat_cache[key]
        mpath = f"/Geom/Looks/{safe_name(key)}"
        mat = UsdShade.Material.Define(stage, mpath)
        sh = UsdShade.Shader.Define(stage, mpath + "/Surface")
        sh.CreateIdAttr("UsdPreviewSurface")
        sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.6)
        sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
        md = materials.get(mname) if mname else None
        rgba = floats(md.get("rgba") if md is not None else None, 4, [0.8, 0.8, 0.8, 1])
        tex = textures.get(md.get("texture")) if md is not None else None
        tex_file = tex.get("file") if tex is not None else None
        src = os.path.join(base, texdir, tex_file) if tex_file else None
        if src and os.path.isfile(src):
            dst_name = safe_name(os.path.splitext(os.path.basename(src))[0]) + os.path.splitext(src)[1].lower()
            shutil.copyfile(src, os.path.join(out_dir, "textures", dst_name))
            rd = UsdShade.Shader.Define(stage, mpath + "/st")
            rd.CreateIdAttr("UsdPrimvarReader_float2")
            rd.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
            tx = UsdShade.Shader.Define(stage, mpath + "/diffuse")
            tx.CreateIdAttr("UsdUVTexture")
            tx.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(f"./textures/{dst_name}")
            tx.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
            tx.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
            tx.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
            tx.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(rd.ConnectableAPI(), "result")
            tx.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tx.ConnectableAPI(), "rgb")
        else:
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgba[:3]))
        mat_cache[key] = mat
        return mat

    for i, (g, (V, T, N, C, FV, FT, FN)) in enumerate(vis_data):
        name = safe_name(g.get("name") or g.get("mesh") or f"vis_{i}")
        mesh = UsdGeom.Mesh.Define(stage, f"/Geom/Visual/{name}")
        P = V + off
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(P.astype(np.float32)))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(C))
        mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(FV.astype(np.int32)))
        mesh.CreateSubdivisionSchemeAttr("none")
        mesh.CreateDoubleSidedAttr(False)
        used = P[np.unique(FV)]
        mesh.CreateExtentAttr([Gf.Vec3f(*used.min(0).tolist()), Gf.Vec3f(*used.max(0).tolist())])
        pv = UsdGeom.PrimvarsAPI(mesh)
        if T is not None and FT is not None:
            st = pv.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying)
            st.Set(Vt.Vec2fArray.FromNumpy(T.astype(np.float32)))
            st.SetIndices(Vt.IntArray.FromNumpy(FT.astype(np.int32)))
        if N is not None and FN is not None:
            nv = pv.CreatePrimvar("normals", Sdf.ValueTypeNames.Normal3fArray, UsdGeom.Tokens.faceVarying)
            nv.Set(Vt.Vec3fArray.FromNumpy(N.astype(np.float32)))
            nv.SetIndices(Vt.IntArray.FromNumpy(FN.astype(np.int32)))
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material_for(g))

    UsdGeom.Scope.Define(stage, "/Geom/Collision")
    parts = col if len(col) <= MAX_COLLISION_PARTS else []
    for i, g in enumerate(parts):
        V, _, _, C, FV, _, _ = load(g, False)
        m = UsdGeom.Mesh.Define(stage, f"/Geom/Collision/part_{i:02d}")
        m.CreatePointsAttr(Vt.Vec3fArray.FromNumpy((V + off).astype(np.float32)))
        m.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(C))
        m.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(FV.astype(np.int32)))
        m.CreatePurposeAttr(UsdGeom.Tokens.guide)
        UsdPhysics.CollisionAPI.Apply(m.GetPrim())
        UsdPhysics.MeshCollisionAPI.Apply(m.GetPrim()).CreateApproximationAttr("convexHull")
    if not parts:  # no shipped decomposition: hull of the visual meshes
        for prim in stage.GetPrimAtPath("/Geom/Visual").GetChildren():
            UsdPhysics.CollisionAPI.Apply(prim)
            UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr("convexHull")

    stage.GetRootLayer().customLayerData = {
        "picky_source_mjcf": os.path.relpath(xml_path, out_dir),
        "picky_bbox_m": Gf.Vec3d(*[float(x) for x in (hi - lo)]),
        "picky_collision_parts": len(parts),
    }
    stage.GetRootLayer().Save()
    print(f"{out_dir}: bbox_m={np.round(hi - lo, 4).tolist()} vis={len(vis_data)} col={len(parts)}")


if __name__ == "__main__":
    main()
