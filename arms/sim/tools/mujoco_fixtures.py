"""Convert the store-fixture models and surface textures for the MuJoCo store (``arms/sim/mujoco_dressing.py``).

Sources (all downloadable without an account; attribution in ``<out>/fixtures/ATTRIBUTION.md``):
- Objaverse (HF allenai/objaverse, CC-BY-4.0): checkout counter, shopping trolley.
- Poly Haven (CC0): plastic produce crate.
- ambientCG (CC0): terrazzo floor tiles, acoustic ceiling tiles, painted plaster, brushed metal.

Output under ``<out>/fixtures/`` (default out = ``PICKY_MJ_ASSET_ROOT`` or ``~/picky-assets-mj``):
``<name>/part<k>.obj|png`` + ``meta.json`` (same layout as the product models) and ``tex/<surface>.png``.

    uv run --extra mujoco --with trimesh --with pymeshlab python arms/sim/tools/mujoco_fixtures.py --src <research dir>
"""

import argparse
import io
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mujoco_assets import Part, _write, decimate  # noqa: E402

OBJAVERSE = "https://huggingface.co/datasets/allenai/objaverse/resolve/main/glbs/{shard}/{uid}.glb"
MODELS = {
    # name: (relative source, scale to meters, Z-up target dims hint (x, y, z), attribution)
    "checkout": (
        "objaverse/40688b571dd84a4a87d513d086c00aac.glb",
        0.01,
        (2.00, 1.10, 0.92),
        '"Checkout" by emaj0510, sketchfab.com/3d-models/40688b571dd84a4a87d513d086c00aac, CC BY 4.0',
    ),
    "cart": (
        "objaverse/5770a906c5004041b65604126e71b620.glb",
        1.0,
        (0.43, 1.05, 0.90),
        '"Shopping Trolley" by mehran1500, sketchfab.com/3d-models/5770a906c5004041b65604126e71b620, CC BY 4.0',
    ),
    "crate": (
        "polyhaven/plastic_crate_02",
        1.0,
        (0.51, 0.41, 0.25),
        '"Plastic Crate 02", Poly Haven (polyhaven.com), CC0',
    ),
}
TEXTURES = {
    "floor": ("Tiles040", "ambientCG Tiles040, CC0"),
    "ceiling": ("OfficeCeiling005", "ambientCG OfficeCeiling005, CC0"),
    "wall": ("PaintedPlaster017", "ambientCG PaintedPlaster017, CC0"),
    "metal": ("Metal009", "ambientCG Metal009, CC0"),
}


def _load_parts(path: Path, scale: float) -> list[Part]:
    import trimesh

    if path.is_dir():
        cands = sorted(path.rglob("*.gltf")) + sorted(path.rglob("*.glb"))
        path = cands[0]
    scene = trimesh.load(str(path), force="scene")
    parts = []
    for node in scene.graph.nodes_geometry:
        tf, gname = scene.graph[node]
        g = scene.geometry[gname]
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        v = trimesh.transformations.transform_points(g.vertices, tf) * scale
        f = np.asarray(g.faces, int)
        uv, png, rgba = None, None, [0.8, 0.8, 0.8, 1.0]
        vis = g.visual
        mat = getattr(vis, "material", None)
        if mat is not None:
            img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
            col = getattr(mat, "baseColorFactor", None)
            if col is None:
                col = getattr(mat, "main_color", None)
            if col is not None:
                c = np.asarray(col, float).reshape(-1)
                rgba = list(c / 255.0 if c.max() > 1.0 else c)[:4] + [1.0] * (4 - min(4, len(c)))
            if img is not None and getattr(vis, "uv", None) is not None and len(vis.uv) == len(g.vertices):
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                png, uv = buf.getvalue(), np.asarray(vis.uv, float)
        elif hasattr(vis, "main_color"):
            c = np.asarray(vis.main_color, float) / 255.0
            rgba = list(c[:4])
        # one vertex per corner so the OBJ writer's (v, vt) indices line up
        fv = v[f.reshape(-1)]
        fuv = uv[f.reshape(-1)] if uv is not None else None
        parts.append(Part(fv, fuv, np.arange(len(fv)).reshape(-1, 3), png, rgba))
    return parts


def so101_light(src_xml: Path, out: Path, budget: int = 6000) -> Path:
    """Copy TheRobotStudio's SO-101 MJCF with every STL decimated (same joints, links and dims; lighter to render)."""
    import shutil

    import pymeshlab

    dst = out / "so101"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_xml.parent, dst, ignore=shutil.ignore_patterns("*.part", "*.urdf"))
    for stl in sorted((dst / "assets").glob("*.stl")):
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(str(stl))
        n = ms.current_mesh().face_number()
        if n > budget:
            ms.meshing_decimation_quadric_edge_collapse(targetfacenum=budget, preserveboundary=True)
            ms.save_current_mesh(str(stl), binary=True)
        print("so101", stl.name, n, "->", ms.current_mesh().face_number())
    return dst / src_xml.name


def main() -> int:
    from PIL import Image

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="research download dir (objaverse/, polyhaven/, textures/)")
    ap.add_argument("--out", default=os.environ.get("PICKY_MJ_ASSET_ROOT", str(Path.home() / "picky-assets-mj")))
    ap.add_argument("--tri-budget", type=int, default=6000)
    ap.add_argument("--so101-xml", default="", help="so101_new_calib.xml to copy with decimated meshes")
    args = ap.parse_args()
    src, out = Path(args.src).expanduser(), Path(args.out).expanduser() / "fixtures"
    out.mkdir(parents=True, exist_ok=True)
    credits = []
    for name, (rel, scale, hint, credit) in MODELS.items():
        parts = _load_parts(src / rel, scale)
        meta = _write(out, name, parts, str(src / rel), list(hint))
        print(name, "bbox", [round(x, 2) for x in meta["bbox_m"]], meta["axes"], decimate(out / name, args.tri_budget))
        credits.append(f"- {name}: {credit}")
    (out / "tex").mkdir(exist_ok=True)
    for surface, (tid, credit) in TEXTURES.items():
        color = next((src / "textures" / tid).glob("*_Color.jpg"))
        img = Image.open(color).convert("RGB")
        img.thumbnail((1024, 1024))
        img.save(out / "tex" / f"{surface}.png")
        credits.append(f"- {surface} texture: {credit}")
        print("texture", surface, img.size)
    if args.so101_xml:
        print("so101 ->", so101_light(Path(args.so101_xml).expanduser(), out.parent))
        credits.append("- so101: TheRobotStudio SO-ARM100 (SO101) MJCF, Apache-2.0 (meshes decimated)")
    (out / "ATTRIBUTION.md").write_text("# Store fixture assets\n\n" + "\n".join(credits) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
