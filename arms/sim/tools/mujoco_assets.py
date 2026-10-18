"""Fetch + convert the grocery product models in data/sim/sku_assets.yaml into MuJoCo-ready textured OBJ meshes.

Same sources and asset ids as the Isaac pipeline (dl.sh / build_items.py, arms/sim/ASSETS.md), all CC-BY-4.0
(arms/sim/tools/ATTRIBUTION.md):
- ``sr_*``  NVIDIA SimReady (HF dataset nvidia/simready-assets): USD -> OBJ with ``usd-core`` (pxr).
- ``lw_*``  Lightwheel kitchen objects (HF nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF): MJCF.
- ``rc_*``  RoboCasa aigen objects (UT Box zip, selective HTTP-range extraction via rzip.py): MJCF.

Output, one folder per asset id under ``--out`` (default ``~/picky-assets-mj``; the backend reads
``PICKY_MJ_ASSET_ROOT``)::

    <asset_id>/part<k>.obj   Z-up, meters, origin at the bottom center of the whole model, with UVs
    <asset_id>/part<k>.png   its albedo texture (<= 512 px), absent when the part is a plain color
    <asset_id>/meta.json     {"bbox_m": [x, y, z], "parts": [{"obj", "png"|null, "rgba"}], "source"}

Run (the USD step needs usd-core, which is not a project dependency)::

    uv run --extra mujoco --with usd-core --with pymeshlab python arms/sim/tools/mujoco_assets.py [--jobs 8]

Nothing here is imported by the worker; it is a one-off asset build, like the other tools in this folder.
"""

import argparse
import concurrent.futures as cf
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SKU_MAP = REPO / "data/sim/sku_assets.yaml"
SR_REPO = "nvidia/simready-assets"
LW_REPO = "nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF"
RC_ZIP = "https://utexas.box.com/shared/static/nwi1vrn5pgbo95kushkasa3nx1i012ff.zip"
TEX_MAX = 512


# ---------------------------------------------------------------------------------------------------------- ids
def wanted_assets() -> dict[str, list[float] | None]:
    """asset id -> the map's native bbox (asset_bbox_m / member_dims_m hint) for every model the SKU map uses."""
    skus = yaml.safe_load(SKU_MAP.read_text())["skus"]
    out: dict[str, list[float] | None] = {}
    for v in skus.values():
        if v.get("render") == "asset" and v.get("asset_id"):
            out.setdefault(v["asset_id"], v.get("asset_bbox_m"))
        for p in v.get("parts") or []:
            if p.get("asset"):
                out.setdefault(p["asset"], None)
    return out


def sr_folder(asset_id: str, hf_ids: list[str]) -> str | None:
    rest = asset_id[3:]
    for h in hf_ids:
        if h.split("_", 1)[-1] == rest or h == rest:
            return h
    return None


# ---------------------------------------------------------------------------------------------------------- mesh io
class Part:
    def __init__(self, v: np.ndarray, uv: np.ndarray | None, f: np.ndarray, png: bytes | None, rgba):
        self.v, self.uv, self.f, self.png, self.rgba = v, uv, f, png, rgba  # f: (n, 3) indices into v (and uv)


