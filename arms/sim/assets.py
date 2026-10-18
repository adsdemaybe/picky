"""Per-item grocery models + rover USD for the Isaac worker: which SKU renders as which model, and why not.

PURE python (pydantic + PyYAML, no pxr): the scene spec uses it for unit dims, the Isaac backend (app.py) for what to
reference, the tests on a Mac for the fallback rules. Everything degrades to today's primitive box/cylinder + label:

- ``PICKY_ASSET_ROOT`` unset / not a directory, or ``PICKY_SIM_ASSETS=off``  -> every SKU is a primitive;
- the SKU is missing from ``data/sim/sku_assets.yaml`` (or ``PICKY_SKU_ASSETS``), is a ``gap`` match (stand-in model),
  or is ``render: primitive`` by design (egg carton, napkins, ...)                   -> primitive;
- any model file the SKU needs is missing under the asset root                      -> primitive (one warning per SKU).

A resolved SKU (``UnitAsset``) renders ONE model reference per unit (``render: asset``: ``items/<id>/item.usda``) or a
small composite (``render: parts``: several ``geom.usda`` references + opaque wrapper boxes) and uses
``target_dims_m`` as the unit size, so spacing, scan bboxes and basket stacking match the mesh.
The map is generated (``arms/sim/tools/gen_sku_assets.py``); never hand-edit it (CLAUDE.md rule 5).

Label decals (``label_meshes``): our label texture (``arms.sim.labels.label_png``: brand, name, size, price, EAN-13)
stays on every packaged unit so perception / basket verification read the same artwork as today. Geometry is in the
ITEM frame (x width, y depth with -y facing the aisle, z up from the unit's bottom center); the backend maps it into
the model's native frame.

``PICKY_ROVER_USD`` -> ``picky_rover.usda`` (LeKiwi + SO-101 + lift overlay): ``rover_usd_from_env`` returns it when
the file exists, else None (primitive rover).
"""

import math
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from utils.log import get_logger

log = get_logger(__name__)

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]

ASSET_ROOT_ENV = "PICKY_ASSET_ROOT"
SKU_ASSETS_ENV = "PICKY_SKU_ASSETS"
ASSETS_SWITCH_ENV = "PICKY_SIM_ASSETS"
ROVER_USD_ENV = "PICKY_ROVER_USD"
SKU_ASSETS_FILE = "sim/sku_assets.yaml"
"""Relative to the data dir (PICKY_DATA_DIR)."""
OFF_VALUES = frozenset({"0", "off", "false", "no", "primitive", "primitives"})

TRANSLUCENT_BOX_KINDS = frozenset({"film", "clamshell", "mesh_bag"})
"""Wrapper boxes the worker skips: clear film / PET / netting need a translucent material and real-time RTX may draw
UsdPreviewSurface opacity as an opaque or cut-out box (unverified), which would hide the tray contents."""
LABEL_GAP_M = 0.0015
"""Decals sit this far outside the unit's bbox face (no z-fighting with the model)."""
WRAP_SEGMENTS = 16
FRONT_STRIP_H_M = 0.015


class PartModel(BaseModel):
    """One model member of a composite unit: references ``geom.usda`` (no rigid body inside) once per offset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    usd: str = Field(description="Absolute path of the member's geom.usda.")
    rotate: tuple[tuple[str, float], ...] = ()
    scale_xyz: Vec3 = (1.0, 1.0, 1.0)
    offsets_m: tuple[Vec3, ...] = ((0.0, 0.0, 0.0),)
    yaw_jitter_deg: float = 0.0


class PartBox(BaseModel):
    """An opaque wrapper box of a composite unit (carrier / board / carton), bottom-center at ``offset_m``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    size_m: Vec3
    offset_m: Vec3 = (0.0, 0.0, 0.0)
    color: str | None = None
    collider: bool = False


class LabelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["wrap", "panel", "top", "sticker", "none"] = "panel"
    band_z: Vec2 = (0.1, 0.9)
    cover_wh: Vec2 = (0.9, 0.8)
    center_z: float = 0.5
    cover_wd: Vec2 = (0.9, 0.8)
    front_strip: bool = True
    size_m: Vec2 = (0.06, 0.04)
    sleeve: bool = False
    png_px: tuple[int, int] = (512, 384)


