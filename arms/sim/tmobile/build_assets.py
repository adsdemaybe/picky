# ruff: noqa  -- standalone asset-pipeline script (GPU host, asset venv: usd-core trimesh numpy pillow pyyaml scipy)
"""Build the T-Mobile store's normalized USD items + index.yaml under $TMO_ASSET_ROOT (see README.md here).

  $TMO_ASSET_ROOT/.venv/bin/python -I arms/sim/tmobile/build_assets.py [--devices] [--packages] [--sheet] [id ...]

--devices   raw/<model_id>/ (glb / gso obj / Poly Haven gltf, fetch_models.py) -> items/dev_<model_id>/
            geom.usda: /Geom with the meshes baked in (glTF Y-up -> Z-up, meters, models.yaml `pose` rotations,
                       origin at the bottom center of the bbox, front / screen toward -Y), UsdPreviewSurface materials
                       (base color texture or factor, metallic, roughness, opacity), textures in ./textures/, and
                       /Geom/Collision: one convex hull (purpose guide, CollisionAPI + MeshCollisionAPI convexHull).
            item.usda: /Item (defaultPrim, kind component, RigidBodyAPI + MassAPI) + /Item/Geom (instanceable ref).
--packages  arms/sim/tmobile/sku_plan.yaml + data/products.yaml -> items/pkg_<SKU>/ (same two files): the SKU's
            retail package at its exact products.yaml dims_m (no runtime scale) in its label color; the front label
            stays the worker's decal (arms.sim.labels.label_png), so perception reads the same artwork.
--sheet     renders/sheet_devices.jpg: each device model from the front (-Y), back (+Y) and right side (+X), flat
            shaded with texture colors (Pillow rasterizer; no Isaac), to check the models.yaml poses.
index.yaml lists every item: usd, bbox_m (real size), source, license, attribution, faces.
"""

import glob
import math
import os
import shutil
import sys

import numpy as np
import yaml
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(f"{HERE}/../../..")
ROOT = os.environ.get("TMO_ASSET_ROOT") or f"/data/{os.environ.get('USER', 'advaiv2')}/picky-assets/tmobile"
ITEMS = f"{ROOT}/items"
MAX_TEX = 2048
HANG_TAB_M = 0.03  # header tab height of hang boxes / sleeves / blister cards (sku_plan.yaml)
SLOT_W_M, SLOT_H_M, SLOT_FROM_TOP_M = 0.03, 0.008, 0.012  # euro slot (hook hole), center 12 mm below the top


def load_yaml(p):
    with open(p) as f:
        return yaml.safe_load(f)


# ----------------------------------------------------------------------------------------------------------- devices
def rot(axis, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


YUP_TO_ZUP = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float)  # (x, y, z) -> (x, -z, y)


def raw_model_path(mid):
    d = f"{ROOT}/raw/{mid}"
    for name in ("model.glb", "model.gltf", "model.obj"):
        if os.path.isfile(f"{d}/{name}"):
            return f"{d}/{name}"
    raise FileNotFoundError(d)


def load_parts(mid, e):
    """-> list of (V (n,3) Z-up, F (m,3), N (n,3) or None, UV (n,2) or None, material dict)."""
    import trimesh

    path = raw_model_path(mid)
    scene = trimesh.load(path, force="scene", process=False)
    up = (e.get("up") or ("z" if path.endswith(".obj") else "y")).lower()
    R = YUP_TO_ZUP if up == "y" else np.eye(3)
    for ax, deg in e.get("pose") or []:
        R = rot(ax, float(deg)) @ R
    drop = set(e.get("drop_meshes") or [])
    parts = []
    for node in scene.graph.nodes_geometry:
        T, gname = scene.graph[node]
        g = scene.geometry[gname]
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        if gname in drop or node in drop:
            continue
        V = trimesh.transform_points(np.asarray(g.vertices, dtype=float), T) @ R.T
        N = None
        try:
            n = np.asarray(g.vertex_normals, dtype=float) @ (R @ T[:3, :3]).T
            N = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        except Exception:
            pass
        uv = getattr(g.visual, "uv", None)
        UV = np.asarray(uv, dtype=float) if uv is not None and len(uv) == len(V) else None
        parts.append((V, np.asarray(g.faces, dtype=np.int64), N, UV, material_of(g), f"{node}"))
    return parts