def _write(out: Path, asset_id: str, parts: list[Part], source: str, hint: list[float] | None) -> dict:
    from PIL import Image

    allv = np.concatenate([p.v for p in parts])
    # pick the axis permutation (identity or a Y-up -> Z-up turn) whose bbox best matches the map's native bbox
    cands = {"id": np.eye(3), "yup": np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)}
    best = "id"
    if hint:
        h = np.maximum(np.array(hint, float), 1e-4)

        def err(rot: np.ndarray) -> float:
            e = np.ptp(allv @ rot.T, axis=0)
            return float(np.abs(np.log(np.maximum(e, 1e-4) / h)).sum())

        best = min(cands, key=lambda k: err(cands[k]))
    rot = cands[best]
    allv = allv @ rot.T
    lo, hi = allv.min(0), allv.max(0)
    shift = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    d = out / asset_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    meta_parts = []
    for k, p in enumerate(parts):
        v = p.v @ rot.T - shift
        lines = [f"v {a:.5f} {b:.5f} {c:.5f}" for a, b, c in v]
        if p.uv is not None:
            lines += [f"vt {a:.5f} {b:.5f}" for a, b in p.uv]
            lines += [f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}" for a, b, c in p.f]
        else:
            lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in p.f]
        (d / f"part{k}.obj").write_text("\n".join(lines) + "\n")
        png = None
        if p.png is not None and p.uv is not None:
            img = Image.open(io.BytesIO(p.png)).convert("RGB")
            img.thumbnail((TEX_MAX, TEX_MAX))
            png = f"part{k}.png"
            img.save(d / png)
        meta_parts.append({"obj": f"part{k}.obj", "png": png, "rgba": [float(x) for x in p.rgba]})
    meta = {"bbox_m": [float(x) for x in hi - lo], "parts": meta_parts, "source": source, "axes": best}
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


# ---------------------------------------------------------------------------------------------------------- MJCF
def parts_from_mjcf(xml: Path) -> list[Part]:
    """Visual mesh geoms of an MJCF object, in its world frame, with their textures."""
    import mujoco
    from PIL import Image

    m = mujoco.MjModel.from_xml_path(str(_localize_mjcf(xml)))
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)
    geoms = [g for g in range(m.ngeom) if m.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH]
    visual = [g for g in geoms if m.geom_contype[g] == 0 and m.geom_conaffinity[g] == 0] or geoms
    parts = []
    for g in visual:
        mid = m.geom_dataid[g]
        va, vn = m.mesh_vertadr[mid], m.mesh_vertnum[mid]
        fa, fn = m.mesh_faceadr[mid], m.mesh_facenum[mid]
        verts = m.mesh_vert[va : va + vn].astype(float)
        faces = m.mesh_face[fa : fa + fn].astype(int)
        R = d.geom_xmat[g].reshape(3, 3)
        verts = verts @ R.T + d.geom_xpos[g]
        uv, png, rgba = None, None, list(m.geom_rgba[g])
        ta, tn = m.mesh_texcoordadr[mid], m.mesh_texcoordnum[mid]
        matid = m.geom_matid[g]
        if matid >= 0:
            rgba = list(m.mat_rgba[matid])
            texid = int(m.mat_texid[matid][mujoco.mjtTextureRole.mjTEXROLE_RGB])
            if texid < 0:
                texid = int(m.mat_texid[matid][0])
            if texid >= 0 and ta >= 0 and tn > 0:
                w, h, c = m.tex_width[texid], m.tex_height[texid], m.tex_nchannel[texid]
                adr = m.tex_adr[texid]
                arr = m.tex_data[adr : adr + w * h * c].reshape(h, w, c)
                out = io.BytesIO()
                Image.fromarray(arr[:, :, :3]).save(out, format="PNG")
                png = out.getvalue()
                # per-corner texcoords -> one vertex per corner, so faces can share positions with different uvs
                ft = m.mesh_facetexcoord[fa : fa + fn].astype(int)
                tc = m.mesh_texcoord[ta : ta + tn].astype(float)
                verts = verts[faces.reshape(-1)]
                uv = tc[ft.reshape(-1)]
                uv[:, 1] = 1.0 - uv[:, 1]  # MuJoCo stores OpenGL-style v; OBJ loading flips it back
                faces = np.arange(len(verts)).reshape(-1, 3)
        parts.append(Part(verts, uv, faces, png, rgba))
    return parts


