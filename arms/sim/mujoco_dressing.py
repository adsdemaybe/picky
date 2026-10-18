"""Visual-only store dressing for the MuJoCo backend, generated from the planogram fixtures (``--backend mujoco``).

``scene_spec`` gives every fixture the minimum the worker needs (shelf boards, a back panel, a top), which reads as
floating plates on camera. This adds what a real grocery fixture has, without touching the shared scene spec, the
routing grid or the data hash (nothing here collides, nothing here is ground truth):

- shelves / endcaps: end panels, a toe kick, a price-strip lip on every board, uprights, a lit header sign with the
  fixture label;
- fridge / freezer: open multideck coolers (dark cabinet, lit canopy sign, LED strip under every shelf, an air-curtain
  grille at the front, white or frost-blue interior);
- checkout: belt, register monitor on a pole, card reader, bagging shelf;
- floor tiles and a wall base trim.

Store-local coordinates, like the scene spec; the backend adds the store offset.
"""

import math
import os
import tempfile
from pathlib import Path

from contracts.planogram import Fixture, Planogram

SIGN_DIR = Path(os.environ.get("PICKY_SIM_LABEL_DIR") or tempfile.gettempdir()) / "picky-mj-signs"
VISUAL = 'contype="0" conaffinity="0"'


def _axis(yaw: float) -> tuple[int, int]:
    """Pick-face direction of a fixture (same convention as scene_spec._axis)."""
    c, s = math.cos(yaw), math.sin(yaw)
    return (1 if c > 0.5 else -1 if c < -0.5 else 0, 1 if s > 0.5 else -1 if s < -0.5 else 0)


def _box(c: tuple[float, float, float], size: tuple[float, float, float], rgba: str = "", material: str = "") -> str:
    mat = f' material="{material}"' if material else ""
    col = f' rgba="{rgba}"' if rgba else ""
    hx, hy, hz = (max(1e-3, v / 2) for v in size)
    pos = f"{c[0]:.4f} {c[1]:.4f} {c[2]:.4f}"
    return f'<geom type="box" pos="{pos}" size="{hx:.4f} {hy:.4f} {hz:.4f}"{col}{mat} {VISUAL}/>'