def _rgba(c, default=(200, 200, 200, 255)):
    if c is None:
        return np.array(default, dtype=float) / 255.0
    c = np.asarray(c, dtype=float).flatten()
    if c.max() > 1.0:
        c = c / 255.0
    if len(c) == 3:
        c = np.append(c, 1.0)
    return c


def material_of(g):
    vis = g.visual
    m = {"color": np.array([0.8, 0.8, 0.8, 1.0]), "tex": None, "metallic": 0.0, "roughness": 0.5, "emissive": None}
    if getattr(vis, "kind", None) == "vertex" or vis.__class__.__name__ == "ColorVisuals":
        try:
            vc = np.asarray(vis.vertex_colors, dtype=float)
            m["color"] = _rgba(vc.mean(axis=0))
        except Exception:
            pass
        return m
    mat = getattr(vis, "material", None)
    if mat is None:
        return m
    m["key"] = id(mat)
    if mat.__class__.__name__ == "PBRMaterial":
        m["color"] = _rgba(mat.baseColorFactor, (255, 255, 255, 255))
        m["tex"] = mat.baseColorTexture
        m["metallic"] = float(mat.metallicFactor) if mat.metallicFactor is not None else 0.0
        m["roughness"] = float(mat.roughnessFactor) if mat.roughnessFactor is not None else 0.5
        if mat.emissiveTexture is not None:
            m["emissive"] = mat.emissiveTexture
        m["name"] = mat.name
    else:  # SimpleMaterial (OBJ mtl)
        m["color"] = _rgba(getattr(mat, "diffuse", None), (255, 255, 255, 255))
        m["tex"] = getattr(mat, "image", None)
        m["name"] = getattr(mat, "name", None)
    return m


def save_tex(img, out_dir, name):
    from PIL import Image

    os.makedirs(f"{out_dir}/textures", exist_ok=True)
    img = img.convert("RGBA") if img.mode in ("P", "LA", "RGBA") else img.convert("RGB")
    if max(img.size) > MAX_TEX:
        s = MAX_TEX / max(img.size)
        img = img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))), Image.LANCZOS)
    if img.mode == "RGBA" and img.getextrema()[3][0] >= 250:
        img = img.convert("RGB")
    rel = f"textures/{name}.png"
    img.save(f"{out_dir}/{rel}")
    return f"./{rel}", img


def make_material(stage, path, m, out_dir, idx):
    mat = UsdShade.Material.Define(stage, path)
    sh = UsdShade.Shader.Define(stage, f"{path}/Shader")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(np.clip(m["metallic"], 0, 1)))
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(np.clip(m["roughness"], 0.05, 1)))
    alpha = float(m["color"][3])
    if m["tex"] is not None:
        rel, img = save_tex(m["tex"], out_dir, f"base_{idx}")
        rd = UsdShade.Shader.Define(stage, f"{path}/stReader")
        rd.CreateIdAttr("UsdPrimvarReader_float2")
        rd.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
        tx = UsdShade.Shader.Define(stage, f"{path}/BaseTex")
        tx.CreateIdAttr("UsdUVTexture")
        tx.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(rel)
        tx.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(rd.ConnectableAPI(), "result")
        tx.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
        tx.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
        c = m["color"]
        tx.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(float(c[0]), float(c[1]), float(c[2]), 1.0))
        tx.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tx.ConnectableAPI(), "rgb")
    else:
        c = m["color"]
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(float(c[0]), float(c[1]), float(c[2])))
    if m.get("emissive") is not None:
        rel, _ = save_tex(m["emissive"], out_dir, f"emis_{idx}")
        tx = UsdShade.Shader.Define(stage, f"{path}/EmisTex")
        tx.CreateIdAttr("UsdUVTexture")
        tx.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(rel)
        rd = UsdShade.Shader.Get(stage, f"{path}/stReader") or UsdShade.Shader.Define(stage, f"{path}/stReader")
        if not rd.GetInput("varname"):
            rd.CreateIdAttr("UsdPrimvarReader_float2")
            rd.CreateInput("varname", Sdf.ValueTypeNames.String).Set("st")
        tx.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(rd.ConnectableAPI(), "result")
        tx.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        sh.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tx.ConnectableAPI(), "rgb")
    if alpha < 0.95:
        sh.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(max(alpha, 0.15))
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    return mat