def _localize_mjcf(xml: Path) -> Path:
    """Some MJCFs point at files on their author's machine: repoint every file= to the copy in the object's folder
    (by basename) and drop textures that are nowhere to be found (their material keeps its color)."""
    import xml.etree.ElementTree as ET

    tree = ET.parse(xml)
    root_dir = xml.parent
    search = root_dir.parent if root_dir.parent.name != "aigen_objs" else root_dir
    local = {p.name: p for p in search.rglob("*") if p.is_file()}
    compiler = tree.getroot().find("compiler")
    base = {
        "mesh": root_dir / (compiler.get("meshdir", "") if compiler is not None else ""),
        "texture": root_dir / (compiler.get("texturedir", "") if compiler is not None else ""),
    }
    dropped = set()
    for el in tree.getroot().iter():
        f = el.get("file")
        if not f:
            continue
        p = Path(f) if Path(f).is_absolute() else base.get(el.tag, root_dir) / f
        if p.exists():
            el.set("file", str(p.resolve()))
        elif Path(f).name in local:
            el.set("file", str(local[Path(f).name].resolve()))
        elif el.tag == "texture":
            dropped.add(el.get("name"))
            el.set("builtin", "flat")
            el.set("rgb1", "0.8 0.8 0.8")
            el.set("width", "8")
            el.set("height", "8")
            del el.attrib["file"]
    if compiler is not None:
        compiler.attrib.pop("meshdir", None)
        compiler.attrib.pop("texturedir", None)
    out = Path(tempfile.mkstemp(suffix=".xml", dir=root_dir)[1])
    tree.write(out)
    return out


def find_model_xml(root: Path) -> Path | None:
    xs = sorted(
        (p for p in root.rglob("*.xml") if not p.name.startswith("tmp")),
        key=lambda p: (p.name != "model.xml", len(str(p))),
    )
    return xs[0] if xs else None


# ---------------------------------------------------------------------------------------------------------- USD
def parts_from_usd(root_usd: Path, textures: list[Path]) -> list[Part]:
    from pxr import Usd, UsdGeom, UsdShade

    stage = Usd.Stage.Open(str(root_usd))
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
    yup = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    cache = UsdGeom.XformCache()
    by_name = {p.name.lower(): p for p in textures}
    alb = [
        p for p in textures if "_alb" in p.name.lower() or "albedo" in p.name.lower() or "basecolor" in p.name.lower()
    ]
    parts = []
    for prim in stage.Traverse(Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Mesh) or UsdGeom.Imageable(prim).ComputeVisibility() == UsdGeom.Tokens.invisible:
            continue
        if UsdGeom.Imageable(prim).ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
            continue
        mesh = UsdGeom.Mesh(prim)
        pts = mesh.GetPointsAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        idx = mesh.GetFaceVertexIndicesAttr().Get()
        if not pts or not counts or not idx:
            continue
        M = np.array(cache.GetLocalToWorldTransform(prim), float)  # row-vector convention
        P = np.c_[np.array(pts, float), np.ones(len(pts))] @ M
        P = P[:, :3] * mpu
        if yup:
            P = P @ np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float).T
        idx = np.array(idx, int)
        st = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
        uv_vals, uv_interp, uv_idx = None, None, None
        if st and st.HasValue():
            uv_vals = np.array(st.Get(), float)
            uv_interp = st.GetInterpolation()
            uv_idx = np.array(st.GetIndices(), int) if st.IsIndexed() else None
        tri_v, tri_uv = [], []
        corner = 0
        for n in counts:
            for j in range(1, n - 1):
                for c in (corner, corner + j, corner + j + 1):
                    tri_v.append(idx[c])
                    if uv_vals is not None:
                        if uv_interp == UsdGeom.Tokens.faceVarying:
                            k = uv_idx[c] if uv_idx is not None else c
                        else:
                            k = uv_idx[idx[c]] if uv_idx is not None else idx[c]
                        tri_uv.append(uv_vals[k] if k < len(uv_vals) else (0.0, 0.0))
            corner += n
        verts = P[np.array(tri_v)]
        faces = np.arange(len(verts)).reshape(-1, 3)
        uv = np.array(tri_uv, float) if tri_uv else None
        # texture: the bound material's diffuse file if we have it, else the asset's only albedo
        png, rgba = None, [0.8, 0.8, 0.8, 1.0]
        mat, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        tex = None
        if mat:
            for p in Usd.PrimRange(mat.GetPrim()):
                for a in p.GetAttributes():
                    val = a.Get()
                    if hasattr(val, "path") and str(val.path).lower().endswith((".png", ".jpg")):
                        name = Path(str(val.path)).name.lower()
                        if ("_alb" in name or "albedo" in name or "basecolor" in name or "diffuse" in name) and (
                            name in by_name
                        ):
                            tex = by_name[name]
        if tex is None and len(alb) == 1:
            tex = alb[0]
        if tex is not None and uv is not None:
            png = tex.read_bytes()
        parts.append(Part(verts, uv if png else None, faces, png, rgba))
    return parts


