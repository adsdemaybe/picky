"""Pillow "sketch" renderer: a tiny flat-shaded 3D rasterizer for the worker WITHOUT Isaac (``--backend sketch``).

It draws the same ``SceneSpec`` / ``RoverSpec`` primitives the Isaac backend builds (boxes; cylinders and spheres as
boxes) with back-face culling, near-plane clipping and painter's-algorithm depth sorting, plus label text on product
fronts. It uses the exact ``CameraPose`` math the worker uses for ground-truth bboxes, so scan bboxes line up with the
picture. Purpose: exercise the whole Isaac link (multi-store, multi-rover, every camera) on a Mac / CI with frames that
look like a store, not a grey square. Not photoreal and not fast (~20-80 ms per 640x480 frame); fine for 2 fps.
"""

import io
import math
from typing import Any

from arms.sim.geometry import NEAR_CLIP_M, CameraPose
from arms.sim.scene_spec import LIFT_REF_M, PrimSpec, RoverSpec, SceneSpec, Vec3
from arms.sim.worker import NullBackend, grey_jpeg
from contracts.common import ARM_MOUNT_X_M, Point3, Pose2D

MAX_DRAW_DIST_M = 30.0
SKY = (236, 238, 240)
JPEG_QUALITY = 75

# box faces as corner indices (corners ordered sx, sy, sz in (-, +)), outward normal in local axes
_FACES: list[tuple[tuple[int, int, int, int], Vec3]] = [
    ((0, 1, 3, 2), (-1.0, 0.0, 0.0)),
    ((4, 6, 7, 5), (1.0, 0.0, 0.0)),
    ((0, 4, 5, 1), (0.0, -1.0, 0.0)),  # -y = label face of products
    ((2, 3, 7, 6), (0.0, 1.0, 0.0)),
    ((0, 2, 6, 4), (0.0, 0.0, -1.0)),
    ((1, 5, 7, 3), (0.0, 0.0, 1.0)),
]