def new_stage(path):
    if os.path.exists(path):
        os.remove(path)
    st = Usd.Stage.CreateNew(path)
    UsdGeom.SetStageUpAxis(st, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(st, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(st, 1.0)
    return st


def hull_mesh(stage, path, pts):
    from scipy.spatial import ConvexHull

    pts = np.asarray(pts, dtype=float)
    if len(pts) > 20000:
        pts = pts[np.random.default_rng(0).choice(len(pts), 20000, replace=False)]
    h = ConvexHull(pts)
    used = np.unique(h.simplices)
    remap = {int(v): i for i, v in enumerate(used)}
    m = UsdGeom.Mesh.Define(stage, path)
    m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*map(float, pts[v])) for v in used]))
    m.CreateFaceVertexCountsAttr([3] * len(h.simplices))
    m.CreateFaceVertexIndicesAttr([remap[int(v)] for tri in h.simplices for v in tri])
    m.CreatePurposeAttr(UsdGeom.Tokens.guide)
    UsdPhysics.CollisionAPI.Apply(m.GetPrim())
    UsdPhysics.MeshCollisionAPI.Apply(m.GetPrim()).CreateApproximationAttr("convexHull")
    return len(used)


def write_item(out, aid, size, mass, extra=None):
    it = new_stage(f"{out}/item.usda")
    item = UsdGeom.Xform.Define(it, "/Item")
    it.SetDefaultPrim(item.GetPrim())
    Usd.ModelAPI(item.GetPrim()).SetKind("component")
    UsdPhysics.RigidBodyAPI.Apply(item.GetPrim())
    UsdPhysics.MassAPI.Apply(item.GetPrim()).CreateMassAttr(float(mass))
    g = it.DefinePrim("/Item/Geom", "Xform")
    g.GetReferences().AddReference("./geom.usda")
    g.SetInstanceable(True)
    item.GetPrim().SetCustomDataByKey("picky:asset_id", aid)
    for k, v in (extra or {}).items():
        item.GetPrim().SetCustomDataByKey(f"picky:{k}", v)
    it.GetRootLayer().Save()


def build_device(mid, e):
    aid = f"dev_{mid}"
    out = f"{ITEMS}/{aid}"
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    parts = load_parts(mid, e)
    allv = np.concatenate([p[0] for p in parts])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    off = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    st = new_stage(f"{out}/geom.usda")
    geom = UsdGeom.Xform.Define(st, "/Geom")
    st.SetDefaultPrim(geom.GetPrim())
    UsdGeom.Scope.Define(st, "/Geom/Looks")
    UsdGeom.Xform.Define(st, "/Geom/Visual")
    mats, nfaces = {}, 0
    for i, (V, F, N, UV, m, name) in enumerate(parts):
        key = m.get("key", f"anon{i}")
        if key not in mats:
            mats[key] = make_material(st, f"/Geom/Looks/M{len(mats)}", m, out, len(mats))
        mesh = UsdGeom.Mesh.Define(st, f"/Geom/Visual/part_{i:03d}")
        P = V + off
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(P.astype(np.float32)))
        mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(F)))
        mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(F.flatten().astype(np.int32)))
        mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        mesh.CreateDoubleSidedAttr(True)
        if N is not None:
            mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(N.astype(np.float32)))
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        if UV is not None:
            pv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
            pv.Set(Vt.Vec2fArray.FromNumpy(UV.astype(np.float32)))
        c = m["color"]
        mesh.CreateDisplayColorAttr([Gf.Vec3f(float(c[0]), float(c[1]), float(c[2]))])
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mats[key])
        nfaces += len(F)
    nh = hull_mesh(st, "/Geom/Collision", allv + off)
    st.GetRootLayer().Save()
    size = (hi - lo).tolist()
    vol = max(size[0] * size[1] * size[2], 1e-6)
    write_item(out, aid, size, min(max(vol * 400.0, 0.02), 2.0), {"source_model": mid})
    return aid, {
        "usd": f"items/{aid}/item.usda",
        "geom": f"items/{aid}/geom.usda",
        "kind": e["kind"],
        "model_id": mid,
        "bbox_m": [round(float(x), 4) for x in size],
        "faces": int(nfaces),
        "hull_points": int(nh),
        "materials": len(mats),
        "pose": e.get("pose") or [],
        "source": e["source"],
        "title": e["title"],
        "author": e["author"],
        "page_url": e["page_url"],
        "license": e["license"],
        "attribution": f"'{e['title']}' by {e['author']} ({e['page_url']}), {e['license']}; normalized by the Picky team",
    }


# ---------------------------------------------------------------------------------------------------------- packages
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def shade(rgb, f):
    return tuple(min(1.0, max(0.0, c * f)) for c in rgb)