# ---------------------------------------------------------------------------------------------------------- fetch
def fetch_sr(asset_ids: list[str], cache: Path, hf_ids: list[str]) -> dict[str, Path]:
    from huggingface_hub import snapshot_download

    folders = {a: sr_folder(a, hf_ids) for a in asset_ids}
    pats = []
    for f in folders.values():
        if f:
            pats += [f"{f}/simready_usd/*.usd", f"{f}/simready_usd/*.usda", f"{f}/simready_usd/payloads/*"]
            pats += [f"{f}/simready_usd/Textures/*"]
    if pats:
        snapshot_download(SR_REPO, repo_type="dataset", local_dir=cache / "simready", allow_patterns=pats)
    return {a: cache / "simready" / f / "simready_usd" for a, f in folders.items() if f}


def fetch_lw(asset_ids: list[str], cache: Path) -> dict[str, Path]:
    from huggingface_hub import hf_hub_download

    out = {}
    names = {}
    for a in asset_ids:
        name, variant = a[3:].rsplit("_", 1)
        names.setdefault(name, []).append((a, variant))
    for name, items in names.items():
        z = hf_hub_download(LW_REPO, f"objects_lightwheel/{name}.zip", repo_type="dataset", local_dir=cache / "lw")
        dest = cache / "lw" / name
        if not dest.exists():
            with zipfile.ZipFile(z) as zf:
                zf.extractall(dest)
        for a, variant in items:
            hits = [p for p in dest.rglob("*") if p.is_dir() and p.name.lower() == variant.lower()]
            if hits:
                out[a] = hits[0]
    return out


def fetch_rc(asset_ids: list[str], cache: Path) -> dict[str, Path]:
    dest = cache / "robocasa"
    rx = []
    want = {}
    for a in asset_ids:
        obj = a[3:]
        cat = obj.rsplit("_", 1)[0]
        want[a] = (cat, obj)
        if not (dest / "aigen_objs" / cat / obj).exists():
            rx.append(f"^aigen_objs/{cat}/{obj}/")
    if rx:
        subprocess.run([sys.executable, "-I", str(HERE / "rzip.py"), "extract", RC_ZIP, str(dest), *rx], check=True)
    return {a: dest / "aigen_objs" / c / o for a, (c, o) in want.items() if (dest / "aigen_objs" / c / o).exists()}


# ---------------------------------------------------------------------------------------------------------- decimate
def decimate(asset_dir: Path, budget: int) -> str:
    """Texture-preserving quadric decimation of every part to share ``budget`` triangles (needs pymeshlab)."""
    import pymeshlab

    meta = json.loads((asset_dir / "meta.json").read_text())
    counts = [sum(1 for ln in open(asset_dir / p["obj"]) if ln.startswith("f ")) for p in meta["parts"]]
    total = sum(counts)
    if total <= budget:
        return f"{asset_dir.name}: {total} tris (kept)"
    after = 0
    for p, n in zip(meta["parts"], counts, strict=True):
        target = max(40, int(budget * n / total))
        obj = asset_dir / p["obj"]
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(str(obj))
        textured = ms.current_mesh().has_wedge_tex_coord()
        ms.meshing_merge_close_vertices()
        if textured:
            ms.meshing_decimation_quadric_edge_collapse_with_texture(targetfacenum=target, preserveboundary=True)
        else:
            ms.meshing_decimation_quadric_edge_collapse(targetfacenum=target, preserveboundary=True)
        ms.save_current_mesh(str(obj), save_textures=False, save_wedge_texcoord=textured, save_vertex_normal=False)
        for extra in (obj.with_suffix(".obj.mtl"), obj.with_suffix(".mtl")):
            extra.unlink(missing_ok=True)
        after += ms.current_mesh().face_number()
    return f"{asset_dir.name}: {total} -> {after} tris"


