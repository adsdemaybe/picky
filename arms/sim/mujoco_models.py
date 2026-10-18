"""Per-SKU product models for the MuJoCo backend: ``data/sim/sku_assets.yaml`` + the meshes built by
``arms/sim/tools/mujoco_assets.py`` (``PICKY_MJ_ASSET_ROOT``, default ``~/picky-assets-mj``).

Same decisions as the Isaac path (arms/sim/ASSETS.md), drawn with MuJoCo meshes instead of USD references:

- ``render: asset``  one model, scaled per axis to ``target_dims_m`` (item frame) after ``facing.yaw_offset_deg``.
- ``render: parts``  every model member scaled to its ``member_dims_m`` at each ``offsets_m`` (bottom-center, item
  frame), plus the wrapper boxes: film / clamshell / mesh bag see-through, carrier / board opaque.
- anything else, or a missing mesh: None, so the backend keeps the primitive box for that SKU.

``plan(sku, unit_size)`` returns geoms relative to the unit's box center (the PrimSpec center), so the backend only has
to place the unit body. Meshes are shared per (SKU, asset, part); textures per (asset, part).
"""

import json
import math
import os
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.log import get_logger

log = get_logger(__name__)

ASSET_ROOT_ENV = "PICKY_MJ_ASSET_ROOT"
DEFAULT_ROOT = Path.home() / "picky-assets-mj"
SEE_THROUGH = {"film": 0.18, "clamshell": 0.25, "mesh_bag": 0.35}
BOX_COLORS = {"carrier": "0.62 0.48 0.32", "board": "0.85 0.80 0.70"}


@dataclass(frozen=True)
class MeshGeom:
    mesh: str  # mesh asset name
    material: str | None
    rgba: str
    pos: tuple[float, float, float]
    yaw: float


@dataclass(frozen=True)
class BoxGeom:
    half: tuple[float, float, float]
    pos: tuple[float, float, float]
    rgba: str