class Pkg:
    def __init__(self, stage):
        self.st = stage
        self.mats = {}
        self.n = 0

    def mat(self, rgb, rough=0.55, metallic=0.0, opacity=1.0):
        key = (tuple(round(c, 3) for c in rgb), rough, metallic, opacity)
        if key not in self.mats:
            p = f"/Geom/Looks/M{len(self.mats)}"
            mat = UsdShade.Material.Define(self.st, p)
            sh = UsdShade.Shader.Define(self.st, f"{p}/Shader")
            sh.CreateIdAttr("UsdPreviewSurface")
            sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb))
            sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
            sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)
            if opacity < 1.0:
                sh.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
            mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
            self.mats[key] = mat
        return self.mats[key]

    def box(self, name, x0, x1, y0, y1, z0, z1, rgb, **kw):
        c = UsdGeom.Cube.Define(self.st, f"/Geom/Visual/{name}")
        c.CreateSizeAttr(1.0)
        xf = UsdGeom.Xformable(c)
        xf.AddTranslateOp().Set(Gf.Vec3d((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
        xf.AddScaleOp().Set(Gf.Vec3f(x1 - x0, y1 - y0, z1 - z0))
        c.CreateDisplayColorAttr([Gf.Vec3f(*rgb)])
        UsdShade.MaterialBindingAPI.Apply(c.GetPrim()).Bind(self.mat(rgb, **kw))
        self.n += 1
        return c

    def slotted_tab(self, name, w, y0, y1, z0, z1, rgb):
        """A header tab (x in [-w/2, w/2], z in [z0, z1]) with a euro slot cut out: 4 boxes around the hole."""
        zc = z1 - SLOT_FROM_TOP_M
        hw, hh = min(SLOT_W_M, w * 0.5) / 2, SLOT_H_M / 2
        self.box(f"{name}_l", -w / 2, -hw, y0, y1, z0, z1, rgb)
        self.box(f"{name}_r", hw, w / 2, y0, y1, z0, z1, rgb)
        self.box(f"{name}_b", -hw, hw, y0, y1, z0, zc - hh, rgb)
        self.box(f"{name}_t", -hw, hw, y0, y1, zc + hh, z1, rgb)


def build_package(sku, prod, plan):
    aid = f"pkg_{sku}"
    out = f"{ITEMS}/{aid}"
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    W, D, H = (float(x) for x in prod["asset"]["dims_m"])
    rgb = hex_rgb(prod["asset"].get("label_color") or "#f4f4f2")
    form = plan["pack"]
    st = new_stage(f"{out}/geom.usda")
    geom = UsdGeom.Xform.Define(st, "/Geom")
    st.SetDefaultPrim(geom.GetPrim())
    UsdGeom.Scope.Define(st, "/Geom/Looks")
    UsdGeom.Xform.Define(st, "/Geom/Visual")
    pk = Pkg(st)
    edge = shade(rgb, 0.82)
    if form in ("pkg_phone_box", "pkg_watch_box", "pkg_audio_box"):
        lid = 0.28 if form == "pkg_phone_box" else 0.22  # 2-piece rigid box: lid overlaps the base
        i = 0.0012
        pk.box("Base", -W / 2 + i, W / 2 - i, -D / 2 + i, D / 2 - i, 0.0, H * (1 - lid) + 0.002, edge, rough=0.6)
        pk.box("Lid", -W / 2, W / 2, -D / 2, D / 2, H * (1 - lid), H, rgb, rough=0.35)
    elif form in ("pkg_shelf_box", "pkg_card_pack"):
        pk.box("Body", -W / 2, W / 2, -D / 2, D / 2, 0.0, H, rgb, rough=0.6)
        if form == "pkg_shelf_box":
            pk.box("Seam", -W / 2 - 0.0005, W / 2 + 0.0005, -0.004, 0.004, H - 0.0006, H + 0.0004, edge)
    elif form in ("pkg_hang_box", "pkg_flat_sleeve"):
        tab_y = (D / 2 - 0.0025, D / 2) if form == "pkg_hang_box" else (-D / 2, D / 2)
        pk.box("Body", -W / 2, W / 2, -D / 2, D / 2, 0.0, H - HANG_TAB_M, rgb, rough=0.5)
        pk.slotted_tab("Tab", W * (0.62 if form == "pkg_hang_box" else 1.0), tab_y[0], tab_y[1], H - HANG_TAB_M, H, rgb)
    elif form == "pkg_blister_card":
        card = 0.002
        pk.box("Card", -W / 2, W / 2, D / 2 - card, D / 2, 0.0, H - HANG_TAB_M, rgb, rough=0.55)
        pk.slotted_tab("Tab", W, D / 2 - card, D / 2, H - HANG_TAB_M, H, rgb)
        bub = shade(rgb, 1.0)
        bub = tuple(0.55 * c + 0.45 * w for c, w in zip(bub, (0.93, 0.95, 0.97)))  # white-tinted, opaque (RTX)
        pk.box("Bubble", -W * 0.36, W * 0.36, -D / 2, D / 2 - card, H * 0.06, H * 0.52, bub, rough=0.15)
    else:
        raise ValueError(f"{sku}: unknown pack {form}")
    col = UsdGeom.Cube.Define(st, "/Geom/Collision")
    col.CreateSizeAttr(1.0)
    xf = UsdGeom.Xformable(col)
    xf.AddTranslateOp().Set(Gf.Vec3d(0, 0, H / 2))
    xf.AddScaleOp().Set(Gf.Vec3f(W, D, H))
    col.CreatePurposeAttr(UsdGeom.Tokens.guide)
    UsdPhysics.CollisionAPI.Apply(col.GetPrim())
    st.GetRootLayer().Save()
    write_item(out, aid, (W, D, H), float(prod["asset"].get("mass_kg") or 0.2), {"sku": sku, "pack": form})
    return aid, {
        "usd": f"items/{aid}/item.usda",
        "geom": f"items/{aid}/geom.usda",
        "kind": "package",
        "pack": form,
        "sku": sku,
        "bbox_m": [W, D, H],
        "boxes": pk.n,
        "source": "procedural (arms/sim/tmobile/build_assets.py)",
        "license": "same as the Picky repo",
        "attribution": "Picky team (procedural)",
    }


# ------------------------------------------------------------------------------------------------------------- sheet
def sheet(ids, models):
    """Contact sheet: front (-Y), back (+Y) and right (+X) orthographic views per device model."""
    from PIL import Image, ImageDraw, ImageFont

    T = 220
    rows = []
    for mid in ids:
        parts = load_parts(mid, models[mid])
        V = np.concatenate([p[0] for p in parts])
        lo, hi = V.min(0), V.max(0)
        tiles = []
        for view in ("front", "back", "right", "top"):
            img = Image.new("RGB", (T, T), (235, 235, 240))
            dr = ImageDraw.Draw(img)
            # view basis: (u right, v up, depth toward viewer)
            if view == "front":  # camera at -Y looking +Y: u = +x, v = +z, near = small y
                U, Vv, Dp = np.array([1, 0, 0]), np.array([0, 0, 1]), np.array([0, -1, 0])
            elif view == "back":
                U, Vv, Dp = np.array([-1, 0, 0]), np.array([0, 0, 1]), np.array([0, 1, 0])
            elif view == "right":  # camera at +X looking -X: u = +y, v = +z
                U, Vv, Dp = np.array([0, 1, 0]), np.array([0, 0, 1]), np.array([1, 0, 0])
            else:  # top: camera at +Z: u = +x, v = +y
                U, Vv, Dp = np.array([1, 0, 0]), np.array([0, 1, 0]), np.array([0, 0, 1])
            ext = max(float((hi - lo) @ np.abs(U)), float((hi - lo) @ np.abs(Vv)), 1e-6)
            c = (hi + lo) / 2
            s = (T - 20) / ext
            light = np.array([0.3, -0.6, 0.75])
            light /= np.linalg.norm(light)
            polys = []
            for P, F, N, UV, m, _ in parts:
                tri = P[F]
                n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
                n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
                lam = 0.35 + 0.65 * np.abs(n @ light)
                base = np.tile(m["color"][:3], (len(F), 1))
                if m["tex"] is not None and UV is not None:
                    try:
                        im = m["tex"].convert("RGB")
                        a = np.asarray(im, dtype=float) / 255.0
                        uv = UV[F].mean(axis=1) % 1.0
                        px = np.clip((uv[:, 0] * (a.shape[1] - 1)).astype(int), 0, a.shape[1] - 1)
                        py = np.clip(((1 - uv[:, 1]) * (a.shape[0] - 1)).astype(int), 0, a.shape[0] - 1)
                        base = a[py, px] * m["color"][:3]
                    except Exception:
                        pass
                col = np.clip(base * lam[:, None], 0, 1)
                depth = tri.mean(axis=1) @ Dp
                u = (tri - c) @ U * s + T / 2
                v = T / 2 - (tri - c) @ Vv * s
                for k in range(len(F)):
                    polys.append((depth[k], [(u[k, j], v[k, j]) for j in range(3)], tuple(int(x * 255) for x in col[k])))
            polys.sort(key=lambda p: p[0])
            for _, pts, cc in polys:
                dr.polygon(pts, fill=cc)
            dr.text((4, 4), view, fill=(0, 0, 0))
            tiles.append(img)
        row = Image.new("RGB", (T * len(tiles) + 260, T), (255, 255, 255))
        for i, t in enumerate(tiles):
            row.paste(t, (i * T, 0))
        d = ImageDraw.Draw(row)
        d.text((T * len(tiles) + 6, 10), mid, fill=(0, 0, 0))
        d.text((T * len(tiles) + 6, 30), f"bbox {np.round(hi - lo, 3).tolist()}", fill=(0, 0, 0))
        d.text((T * len(tiles) + 6, 50), f"pose {models[mid].get('pose') or []}", fill=(0, 0, 0))
        rows.append(row)
        print("sheet", mid, flush=True)
    os.makedirs(f"{ROOT}/renders", exist_ok=True)
    per = 8
    for k in range(0, len(rows), per):
        chunk = rows[k : k + per]
        out = Image.new("RGB", (chunk[0].width, T * len(chunk)), (255, 255, 255))
        for i, r in enumerate(chunk):
            out.paste(r, (0, i * T))
        out.save(f"{ROOT}/renders/sheet_devices_{k // per}.jpg", quality=88)


# -------------------------------------------------------------------------------------------------------------- main
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    models = load_yaml(f"{HERE}/models.yaml")["models"]
    idx_path = f"{ROOT}/index.yaml"
    index = (load_yaml(idx_path) or {}).get("assets", {}) if os.path.exists(idx_path) else {}
    errors = {}
    if "--devices" in flags:
        for mid, e in models.items():
            if args and mid not in args:
                continue
            if e["kind"] == "fixture" and e["format"] == "polyhaven_gltf" and "--fixtures" not in flags:
                pass
            try:
                aid, entry = build_device(mid, e)
                index[aid] = entry
                print("device", aid, entry["bbox_m"], entry["faces"], flush=True)
            except Exception as ex:
                errors[mid] = repr(ex)[:300]
                print("ERR", mid, errors[mid], flush=True)
    if "--packages" in flags:
        plan = load_yaml(f"{HERE}/sku_plan.yaml")["skus"]
        prods = {p["sku"]: p for p in load_yaml(f"{REPO}/data/products.yaml")["products"]}
        for sku, p in plan.items():
            if args and sku not in args:
                continue
            try:
                aid, entry = build_package(sku, prods[sku], p)
                index[aid] = entry
            except Exception as ex:
                errors[sku] = repr(ex)[:300]
                print("ERR", sku, errors[sku], flush=True)
        print("packages", sum(1 for k in index if k.startswith("pkg_")), flush=True)
    if "--sheet" in flags:
        ids = args or [m for m, e in models.items() if e["kind"] != "fixture" or e["format"] == "glb"]
        sheet(ids, models)
    if "--devices" in flags or "--packages" in flags:
        doc = {
            "schema": "picky tmobile assets v1",
            "root": ROOT,
            "conventions": "Z-up, meters, kg; origin at the bottom center of the render bbox; front / screen toward -Y. "
            "One unit = one prim referencing <usd> (defaultPrim /Item: RigidBodyAPI + MassAPI; /Item/Geom instanceable; "
            "/Geom/Collision = convex hull or box, purpose guide). bbox_m is the real size at scale 1.",
            "assets": dict(sorted(index.items())),
        }
        if errors:
            doc["errors"] = errors
        with open(idx_path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, width=160)
        att = ["# Attribution: Picky T-Mobile store models", ""]
        for aid, a in sorted(index.items()):
            if a.get("kind") != "package":
                att.append(f"- `{aid}`: {a['attribution']}")
        with open(f"{ROOT}/ATTRIBUTION.md", "w") as f:
            f.write("\n".join(att) + "\n")
        print(f"index: {len(index)} assets, {len(errors)} errors")


if __name__ == "__main__":
    main()