class UnitAsset(BaseModel):
    """How the Isaac backend draws one unit of a SKU (resolved: every file exists)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sku: str
    render: Literal["asset", "parts"]
    match: str = "close"
    asset_id: str | None = None
    dims: Vec3 = Field(description="target_dims_m: rendered bbox in the item frame (= SlotLayout.dims).")
    usd: str | None = Field(None, description="render=asset: absolute path of items/<id>/item.usda.")
    scale_xyz: Vec3 = (1.0, 1.0, 1.0)
    yaw_offset_deg: float = 0.0
    yaw_jitter_deg: float = 0.0
    models: tuple[PartModel, ...] = ()
    boxes: tuple[PartBox, ...] = ()
    label: LabelSpec = LabelSpec()
    mass_kg: float | None = None

    def files(self) -> list[str]:
        return ([self.usd] if self.usd else []) + [m.usd for m in self.models]

    def yaw_jitter(self, instance_id: str) -> float:
        """Deterministic per-unit yaw jitter in degrees (loose produce; piles don't look cloned)."""
        return jitter_deg(instance_id, self.yaw_jitter_deg)


def jitter_deg(key: str, amplitude_deg: float) -> float:
    """Deterministic value in [-amplitude, +amplitude] from ``key`` (crc32: stable across processes)."""
    if not amplitude_deg:
        return 0.0
    import zlib

    return ((zlib.crc32(key.encode("utf-8")) % 2001) / 1000.0 - 1.0) * amplitude_deg


def _vec3(v: Any, default: Vec3 = (0.0, 0.0, 0.0)) -> Vec3:
    if v is None:
        return default
    x, y, z = (float(c) for c in v)
    return (x, y, z)


def _vec2(v: Any, default: Vec2) -> Vec2:
    if v is None:
        return default
    a, b = (float(c) for c in v)
    return (a, b)


def _hex_color(value: Any) -> str | None:
    """'#rrggbb' or None (the map also says ``label_color``: the product's label color, filled in by the backend)."""
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        try:
            int(value[1:], 16)
            return value
        except ValueError:
            return None
    return None


def parse_label(raw: Mapping[str, Any] | None) -> LabelSpec:
    raw = raw or {}
    mode = str(raw.get("mode") or "panel")
    if mode not in ("wrap", "panel", "top", "sticker", "none"):
        mode = "panel"
    px = raw.get("png_px") or (512, 384)
    return LabelSpec(
        mode=mode,  # type: ignore[arg-type]
        band_z=_vec2(raw.get("band_z"), (0.1, 0.9)),
        cover_wh=_vec2(raw.get("cover_wh"), (0.9, 0.8)),
        center_z=float(raw.get("center_z", 0.5)),
        cover_wd=_vec2(raw.get("cover_wd"), (0.9, 0.8)),
        front_strip=bool(raw.get("front_strip", True)),
        size_m=_vec2(raw.get("size_m"), (0.06, 0.04)),
        sleeve=bool(raw.get("sleeve")),
        png_px=(int(px[0]), int(px[1])),
    )


class SkuAssets:
    """``sku_assets.yaml`` + an asset root -> ``resolve(sku)`` (cached; None = draw the primitive).

    ``exists`` is injectable for tests (default ``os.path.isfile``)."""

    def __init__(
        self,
        entries: Mapping[str, Mapping[str, Any]] | None,
        root: str | os.PathLike[str] | None,
        *,
        enabled: bool = True,
        exists: Callable[[str], bool] = os.path.isfile,
        source: str = "",
    ) -> None:
        self.entries: dict[str, Mapping[str, Any]] = dict(entries or {})
        self.root = str(root) if root else None
        self.enabled = enabled
        self.exists = exists
        self.source = source
        self.reasons: dict[str, str] = {}
        """sku -> why it falls back to the primitive (filled by ``resolve``)."""
        self.off_reason = ""
        self._cache: dict[str, UnitAsset | None] = {}
        self._warned: set[str] = set()

    # ---- construction --------------------------------------------------------------------------------------------
    @classmethod
    def disabled(cls, reason: str = "assets off") -> "SkuAssets":
        sa = cls({}, None, enabled=False)
        sa.off_reason = reason
        return sa

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        root: str | os.PathLike[str] | None,
        *,
        enabled: bool = True,
        exists: Callable[[str], bool] = os.path.isfile,
    ) -> "SkuAssets":
        """Read the map (a missing / broken file means: no SKU resolves)."""
        entries: dict[str, Any] = {}
        try:
            import yaml

            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            with open(path, encoding="utf-8") as fh:
                doc = yaml.load(fh, Loader=loader) or {}  # noqa: S506 - SafeLoader / CSafeLoader
            entries = dict(doc.get("skus") or {})
        except Exception as exc:  # noqa: BLE001 - OSError / yaml.YAMLError / bad shape: degrade, never raise
            log.warning("sku assets map %s unreadable (%s): every SKU renders as a primitive", path, exc)
        return cls(entries, root, enabled=enabled, exists=exists, source=str(path))

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, data_root: str | os.PathLike[str] | None = None
    ) -> "SkuAssets":
        """The worker's config: PICKY_ASSET_ROOT (models), PICKY_SKU_ASSETS (map; default data/sim/sku_assets.yaml),
        PICKY_SIM_ASSETS=off (force primitives)."""
        env = os.environ if env is None else env
        switch = (env.get(ASSETS_SWITCH_ENV) or "").strip().lower()
        if switch in OFF_VALUES:
            return cls.disabled(f"{ASSETS_SWITCH_ENV}={switch}")
        root = (env.get(ASSET_ROOT_ENV) or "").strip()
        if not root:
            return cls.disabled(f"{ASSET_ROOT_ENV} unset")
        if not os.path.isdir(root):
            log.warning("%s=%s is not a directory: every SKU renders as a primitive", ASSET_ROOT_ENV, root)
            return cls.disabled(f"{ASSET_ROOT_ENV}={root} missing")
        path = (env.get(SKU_ASSETS_ENV) or "").strip() or str(default_map_path(data_root))
        return cls.load(path, root)

    # ---- resolution ------------------------------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.enabled and bool(self.root) and bool(self.entries)

    def _abs(self, rel: str) -> str:
        assert self.root is not None
        return rel if os.path.isabs(rel) else os.path.join(self.root, rel)

    def resolve(self, sku: str | None) -> UnitAsset | None:
        """The model plan for ``sku``, or None (primitive; ``reasons[sku]`` says why)."""
        if not sku:
            return None
        if sku in self._cache:
            return self._cache[sku]
        res = self._resolve(sku)
        self._cache[sku] = res
        return res

    def _fallback(self, sku: str, reason: str, warn: bool = False) -> None:
        self.reasons[sku] = reason
        if warn and sku not in self._warned:
            self._warned.add(sku)
            log.warning("sku %s: %s; drawing the primitive", sku, reason)

    def _resolve(self, sku: str) -> UnitAsset | None:
        if not self.enabled or not self.root:
            self._fallback(sku, self.off_reason or "assets off")
            return None
        e = self.entries.get(sku)
        if not e:
            self._fallback(sku, "not in the sku assets map")
            return None
        render = str(e.get("render") or "")
        match = str(e.get("match") or "")
        if match == "gap":
            self._fallback(sku, "gap: no matching model (stand-in skipped)")
            return None
        if render not in ("asset", "parts"):
            self._fallback(sku, f"render={render or 'none'} by design")
            return None
        try:
            ua = self._build(sku, e, render)
        except (KeyError, TypeError, ValueError) as exc:
            self._fallback(sku, f"bad map entry ({exc})", warn=True)
            return None
        missing = [f for f in ua.files() if not self.exists(f)]
        if missing:
            self._fallback(sku, f"missing model file {missing[0]}", warn=True)
            return None
        return ua

    def _build(self, sku: str, e: Mapping[str, Any], render: str) -> UnitAsset:
        facing = e.get("facing") or {}
        dims = _vec3(e["target_dims_m"])
        if min(dims) <= 0:
            raise ValueError(f"target_dims_m {dims}")
        models: list[PartModel] = []
        boxes: list[PartBox] = []
        usd = None
        if render == "asset":
            usd = self._abs(str(e["usd"]))
        else:
            for part in e.get("parts") or []:
                if "asset" in part:
                    models.append(
                        PartModel(
                            asset_id=str(part["asset"]),
                            usd=self._abs(str(part["usd"])),
                            rotate=tuple((str(a), float(d)) for a, d in (part.get("rotate") or [])),
                            scale_xyz=_vec3(part.get("scale_xyz"), (1.0, 1.0, 1.0)),
                            offsets_m=tuple(_vec3(o) for o in (part.get("offsets_m") or [(0, 0, 0)])),
                            yaw_jitter_deg=float(part.get("yaw_jitter_deg") or 0.0),
                        )
                    )
                elif str(part.get("kind")) not in TRANSLUCENT_BOX_KINDS:
                    boxes.append(
                        PartBox(
                            kind=str(part.get("kind") or "box"),
                            size_m=_vec3(part["size_m"]),
                            offset_m=_vec3(part.get("offset_m")),
                            color=_hex_color(part.get("color")),
                            collider=bool(part.get("collider")),
                        )
                    )
            if not models and not boxes:
                raise ValueError("parts without members")
        return UnitAsset(
            sku=sku,
            render=render,  # type: ignore[arg-type]
            match=str(e.get("match") or ""),
            asset_id=e.get("asset_id"),
            dims=dims,
            usd=usd,
            scale_xyz=_vec3(e.get("scale_xyz"), (1.0, 1.0, 1.0)),
            yaw_offset_deg=float(facing.get("yaw_offset_deg") or 0.0),
            yaw_jitter_deg=float(facing.get("yaw_jitter_deg") or 0.0),
            models=tuple(models),
            boxes=tuple(boxes),
            label=parse_label(e.get("label")),
            mass_kg=float(e["mass_kg"]) if e.get("mass_kg") is not None else None,
        )

    def unit_dims(self, sku: str | None) -> Vec3 | None:
        """Layout dims override for ``sku`` (None = keep products.yaml dims)."""
        ua = self.resolve(sku)
        return ua.dims if ua else None

    def summary(self, skus: list[str]) -> dict[str, Any]:
        """Counts for logs: how many of ``skus`` resolve, and the fallback reasons."""
        ok = [s for s in skus if self.resolve(s) is not None]
        why: dict[str, list[str]] = {}
        for s in skus:
            if s not in ok:
                why.setdefault(self.reasons.get(s, "?").split(" (")[0].split(":")[0], []).append(s)
        return {"models": len(ok), "primitive": len(skus) - len(ok), "why": why}

    @property
    def describe(self) -> str:
        if not self.enabled:
            return f"per-item models off ({self.off_reason or 'disabled'})"
        return f"per-item models from {self.root} ({len(self.entries)} SKUs mapped in {self.source or 'memory'})"