@dataclass
class ModelLibrary:
    root: Path
    skus: dict[str, Any]
    meshes: dict[str, tuple[str, tuple[float, float, float]]] = field(default_factory=dict)  # name -> (obj, scale)
    textures: dict[str, str] = field(default_factory=dict)  # material name -> png
    _meta: dict[str, dict | None] = field(default_factory=dict)
    _plans: dict[tuple[str, tuple[float, float, float]], list | None] = field(default_factory=dict)

    @classmethod
    def load(
        cls, root: str | os.PathLike | None = None, sku_map: str | os.PathLike | None = None
    ) -> "ModelLibrary | None":
        """None when models are off (``PICKY_SIM_ASSETS=off``), the root is missing, or the map is unreadable."""
        if os.environ.get("PICKY_SIM_ASSETS", "").lower() in ("off", "0", "false"):
            return None
        r = Path(root or os.environ.get(ASSET_ROOT_ENV) or DEFAULT_ROOT).expanduser()
        if not r.is_dir():
            log.info("mujoco product models off: %s not found (build: arms/sim/tools/mujoco_assets.py)", r)
            return None
        if sku_map is None:
            from utils.config import get_settings

            sku_map = Path(get_settings().picky_data_dir) / "sim" / "sku_assets.yaml"
        try:
            import yaml

            skus = yaml.safe_load(Path(sku_map).read_text())["skus"]
        except Exception as exc:  # noqa: BLE001 - primitives still work
            log.warning("mujoco product models off: cannot read %s (%s)", sku_map, exc)
            return None
        lib = cls(root=r, skus=skus)
        n = sum(1 for s in skus if lib.has_model(s))
        log.info("mujoco product models from %s: %d/%d SKUs have a model", r, n, len(skus))
        return lib

    # ---- assets on disk
    def meta(self, asset_id: str) -> dict | None:
        if asset_id not in self._meta:
            p = self.root / asset_id / "meta.json"
            try:
                self._meta[asset_id] = json.loads(p.read_text())
            except (OSError, ValueError):
                self._meta[asset_id] = None
        return self._meta[asset_id]

    def has_model(self, sku: str) -> bool:
        e = self.skus.get(sku) or {}
        if e.get("render") == "asset":
            return e.get("match") != "gap" and self.meta(e.get("asset_id") or "") is not None
        if e.get("render") == "parts":
            return any(self.meta(p["asset"]) for p in e.get("parts") or [] if p.get("asset"))
        return False

    # ---- plans
    def _mesh_geoms(
        self, sku: str, asset_id: str, dims: tuple[float, float, float], yaw: float, at: tuple[float, float, float]
    ) -> list[MeshGeom]:
        meta = self.meta(asset_id)
        if meta is None:
            return []
        bx, by, bz = (max(1e-4, v) for v in meta["bbox_m"])
        swap = abs(math.sin(yaw)) > 0.7  # 90 / 270: native x lies along the item's y
        tx, ty, tz = (dims[1], dims[0], dims[2]) if swap else dims
        scale = (tx / bx, ty / by, tz / bz)
        out = []
        for k, part in enumerate(meta["parts"]):
            mesh = f"m.{sku}.{asset_id}.{k}"
            self.meshes[mesh] = (str(self.root / asset_id / part["obj"]), scale)
            material = None
            if part.get("png"):
                material = f"t.{asset_id}.{k}"
                self.textures[material] = str(self.root / asset_id / part["png"])
            rgba = " ".join(f"{c:.3f}" for c in (part.get("rgba") or [0.8, 0.8, 0.8, 1.0]))
            out.append(MeshGeom(mesh, material, "1 1 1 1" if material else rgba, at, yaw))
        return out

    def plan(self, sku: str | None, unit_size: tuple[float, float, float], key: str = "") -> list | None:
        """Geoms for one unit, relative to its box center; None = draw the primitive."""
        if not sku:
            return None
        e = self.skus.get(sku)
        if not e or not self.has_model(sku):
            return None
        bottom = -unit_size[2] / 2
        cache_key = (sku, unit_size)
        if e.get("render") == "asset" and key == "" and cache_key in self._plans:
            return self._plans[cache_key]
        geoms: list = []
        if e["render"] == "asset":
            dims = tuple(e.get("target_dims_m") or unit_size)
            yaw = math.radians(float((e.get("facing") or {}).get("yaw_offset_deg") or 0.0))
            geoms = self._mesh_geoms(sku, e["asset_id"], dims, yaw, (0.0, 0.0, bottom))
            self._plans[cache_key] = geoms or None
        else:
            for p in e.get("parts") or []:
                if p.get("asset"):
                    yaw = sum(math.radians(a) for ax, a in (p.get("rotate") or []) if ax == "z")
                    dims = tuple(p.get("member_dims_m") or unit_size)
                    amp = float(p.get("yaw_jitter_deg") or 0.0)
                    for i, off in enumerate(p.get("offsets_m") or [[0.0, 0.0, 0.0]]):
                        jit = 0.0
                        if amp:
                            jit = math.radians(((zlib.crc32(f"{key}/{i}".encode()) % 2001) / 1000 - 1) * amp)
                        at = (float(off[0]), float(off[1]), bottom + float(off[2]))
                        geoms += self._mesh_geoms(sku, p["asset"], dims, yaw + jit, at)
                elif p.get("prim") == "box":
                    sx, sy, sz = (float(v) for v in p["size_m"])
                    ox, oy, oz = (float(v) for v in p.get("offset_m") or (0, 0, 0))
                    kind = p.get("kind") or ""
                    if kind in SEE_THROUGH:
                        rgba = f"0.92 0.95 0.97 {SEE_THROUGH[kind]}"
                    else:
                        rgba = BOX_COLORS.get(kind, "0.7 0.6 0.45") + " 1"
                    geoms.append(BoxGeom((sx / 2, sy / 2, sz / 2), (ox, oy, bottom + oz + sz / 2), rgba))
        return geoms or None

    # ---- MJCF
    def asset_xml(self) -> str:
        out = []
        for name, png in self.textures.items():
            out.append(f'<texture name="{name}" type="2d" file="{png}"/>')
            out.append(f'<material name="{name}" texture="{name}" specular="0.15" shininess="0.2"/>')
        for name, (obj, s) in self.meshes.items():
            out.append(f'<mesh name="{name}" file="{obj}" scale="{s[0]:.5f} {s[1]:.5f} {s[2]:.5f}"/>')
        return "".join(out)


def geom_xml(g: MeshGeom | BoxGeom, quat_of) -> str:
    """One planned geom as MJCF (visual only)."""
    if isinstance(g, BoxGeom):
        return (
            f'<geom type="box" size="{g.half[0]:.4f} {g.half[1]:.4f} {g.half[2]:.4f}" '
            f'pos="{g.pos[0]:.4f} {g.pos[1]:.4f} {g.pos[2]:.4f}" rgba="{g.rgba}" contype="0" conaffinity="0"/>'
        )
    q = quat_of(g.yaw)
    mat = f' material="{g.material}"' if g.material else ""
    return (
        f'<geom type="mesh" mesh="{g.mesh}"{mat} rgba="{g.rgba}" pos="{g.pos[0]:.4f} {g.pos[1]:.4f} {g.pos[2]:.4f}" '
        f'quat="{q[0]:.6f} {q[1]:.6f} {q[2]:.6f} {q[3]:.6f}" contype="0" conaffinity="0"/>'
    )