# ---------------------------------------------------------------------------------------------------------- main
def convert(asset_id: str, src: Path, out: Path, hint) -> str:
    try:
        if asset_id.startswith("sr_"):
            roots = sorted(src.glob("sm_*.usd")) or sorted(src.glob("*.usd*"))
            texs = sorted((src / "Textures").glob("*.png")) + sorted((src / "Textures").glob("*.jpg"))
            parts = parts_from_usd(roots[0], texs)
        else:
            xml = find_model_xml(src)
            if xml is None:
                return f"{asset_id}: no MJCF under {src}"
            parts = parts_from_mjcf(xml)
        if not parts:
            return f"{asset_id}: no meshes"
        meta = _write(out, asset_id, parts, str(src), hint)
        bb = " x ".join(f"{x:.3f}" for x in meta["bbox_m"])
        tex = sum(1 for p in meta["parts"] if p["png"])
        return f"{asset_id}: ok {len(parts)} part(s), {tex} textured, bbox {bb} m ({meta['axes']})"
    except Exception as exc:  # noqa: BLE001 - one bad asset must not stop the build
        return f"{asset_id}: FAILED {type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.environ.get("PICKY_MJ_ASSET_ROOT", str(Path.home() / "picky-assets-mj")))
    ap.add_argument("--cache", default=None, help="raw downloads (default <out>/_cache)")
    ap.add_argument("--only", default="", help="comma list of asset ids")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--tri-budget", type=int, default=2500, help="max triangles per model (0 = keep)")
    ap.add_argument("--decimate-only", action="store_true", help="skip fetch/convert; decimate what is in --out")
    args = ap.parse_args()
    out = Path(args.out).expanduser()
    if args.decimate_only:
        dirs = [d for d in sorted(out.iterdir()) if (d / "meta.json").exists()]
        with cf.ProcessPoolExecutor(args.jobs) as ex:
            for line in ex.map(decimate, dirs, [args.tri_budget] * len(dirs)):
                print(line, flush=True)
        return 0
    cache = Path(args.cache).expanduser() if args.cache else out / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    want = wanted_assets()
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        want = {k: v for k, v in want.items() if k in keep}
    hf_ids = [ln.strip() for ln in (HERE / "hf_ids.txt").read_text().splitlines() if ln.strip()]
    by = {p: [a for a in want if a.startswith(p + "_")] for p in ("sr", "lw", "rc")}
    print(f"{len(want)} models: " + ", ".join(f"{k}={len(v)}" for k, v in by.items()), flush=True)
    srcs: dict[str, Path] = {}
    with cf.ThreadPoolExecutor(3) as ex:
        futs = {
            ex.submit(fetch_sr, by["sr"], cache, hf_ids): "sr",
            ex.submit(fetch_lw, by["lw"], cache): "lw",
            ex.submit(fetch_rc, by["rc"], cache): "rc",
        }
        for f in cf.as_completed(futs):
            try:
                got = f.result()
                srcs.update(got)
                print(f"fetched {futs[f]}: {len(got)}/{len(by[futs[f]])}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"fetch {futs[f]} FAILED: {exc}", flush=True)
    missing = sorted(set(want) - set(srcs))
    if missing:
        print("not fetched:", ", ".join(missing), flush=True)
    with cf.ProcessPoolExecutor(args.jobs) as ex:
        for line in ex.map(convert, list(srcs), list(srcs.values()), [out] * len(srcs), [want[a] for a in srcs]):
            print(line, flush=True)
    if args.tri_budget:
        dirs = [out / a for a in srcs if (out / a / "meta.json").exists()]
        with cf.ProcessPoolExecutor(args.jobs) as ex:
            for line in ex.map(decimate, dirs, [args.tri_budget] * len(dirs)):
                print(line, flush=True)
    shutil.copy(HERE / "ATTRIBUTION.md", out / "ATTRIBUTION.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