def _sign_png(text: str, fg: tuple[int, int, int], bg: tuple[int, int, int], name: str) -> str:
    from PIL import Image, ImageDraw, ImageFont

    SIGN_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGN_DIR / f"{name}.png"
    img = Image.new("RGB", (1024, 128), bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    d.text((512, 64), text.upper()[:34], fill=fg, font=font, anchor="mm")
    img.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(path)  # MuJoCo maps the box's facing side mirrored
    return str(path)


class Dressing:
    """Collects the MJCF for one model build: ``asset_xml()`` + ``geoms_xml()``."""

    def __init__(self) -> None:
        self.assets: list[str] = [
            '<texture name="d.tiles" type="2d" builtin="checker" rgb1="0.90 0.89 0.86" rgb2="0.84 0.83 0.80" '
            'width="256" height="256" mark="edge" markrgb="0.70 0.69 0.66"/>',
            '<material name="d.tiles" texture="d.tiles" texrepeat="0.83 0.83" texuniform="true" '
            'specular="0.25" shininess="0.4" reflectance="0.03"/>',
            '<material name="d.wall" rgba="0.93 0.92 0.88 1" specular="0.05"/>',
            '<material name="d.metal" rgba="0.80 0.81 0.83 1" specular="0.5" shininess="0.6"/>',
            '<material name="d.dark" rgba="0.16 0.17 0.19 1" specular="0.3" shininess="0.4"/>',
            '<material name="d.led" rgba="1 1 0.97 1" emission="1"/>',
            '<material name="d.screen" rgba="0.20 0.55 0.95 1" emission="0.8"/>',
            '<material name="d.belt" rgba="0.09 0.09 0.10 1" specular="0.1"/>',
        ]
        self.geoms: list[str] = []
        self._signs = 0

    def material_for_role(self, role: str) -> str:
        return {"floor": "d.tiles", "wall": "d.wall"}.get(role, "")

    def _sign(self, text: str, fg, bg, c, size, face: tuple[int, int]) -> None:
        """A flat sign whose texture faces ``face`` (unit axis in the floor plane)."""
        self._signs += 1
        name = f"d.sign{self._signs}"
        png = _sign_png(text, fg, bg, f"{os.getpid()}_{self._signs}")
        self.assets.append(f'<texture name="{name}" type="2d" file="{png}"/>')
        self.assets.append(f'<material name="{name}" texture="{name}" emission="0.35"/>')
        # a thin box whose +y local face carries the texture: yaw it so +y points along `face`
        yaw = math.atan2(face[1], face[0]) - math.pi / 2
        q = (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))
        w, t, h = size
        self.geoms.append(
            f'<geom type="box" pos="{c[0]:.4f} {c[1]:.4f} {c[2]:.4f}" size="{w / 2:.4f} {t / 2:.4f} {h / 2:.4f}" '
            f'quat="{q[0]:.6f} {q[1]:.6f} {q[2]:.6f} {q[3]:.6f}" material="{name}" {VISUAL}/>'
        )

    # ---- per store
    def add_store(self, plan: Planogram, offset: tuple[float, float, float], table_ids: set[str]) -> None:
        ox, oy, oz = offset
        b = plan.bounds
        # wall base trim (store perimeter)
        for c, s in (
            ((b.x + b.w / 2, b.y + 0.02, 0.06), (b.w, 0.04, 0.12)),
            ((b.x + b.w / 2, b.y + b.h - 0.02, 0.06), (b.w, 0.04, 0.12)),
            ((b.x + 0.02, b.y + b.h / 2, 0.06), (0.04, b.h, 0.12)),
            ((b.x + b.w - 0.02, b.y + b.h / 2, 0.06), (0.04, b.h, 0.12)),
        ):
            self.geoms.append(_box((c[0] + ox, c[1] + oy, c[2] + oz), s, rgba="0.35 0.36 0.38 1"))
        for fx in plan.fixtures:
            if fx.fixture_id in table_ids:
                continue
            if fx.kind in ("shelf", "endcap"):
                self._shelf(fx, (ox, oy, oz))
            elif fx.kind in ("fridge", "freezer"):
                self._cooler(fx, (ox, oy, oz))
            elif fx.kind == "checkout":
                self._checkout(fx, (ox, oy, oz))

    def _frame(self, fx: Fixture):
        r = fx.rect
        ax, ay = _axis(fx.yaw)
        along_x = ax == 0  # the run is along x when the pick face looks along y
        length = r.w if along_x else r.h
        depth = r.h if along_x else r.w
        cx, cy = r.x + r.w / 2, r.y + r.h / 2
        fronts = [(ax, ay), (-ax, -ay)] if fx.double_sided else [(ax, ay)]
        return r, along_x, length, depth, cx, cy, fronts

    def _levels(self, fx: Fixture) -> list[float]:
        return [z for z in (fx.level_z_m or []) if z > 0.05]

    def _shelf(self, fx: Fixture, off) -> None:
        ox, oy, oz = off
        r, along_x, length, depth, cx, cy, fronts = self._frame(fx)
        h = fx.height_m

        def P(u: float, v: float, z: float) -> tuple[float, float, float]:
            """(along the run, across the depth, up) -> store frame + offset."""
            return (cx + u + ox, cy + v + oy, z + oz) if along_x else (cx + v + ox, cy + u + oy, z + oz)

        def S(lu: float, lv: float, lz: float) -> tuple[float, float, float]:
            return (lu, lv, lz) if along_x else (lv, lu, lz)

        # end panels and uprights every ~1.25 m
        for u in (-length / 2 + 0.015, length / 2 - 0.015):
            self.geoms.append(_box(P(u, 0, h / 2), S(0.03, depth, h), material="d.metal"))
        n = max(1, round(length / 1.25))
        for k in range(1, n):
            u = -length / 2 + k * length / n
            self.geoms.append(_box(P(u, 0, h / 2), S(0.04, 0.05, h), material="d.metal"))
        for fu, fv in fronts:
            sgn = fv if along_x else fu
            edge = sgn * (depth / 2 if not fx.double_sided else depth / 2)
            # toe kick + price-strip lips at every level
            self.geoms.append(_box(P(0, edge - sgn * 0.03, 0.06), S(length, 0.06, 0.12), material="d.dark"))
            for z in self._levels(fx):
                self.geoms.append(
                    _box(P(0, edge - sgn * 0.01, z - 0.02), S(length, 0.02, 0.045), rgba="0.97 0.97 0.95 1")
                )
                self.geoms.append(
                    _box(P(0, edge - sgn * 0.002, z - 0.015), S(length, 0.004, 0.022), rgba="0.98 0.82 0.20 1")
                )
            if fx.label:
                face = (0, int(sgn)) if along_x else (int(sgn), 0)
                self._sign(
                    fx.label,
                    (255, 255, 255),
                    (32, 105, 70),
                    P(0, edge - sgn * 0.02, h + 0.16),
                    S(min(length, 3.0), 0.02, 0.26) if along_x else (min(length, 3.0), 0.02, 0.26),
                    face,
                )

    def _cooler(self, fx: Fixture, off) -> None:
        ox, oy, oz = off
        r, along_x, length, depth, cx, cy, fronts = self._frame(fx)
        h = fx.height_m
        frost = fx.kind == "freezer"
        interior = "0.82 0.90 0.98 1" if frost else "0.96 0.96 0.95 1"

        def P(u, v, z):
            return (cx + u + ox, cy + v + oy, z + oz) if along_x else (cx + v + ox, cy + u + oy, z + oz)

        def S(lu, lv, lz):
            return (lu, lv, lz) if along_x else (lv, lu, lz)

        fu, fv = fronts[0]
        sgn = fv if along_x else fu
        front = sgn * depth / 2
        # cabinet: back + sides + canopy in dark, white/frost interior back
        self.geoms.append(_box(P(0, -front + sgn * 0.03, h / 2), S(length, 0.06, h + 0.3), material="d.dark"))
        self.geoms.append(_box(P(0, -front + sgn * 0.065, h / 2), S(length - 0.08, 0.01, h - 0.1), rgba=interior))
        for u in (-length / 2 + 0.03, length / 2 - 0.03):
            self.geoms.append(_box(P(u, 0, (h + 0.3) / 2), S(0.06, depth, h + 0.3), material="d.dark"))
        self.geoms.append(_box(P(0, 0, h + 0.15), S(length, depth, 0.3), material="d.dark"))
        # air-curtain grille + base, LED strips under each shelf front
        self.geoms.append(_box(P(0, front - sgn * 0.08, 0.15), S(length, 0.16, 0.30), material="d.dark"))
        self.geoms.append(_box(P(0, front - sgn * 0.08, 0.305), S(length - 0.1, 0.12, 0.01), material="d.metal"))
        for z in self._levels(fx):
            self.geoms.append(
                _box(P(0, front - sgn * 0.04, z - 0.03), S(length - 0.12, 0.015, 0.012), material="d.led")
            )
            self.geoms.append(
                _box(P(0, front - sgn * 0.01, z - 0.02), S(length - 0.1, 0.02, 0.045), rgba="0.97 0.97 0.95 1")
            )
        label = fx.label or ("Frozen" if frost else "Dairy & Chilled")
        face = (0, int(sgn)) if along_x else (int(sgn), 0)
        bg = (20, 70, 140) if frost else (22, 120, 160)
        self._sign(
            label,
            (255, 255, 255),
            bg,
            P(0, front - sgn * 0.01, h + 0.15),
            S(min(length - 0.2, 4.0), 0.02, 0.24) if along_x else (min(length - 0.2, 4.0), 0.02, 0.24),
            face,
        )

    def _checkout(self, fx: Fixture, off) -> None:
        ox, oy, oz = off
        r, along_x, length, depth, cx, cy, fronts = self._frame(fx)
        h = fx.height_m

        def P(u, v, z):
            return (cx + u + ox, cy + v + oy, z + oz) if along_x else (cx + v + ox, cy + u + oy, z + oz)

        def S(lu, lv, lz):
            return (lu, lv, lz) if along_x else (lv, lu, lz)

        self.geoms.append(_box(P(-length * 0.1, 0, h + 0.006), S(length * 0.6, depth * 0.7, 0.012), material="d.belt"))
        self.geoms.append(
            _box(P(length * 0.35, 0, h + 0.004), S(length * 0.25, depth * 0.8, 0.008), material="d.metal")
        )
        mx = length * 0.15
        self.geoms.append(_box(P(mx, -depth * 0.3, h + 0.2), S(0.03, 0.03, 0.4), material="d.metal"))
        self.geoms.append(_box(P(mx, -depth * 0.3, h + 0.45), S(0.32, 0.03, 0.22), material="d.dark"))
        self.geoms.append(_box(P(mx, -depth * 0.3 + 0.017, h + 0.45), S(0.29, 0.004, 0.19), material="d.screen"))
        self.geoms.append(_box(P(mx + 0.25, depth * 0.3, h + 0.06), S(0.09, 0.07, 0.12), material="d.dark"))
        face = fronts[0]
        self._sign(
            "Checkout",
            (255, 255, 255),
            (200, 40, 50),
            P(0, 0, h + 0.9),
            S(1.2, 0.02, 0.25) if along_x else (1.2, 0.02, 0.25),
            (face[0] or 0, face[1] or -1),
        )

    def asset_xml(self) -> str:
        return "".join(self.assets)

    def geoms_xml(self) -> str:
        return "".join(self.geoms)