def _hex(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return (200, 200, 200)


def _shade(rgb: tuple[int, int, int], n: Vec3) -> tuple[int, int, int]:
    k = 0.62 + 0.38 * max(0.0, n[2]) + 0.12 * abs(n[0]) + 0.05 * abs(n[1]) - 0.25 * max(0.0, -n[2])
    return tuple(max(0, min(255, int(c * k))) for c in rgb)  # type: ignore[return-value]


def _box(center: Vec3, size: Vec3, yaw: float, pitch: float = 0.0) -> tuple[list[Vec3], Any]:
    """World corners of a box + a function mapping a local normal to world."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)

    def rot(v: Vec3) -> Vec3:
        x, y, z = v
        x, z = x * cp + z * sp, -x * sp + z * cp  # pitch about local y
        return (x * cy - y * sy, x * sy + y * cy, z)

    corners = []
    for sx in (-0.5, 0.5):
        for syy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                d = rot((sx * size[0], syy * size[1], sz * size[2]))
                corners.append((center[0] + d[0], center[1] + d[1], center[2] + d[2]))
    return corners, rot


def _clip_near(poly: list[Vec3]) -> list[Vec3]:
    """Sutherland-Hodgman against zc >= NEAR_CLIP_M (camera coords)."""
    out: list[Vec3] = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ina, inb = a[2] >= NEAR_CLIP_M, b[2] >= NEAR_CLIP_M
        if ina:
            out.append(a)
        if ina != inb:
            t = (NEAR_CLIP_M - a[2]) / (b[2] - a[2])
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), NEAR_CLIP_M))
    return out


class SketchBackend(NullBackend):
    """NullBackend that actually draws (Pillow)."""

    camera_source = "isaac"

    def __init__(self, jpeg_quality: int = JPEG_QUALITY) -> None:
        super().__init__()
        self.jpeg_quality = jpeg_quality
        self._font_cache: dict[int, Any] = {}

    def _font(self, px: int) -> Any:
        if px not in self._font_cache:
            from PIL import ImageFont

            try:
                self._font_cache[px] = ImageFont.load_default(size=px)
            except TypeError:  # Pillow < 10.1
                self._font_cache[px] = ImageFont.load_default()
        return self._font_cache[px]

    # ---- scene -> world primitives
    def _static_prims(self, spec: SceneSpec) -> list[PrimSpec]:
        return [p for p in spec.prims if p.role != "product"]

    def _rover_prims(self, rover: RoverSpec) -> list[tuple[PrimSpec, Vec3, float]]:
        pose, lift, ext = self.robot_poses.get(rover.robot_id, (rover.dock, LIFT_REF_M, 0.12))
        c, s = math.cos(pose.theta), math.sin(pose.theta)
        out = []
        for p in rover.prims:
            x, y, z = p.center
            sx = p.size[0]
            if p.group in ("lift", "arm"):
                z += lift - LIFT_REF_M
            if p.group == "arm":  # stretch the arm along +x with the extension (authored at full stretch ~0.35 m)
                k = max(0.3, min(1.0, ext / 0.33))
                x = ARM_MOUNT_X_M + (x - ARM_MOUNT_X_M) * k
                sx = p.size[0] * (k if p.size[0] > 0.06 else 1.0)
            wx, wy = pose.x + c * x - s * y, pose.y + s * x + c * y
            out.append((p.model_copy(update={"size": (sx, p.size[1], p.size[2])}), (wx, wy, z), pose.theta + p.yaw))
        return out

    def render(self, camera_id: str, store_id: str, cam: CameraPose) -> tuple[bytes, int, int] | None:
        self.renders.append((camera_id, store_id))
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return grey_jpeg(cam.w, cam.h)
        spec = self.stores.get(store_id)
        img = Image.new("RGB", (cam.w, cam.h), SKY)
        draw = ImageDraw.Draw(img)
        if spec is None:
            return self._encode(img), cam.w, cam.h
        items: list[tuple[PrimSpec, Vec3, float]] = [(p, p.center, p.yaw) for p in self._static_prims(spec)]
        for (sid, _iid), prim in self.instance_prims.items():
            if sid != store_id:
                continue
            pos: Point3 = self.positions.get((sid, _iid), Point3(x=prim.center[0], y=prim.center[1], z=prim.center[2]))
            items.append((prim, (pos.x, pos.y, pos.z), self.yaws.get((sid, _iid), prim.yaw)))
        for rover in self.rovers.values():
            if rover.store_id == store_id:
                items += self._rover_prims(rover)

        polys: list[tuple[float, list[tuple[float, float]], tuple[int, int, int], PrimSpec | None, bool]] = []
        eye = (cam.x, cam.y, cam.z)
        _, _, fwd = cam.basis()
        for prim, center, yaw in items:
            radius = 0.5 * math.sqrt(sum(d * d for d in prim.size))
            rel = (center[0] - eye[0], center[1] - eye[1], center[2] - eye[2])
            depth = rel[0] * fwd[0] + rel[1] * fwd[1] + rel[2] * fwd[2]
            if depth < -radius or depth - radius > MAX_DRAW_DIST_M:
                continue
            corners, rot = _box(center, prim.size, yaw, prim.pitch)
            base = _hex(prim.color)
            for idx, n_local in _FACES:
                n = rot(n_local)
                fc = [corners[i] for i in idx]
                cx = sum(p[0] for p in fc) / 4
                cy = sum(p[1] for p in fc) / 4
                cz = sum(p[2] for p in fc) / 4
                if (cx - eye[0]) * n[0] + (cy - eye[1]) * n[1] + (cz - eye[2]) * n[2] >= 0:
                    continue  # back face
                cam_pts = _clip_near([cam.to_camera(p) for p in fc])
                if len(cam_pts) < 3:
                    continue
                pix = [cam.image_of(p) for p in cam_pts]
                if max(p[0] for p in pix) < 0 or min(p[0] for p in pix) > cam.w:
                    continue
                if max(p[1] for p in pix) < 0 or min(p[1] for p in pix) > cam.h:
                    continue
                d = sum(p[2] for p in cam_pts) / len(cam_pts)
                is_label = prim.role == "product" and n_local == (0.0, -1.0, 0.0)
                color = (255, 255, 250) if prim.role == "light" else _shade(base, n)
                polys.append((d, pix, color, prim, is_label))
        polys.sort(key=lambda t: -t[0])
        for _d, pix, color, prim, is_label in polys:
            draw.polygon(pix, fill=color, outline=tuple(max(0, c - 40) for c in color))
            if is_label and prim is not None:
                self._label(draw, pix, prim)
        return self._encode(img), cam.w, cam.h

    def _label(self, draw: Any, pix: list[tuple[float, float]], prim: PrimSpec) -> None:
        x0, x1 = min(p[0] for p in pix), max(p[0] for p in pix)
        y0, y1 = min(p[1] for p in pix), max(p[1] for p in pix)
        w, h = x1 - x0, y1 - y0
        if w < 28 or h < 16:
            return
        text = (prim.label_text or prim.sku or "")[: max(3, int(w / 7))]
        fill = (20, 20, 20) if sum(_hex(prim.color)) > 3 * 140 else (250, 250, 250)
        px = max(8, min(22, int(h / 4)))
        draw.text(((x0 + x1) / 2, y0 + h * 0.35), text, fill=fill, font=self._font(px), anchor="mm")
        if prim.price_cents is not None and h > 40:
            price = f"${prim.price_cents // 100}.{prim.price_cents % 100:02d}"
            draw.text(((x0 + x1) / 2, y0 + h * 0.72), price, fill=fill, font=self._font(max(8, px - 2)), anchor="mm")

    def _encode(self, img: Any) -> bytes:
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=self.jpeg_quality)
        return out.getvalue()


def render_overview_png(backend: SketchBackend, store_id: str, pose: Pose2D, path: str) -> None:  # pragma: no cover
    """Debug helper: save one frame from ``pose`` (front-camera height) to ``path``."""
    from arms.sim.geometry import robot_camera_pose

    jpeg = backend.render("debug", store_id, robot_camera_pose("front", pose))
    if jpeg:
        with open(path, "wb") as f:
            f.write(jpeg[0])