def default_map_path(data_root: str | os.PathLike[str] | None = None) -> Path:
    try:
        from utils.data import data_dir

        return data_dir(data_root) / SKU_ASSETS_FILE
    except Exception:  # noqa: BLE001 - settings unavailable: repo-relative default
        return Path(__file__).resolve().parents[2] / "data" / SKU_ASSETS_FILE


def rover_usd_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """PICKY_ROVER_USD when it names an existing file (the LeKiwi + SO-101 + lift ``picky_rover.usda``), else None."""
    env = os.environ if env is None else env
    path = (env.get(ROVER_USD_ENV) or "").strip()
    if not path:
        return None
    if not os.path.isfile(path):
        log.warning("%s=%s does not exist: rovers stay primitive", ROVER_USD_ENV, path)
        return None
    return os.path.abspath(path)


# --------------------------------------------------------------------------------------------------------------------
# Label decals (item frame)
# --------------------------------------------------------------------------------------------------------------------
class LabelMesh(BaseModel):
    """One decal mesh in the ITEM frame. ``textured`` = our label PNG (uv 0..1), else solid label color."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    points: tuple[Vec3, ...]
    counts: tuple[int, ...]
    indices: tuple[int, ...]
    normals: tuple[Vec3, ...]
    uvs: tuple[Vec2, ...] = ()
    textured: bool = True


def _quad(name: str, corners: tuple[Vec3, Vec3, Vec3, Vec3], normal: Vec3, textured: bool = True) -> LabelMesh:
    """corners in order bottom-left, bottom-right, top-right, top-left (as seen from outside)."""
    return LabelMesh(
        name=name,
        points=corners,
        counts=(4,),
        indices=(0, 1, 2, 3),
        normals=(normal,) * 4,
        uvs=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)) if textured else (),
        textured=textured,
    )


def label_meshes(label: LabelSpec, dims: Vec3) -> list[LabelMesh]:
    """Decals for one unit of size ``dims`` (item frame: origin at the bottom center, -y faces the aisle)."""
    w, d, h = dims
    g = LABEL_GAP_M
    y_front, y_back = -d / 2 - g, d / 2 + g
    out: list[LabelMesh] = []
    if label.mode == "none":
        return out
    if label.mode == "wrap":
        z0, z1 = label.band_z[0] * h, label.band_z[1] * h
        a, b = w / 2 + 0.001, d / 2 + 0.001
        for name, start, textured in (("Label", math.pi, True), ("LabelBack", 0.0, False)):
            pts: list[Vec3] = []
            nrm: list[Vec3] = []
            uvs: list[Vec2] = []
            for i in range(WRAP_SEGMENTS + 1):
                phi = start + math.pi * i / WRAP_SEGMENTS  # front half: pi -> 2pi passes -y (the aisle side)
                c, s = math.cos(phi), math.sin(phi)
                for z, v in ((z0, 0.0), (z1, 1.0)):
                    pts.append((a * c, b * s, z))
                    nrm.append((c, s, 0.0))
                    uvs.append((i / WRAP_SEGMENTS, v))
            counts, idx = [], []
            for i in range(WRAP_SEGMENTS):
                k = 2 * i
                counts.append(4)
                idx += [k, k + 2, k + 3, k + 1]
            out.append(
                LabelMesh(
                    name=name,
                    points=tuple(pts),
                    counts=tuple(counts),
                    indices=tuple(idx),
                    normals=tuple(nrm),
                    uvs=tuple(uvs) if textured else (),
                    textured=textured,
                )
            )
        return out
    if label.mode == "top":
        hw, hd = label.cover_wd[0] * w / 2, label.cover_wd[1] * d / 2
        zt = h + g
        out.append(_quad("Label", ((-hw, -hd, zt), (hw, -hd, zt), (hw, hd, zt), (-hw, hd, zt)), (0.0, 0.0, 1.0)))
        if label.front_strip:
            sh = min(FRONT_STRIP_H_M, h * 0.5)
            sw = w * 0.45
            out.append(
                _quad(
                    "LabelFront",
                    (
                        (-sw, y_front, h - sh - 0.002),
                        (sw, y_front, h - sh - 0.002),
                        (sw, y_front, h - 0.002),
                        (-sw, y_front, h - 0.002),
                    ),
                    (0.0, -1.0, 0.0),
                )
            )
        return out
    if label.mode == "sticker":
        sw, sh = label.size_m[0] / 2, label.size_m[1] / 2
        zc = max(sh, min(h - sh, label.center_z * h))
        out.append(
            _quad(
                "Label",
                ((-sw, y_front, zc - sh), (sw, y_front, zc - sh), (sw, y_front, zc + sh), (-sw, y_front, zc + sh)),
                (0.0, -1.0, 0.0),
            )
        )
        return out
    # panel (default): front quad (+ solid sleeve on the back and both sides)
    hw, hh = label.cover_wh[0] * w / 2, label.cover_wh[1] * h / 2
    zc = max(hh, min(h - hh, label.center_z * h))
    out.append(
        _quad(
            "Label",
            ((-hw, y_front, zc - hh), (hw, y_front, zc - hh), (hw, y_front, zc + hh), (-hw, y_front, zc + hh)),
            (0.0, -1.0, 0.0),
        )
    )
    if label.sleeve:
        x_r, x_l = w / 2 + g, -w / 2 - g
        z0, z1 = zc - hh, zc + hh
        hd = d / 2
        out.append(
            _quad(
                "SleeveBack",
                ((hw, y_back, z0), (-hw, y_back, z0), (-hw, y_back, z1), (hw, y_back, z1)),
                (0.0, 1.0, 0.0),
                textured=False,
            )
        )
        out.append(
            _quad(
                "SleeveRight",
                ((x_r, -hd, z0), (x_r, hd, z0), (x_r, hd, z1), (x_r, -hd, z1)),
                (1.0, 0.0, 0.0),
                textured=False,
            )
        )
        out.append(
            _quad(
                "SleeveLeft",
                ((x_l, hd, z0), (x_l, -hd, z0), (x_l, -hd, z1), (x_l, hd, z1)),
                (-1.0, 0.0, 0.0),
                textured=False,
            )
        )
    return out


def item_to_native(p: Vec3, scale_xyz: Vec3, yaw_offset_deg: float) -> Vec3:
    """Item-frame point -> the referenced model's native (pre-scale, pre-yaw-offset) frame. The unit root is
    ``T . Rz(item yaw + yaw_offset) . S``, the item frame is ``T . Rz(item yaw)``: native = S^-1 . Rz(-offset) . p."""
    c, s = math.cos(math.radians(-yaw_offset_deg)), math.sin(math.radians(-yaw_offset_deg))
    x, y = c * p[0] - s * p[1], s * p[0] + c * p[1]
    sx, sy, sz = (v if abs(v) > 1e-9 else 1.0 for v in scale_xyz)
    return (x / sx, y / sy, p[2] / sz)


def item_normal_to_native(n: Vec3, scale_xyz: Vec3, yaw_offset_deg: float) -> Vec3:
    """Normals transform with the inverse transpose: native n ~ S . Rz(-offset) . n (normalized)."""
    c, s = math.cos(math.radians(-yaw_offset_deg)), math.sin(math.radians(-yaw_offset_deg))
    x, y = c * n[0] - s * n[1], s * n[0] + c * n[1]
    v = (x * scale_xyz[0], y * scale_xyz[1], n[2] * scale_xyz[2])
    ln = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / ln, v[1] / ln, v[2] / ln)
