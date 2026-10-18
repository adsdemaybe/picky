"""Synthetic camera frames for the 2D robot fake (Pillow), so the cameras API shows something real-looking.

- ``<robot>.front``  first-person view of the shelf the rover faces: fixture face, shelf boards, one label box per
  unit (product color + name), price tags (the PHYSICAL tag, so mispricings show), out-of-stock gaps, HUD (robot,
  pose, status, battery, current task). Scans aim the pan-tilt head at the slot (zoomed view centered on it).
- ``<robot>.wrist``  SO-101 wrist close-up of the held item, else of the slot in front.
- ``<robot>.basket`` top-down into the basket with every unit placed in it.
- ``<store>.ceiling-N`` top-down store map: fixtures, aisles, checkout, docks, every robot (colored dot + heading +
  name) and the remaining route of every moving robot.

Every frame is RAW (``blurred=False``; perception blurs before serving) and marked synthetic: a "SIM" watermark in the
image and a JPEG comment. There are never people in these images. Pure functions over plain snapshots, so the fake can
render in a worker thread (``asyncio.to_thread``) without racing the event loop.
"""

import io
import math
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from arms.robot.instances import _n_of
from contracts.catalog import Product, SimAsset
from contracts.common import Pose2D, Rect2D, angle_diff
from contracts.planogram import Fixture, Planogram, Slot, StoreCameraSpec
from contracts.sim import BASKET_SIZE_M, BBox, ItemInstance

JPEG_COMMENT = b"picky synthetic frame (2D robot fake, no people)"
JPEG_QUALITY = 80
HUD_H = 26

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 (pixels)


# ---------------------------------------------------------------------------------------------------------------------
# snapshots handed to the renderer
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class RoverView:
    robot_id: str
    name: str
    color: str
    pose: Pose2D
    status: str = "idle"
    battery: float = 100.0
    lift_m: float = 0.45
    backend: str = "fake"
    task: str | None = None
    route: list[Pose2D] = field(default_factory=list)
    dock: Pose2D | None = None
    holding: ItemInstance | None = None
    basket: list[ItemInstance] = field(default_factory=list)
    capacity: int = 12


@dataclass
class SceneView:
    plan: Planogram
    products: dict[str, Product]
    units: dict[str, list[ItemInstance]]
    tags: dict[str, int | None] = field(default_factory=dict)

    def asset(self, sku: str) -> SimAsset:
        p = self.products.get(sku)
        return p.asset if p is not None else SimAsset()

    def label(self, sku: str) -> str:
        p = self.products.get(sku)
        if p is None:
            return sku
        return p.asset.label_text or p.name


@dataclass
class Rendered:
    jpeg: bytes
    w: int
    h: int
    instances: list[ItemInstance] = field(default_factory=list)
    slot_id: str | None = None


# ---------------------------------------------------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------------------------------------------------
def _norm(xy):  # noqa: ANN001, ANN202
    if isinstance(xy, list | tuple) and len(xy) == 4 and all(isinstance(v, int | float) for v in xy):
        x0, y0, x1, y1 = xy
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return xy


class SafeDraw(ImageDraw.ImageDraw):
    """ImageDraw that accepts boxes with swapped corners (world->pixel mapping flips axes)."""

    def rectangle(self, xy, *args, **kw):  # noqa: ANN001, ANN002, ANN003, ANN201
        return super().rectangle(_norm(xy), *args, **kw)

    def rounded_rectangle(self, xy, *args, **kw):  # noqa: ANN001, ANN002, ANN003, ANN201
        b = _norm(xy)
        r = kw.get("radius", args[0] if args else 0)
        r = max(0, min(r, int((b[2] - b[0]) / 2), int((b[3] - b[1]) / 2)))
        kw["radius"] = r
        return super().rounded_rectangle(b, *args[1:], **kw)

    def ellipse(self, xy, *args, **kw):  # noqa: ANN001, ANN002, ANN003, ANN201
        return super().ellipse(_norm(xy), *args, **kw)


def new_canvas(w: int, h: int, color: tuple[int, int, int]) -> tuple[Image.Image, SafeDraw]:
    img = Image.new("RGB", (w, h), color)
    return img, SafeDraw(img)


@lru_cache(maxsize=16)
def font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: fixed bitmap font
        return ImageFont.load_default()


def rgb(hex_color: str | None, default: tuple[int, int, int] = (200, 200, 200)) -> tuple[int, int, int]:
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return default
    try:
        return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    except ValueError:
        return default


def shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    if f >= 1:
        return tuple(min(255, int(v + (255 - v) * (f - 1))) for v in c)  # type: ignore[return-value]
    return tuple(max(0, int(v * f)) for v in c)  # type: ignore[return-value]


def ink_for(c: tuple[int, int, int]) -> tuple[int, int, int]:
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    return (20, 20, 20) if lum > 140 else (250, 250, 250)


def text_w(draw: ImageDraw.ImageDraw, text: str, f) -> float:  # noqa: ANN001
    x0, _, x1, _ = draw.textbbox((0, 0), text, font=f)
    return x1 - x0


def fit_text(draw: ImageDraw.ImageDraw, text: str, f, max_w: float) -> str:  # noqa: ANN001
    if text_w(draw, text, f) <= max_w:
        return text
    while len(text) > 1 and text_w(draw, text + "…", f) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


def money(cents: int | None) -> str:
    if cents is None:
        return "--"
    return f"${cents // 100}.{cents % 100:02d}"


def clip_box(b: Box, w: int, h: int) -> BBox | None:
    x0, y0, x1, y1 = max(0.0, b[0]), max(0.0, b[1]), min(float(w), b[2]), min(float(h), b[3])
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return BBox(x=round(x0, 1), y=round(y0, 1), w=round(x1 - x0, 1), h=round(y1 - y0, 1))


def encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY, comment=JPEG_COMMENT)
    return buf.getvalue()


def watermark(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    f = font(11)
    t = "SIM · synthetic"
    tw = text_w(draw, t, f)
    draw.rectangle((w - tw - 10, h - 17, w - 2, h - 2), fill=(0, 0, 0))
    draw.text((w - tw - 6, h - 16), t, fill=(255, 210, 60), font=f)


def hud(draw: ImageDraw.ImageDraw, w: int, rover: RoverView, camera: str) -> None:
    draw.rectangle((0, 0, w, HUD_H), fill=(15, 23, 42))
    c = rgb(rover.color, (37, 99, 235))
    draw.rectangle((0, 0, 6, HUD_H), fill=c)
    f = font(12)
    left = f"{rover.name} ({rover.robot_id}) · {camera} · {rover.status.upper()}"
    deg = math.degrees(rover.pose.theta)
    p = rover.pose
    right = f"x {p.x:5.2f}  y {p.y:5.2f}  hdg {deg:4.0f}  lift {rover.lift_m:.2f}m  bat {rover.battery:.0f}%"
    draw.text((12, 6), fit_text(draw, left, f, w * 0.48), fill=(241, 245, 249), font=f)
    rw = text_w(draw, right, f)
    draw.text((w - rw - 8, 6), right, fill=(148, 163, 184), font=f)
    # battery bar under the HUD
    pct = max(0.0, min(100.0, rover.battery)) / 100.0
    bar_c = (34, 197, 94) if pct > 0.3 else (234, 179, 8) if pct > 0.1 else (239, 68, 68)
    draw.rectangle((0, HUD_H, int(w * pct), HUD_H + 2), fill=bar_c)


def task_line(draw: ImageDraw.ImageDraw, w: int, h: int, text: str | None) -> None:
    if not text:
        return
    f = font(12)
    t = fit_text(draw, text, f, w * 0.7)
    tw = text_w(draw, t, f)
    draw.rectangle((4, h - 22, tw + 14, h - 4), fill=(15, 23, 42))
    draw.text((9, h - 20), t, fill=(250, 250, 250), font=f)


def draw_unit(draw: ImageDraw.ImageDraw, box: Box, asset: SimAsset, label: str, depth_row: int, big: bool) -> None:
    c = shade(rgb(asset.label_color), 1.0 - 0.18 * min(depth_row, 3))
    edge = shade(c, 0.55)
    x0, y0, x1, y1 = box
    if asset.shape == "sphere":
        draw.ellipse(box, fill=c, outline=edge, width=2)
    elif asset.shape == "cylinder":
        r = max(2, int((x1 - x0) * 0.25))
        draw.rounded_rectangle(box, radius=r, fill=c, outline=edge, width=2)
        draw.ellipse((x0, y0, x1, y0 + max(3, (y1 - y0) * 0.08)), outline=edge, width=1)
    else:
        draw.rectangle(box, fill=c, outline=edge, width=2)
    if depth_row > 0:
        return
    bw, bh = x1 - x0, y1 - y0
    if bw < 18 or bh < 14:
        return
    # white label band with the product name
    band_h = max(10.0, bh * (0.34 if big else 0.42))
    by0 = y0 + (bh - band_h) / 2
    draw.rectangle((x0 + 2, by0, x1 - 2, by0 + band_h), fill=(250, 250, 245))
    size = 18 if big and bw > 140 else 13 if bw > 60 else 10
    f = font(size)
    words = label.split()
    lines: list[str] = []
    max_lines = max(1, int(band_h // (size + 2)))
    cur = ""
    for wd in words:
        trial = f"{cur} {wd}".strip()
        if text_w(draw, trial, f) <= bw - 6 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    ty = by0 + (band_h - len(lines) * (size + 2)) / 2
    for line in lines:
        line = fit_text(draw, line, f, bw - 6)
        tw = text_w(draw, line, f)
        draw.text((x0 + (bw - tw) / 2, ty), line, fill=shade(rgb(asset.label_color), 0.45), font=f)
        ty += size + 2


def price_tag(draw: ImageDraw.ImageDraw, cx: float, top: float, cents: int | None, deal: bool, scale: float) -> None:
    f = font(16 if scale > 400 else 12 if scale > 180 else 10)
    t = money(cents) + (" DEAL" if deal else "")
    tw = text_w(draw, t, f)
    h = (f.size if hasattr(f, "size") else 10) + 4
    bg = (253, 224, 71) if deal else (255, 255, 255)
    draw.rectangle((cx - tw / 2 - 4, top, cx + tw / 2 + 4, top + h), fill=bg, outline=(60, 60, 60))
    draw.text((cx - tw / 2, top + 1), t, fill=(20, 20, 20), font=f)


# ---------------------------------------------------------------------------------------------------------------------
# front camera (first person) + scan view
# ---------------------------------------------------------------------------------------------------------------------
FACING_TOL_RAD = 0.7
MAX_DRAWN_ROWS = 3
VIEW_DEPTH_M = 2.6


def _axes(pose: Pose2D) -> tuple[tuple[float, float], tuple[float, float]]:
    th = pose.theta
    return (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th))


def facing_slots(plan: Planogram, pose: Pose2D, max_lateral: float = 2.2) -> list[tuple[Slot, float, float]]:
    """Slots on the shelf face the rover looks at: (slot, forward m, lateral m (left +))."""
    fwd, left = _axes(pose)
    out: list[tuple[Slot, float, float]] = []
    for s in plan.slots:
        if abs(angle_diff(s.approach_pose.theta, pose.theta)) > FACING_TOL_RAD:
            continue
        dx, dy = s.item_position.x - pose.x, s.item_position.y - pose.y
        f = dx * fwd[0] + dy * fwd[1]
        lat = dx * left[0] + dy * left[1]
        if 0.05 < f < VIEW_DEPTH_M and abs(lat) < max_lateral:
            out.append((s, f, lat))
    if not out:
        return out
    near = min(f for _, f, _ in out)
    return [t for t in out if t[1] <= near + 0.6]  # only the nearest face (not the gondola behind it)


def nearest_facing_slot(plan: Planogram, pose: Pose2D) -> Slot | None:
    cands = facing_slots(plan, pose, max_lateral=0.8)
    if not cands:
        return None
    return min(cands, key=lambda t: abs(t[2]) + 0.2 * t[1])[0]


def _fixture_lat_extent(fx: Fixture, pose: Pose2D) -> tuple[float, float]:
    _, left = _axes(pose)
    r = fx.rect
    lats = [
        (x - pose.x) * left[0] + (y - pose.y) * left[1]
        for x, y in ((r.x, r.y), (r.x_max, r.y), (r.x, r.y_max), (r.x_max, r.y_max))
    ]
    return min(lats), max(lats)


def render_front(
    scene: SceneView,
    rover: RoverView,
    *,
    w: int = 640,
    h: int = 480,
    focus: Slot | None = None,
    camera_label: str = "front",
) -> Rendered:
    """First-person shelf view. With ``focus`` (scan) the head is aimed at that slot from its approach pose."""
    plan = scene.plan
    view_pose = focus.approach_pose if focus is not None else rover.pose
    img, draw = new_canvas(w, h, (226, 232, 240))
    avail = h - HUD_H

    cands = facing_slots(plan, view_pose)
    if focus is not None and all(s.slot_id != focus.slot_id for s, _, _ in cands):
        cands.append((focus, 0.38, 0.0))
    if focus is not None:
        asset = scene.asset(focus.sku)
        zc = focus.item_position.z + asset.dims_m[2] / 2
        z_span = max(0.55, asset.dims_m[2] * 3.2)
        z0 = zc - z_span * 0.47
        center_l = next((lat for s, _, lat in cands if s.slot_id == focus.slot_id), 0.0)
    else:
        z_span, z0, center_l = 2.15, -0.15, 0.0
    scale = avail / z_span  # px per meter

    def px(lat: float) -> float:
        return w / 2 - (lat - center_l) * scale

    def py(z: float) -> float:
        return h - (z - z0) * scale

    visible: list[ItemInstance] = []
    focus_units: list[ItemInstance] = []
    if not cands:
        _aisle_view(draw, w, h)
    else:
        # floor
        draw.rectangle((0, py(0.0), w, h), fill=(203, 213, 225))
        fixtures = {s.fixture_id for s, _, _ in cands}
        for fid in sorted(fixtures):
            fx = plan.fixture(fid)
            if fx is None:
                continue
            l0, l1 = _fixture_lat_extent(fx, view_pose)
            body = (px(l1), py(fx.height_m), px(l0), py(0.0))
            kind_c = {"fridge": (219, 234, 254), "freezer": (191, 219, 254), "produce_bin": (220, 252, 231)}.get(
                fx.kind, (245, 240, 230)
            )
            draw.rectangle(body, fill=kind_c, outline=(120, 113, 108), width=2)
            if fx.label:
                f = font(12 if focus is None else 14)
                lbl = fit_text(draw, fx.label, f, max(20.0, body[2] - body[0] - 8))
                draw.text((max(body[0] + 4, 4), max(py(fx.height_m) + 4, HUD_H + 6)), lbl, fill=(87, 83, 78), font=f)
            for z in fx.level_z_m or []:
                draw.rectangle((body[0], py(z), body[2], py(z) + max(3, scale * 0.025)), fill=(120, 113, 108))
        for slot, _f, lat in sorted(cands, key=lambda t: -t[2]):
            asset = scene.asset(slot.sku)
            label = scene.label(slot.sku)
            units = scene.units.get(slot.slot_id, [])
            uw, _ud, uh = asset.dims_m
            facings = max(1, slot.facings)
            fwd, left = _axes(view_pose)
            if not units:
                span = uw * 1.1 * facings
                box = (px(lat + span / 2), py(slot.item_position.z + uh), px(lat - span / 2), py(slot.item_position.z))
                draw.rectangle(box, outline=(220, 38, 38), width=2)
                f = font(12 if focus is not None else 9)
                t = fit_text(draw, "OUT OF STOCK", f, max(10.0, box[2] - box[0] - 4))
                draw.text((box[0] + 3, (box[1] + box[3]) / 2 - 6), t, fill=(220, 38, 38), font=f)
            # back rows first so the front row overlaps them
            ordered = list(enumerate(units))
            for k, u in sorted(ordered, key=lambda t: -(t[0] // facings)):
                row = min(k // facings, MAX_DRAWN_ROWS - 1)  # deeper rows hide behind the third one
                ulat = (u.position.x - view_pose.x) * left[0] + (u.position.y - view_pose.y) * left[1]
                shrink = 0.9**row
                lift = row * max(3.0, scale * 0.012)
                cx = px(ulat) + row * max(2.0, scale * 0.006)
                half = uw * scale * shrink / 2
                bottom = py(slot.item_position.z) - lift
                box = (cx - half, bottom - uh * scale * shrink, cx + half, bottom)
                hidden = k // facings >= MAX_DRAWN_ROWS
                if not hidden:
                    draw_unit(draw, box, asset, label, row, focus is not None)
                bb = clip_box(box, w, h)
                if bb is not None and not hidden:
                    visible.append(u.model_copy(update={"bbox": bb}))
                if focus is not None and slot.slot_id == focus.slot_id:
                    focus_units.append(u.model_copy(update={"bbox": bb}))
            tag_top = py(slot.item_position.z) + max(4, scale * 0.03)
            price_tag(draw, px(lat), tag_top, scene.tags.get(slot.slot_id), slot.deal is not None, scale)
        if focus is not None:
            fu = [u for u in focus_units if u.bbox is not None]
            if fu:
                x0 = min(u.bbox.x for u in fu) - 6  # type: ignore[union-attr]
                y0 = min(u.bbox.y for u in fu) - 6  # type: ignore[union-attr]
                x1 = max(u.bbox.x + u.bbox.w for u in fu) + 6  # type: ignore[union-attr]
                y1 = max(u.bbox.y + u.bbox.h for u in fu) + 6  # type: ignore[union-attr]
                draw.rectangle((x0, y0, x1, y1), outline=(37, 99, 235), width=3)
                f = font(13)
                draw.text((x0, max(HUD_H + 4, y0 - 18)), f"scan {focus.slot_id}", fill=(37, 99, 235), font=f)
    hud(draw, w, rover, camera_label)
    task_line(draw, w, h, rover.task)
    watermark(draw, w, h)
    if focus is not None:
        units_out = sorted(focus_units, key=_n_of)
        return Rendered(encode(img), w, h, units_out, focus.slot_id)
    near = nearest_facing_slot(plan, rover.pose)
    return Rendered(encode(img), w, h, visible, near.slot_id if near else None)


def _aisle_view(draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
    """No shelf face in view: a simple corridor in perspective."""
    vx, vy = w / 2, HUD_H + (h - HUD_H) * 0.42
    draw.polygon([(0, h), (w, h), (vx + 40, vy), (vx - 40, vy)], fill=(203, 213, 225))
    draw.polygon([(0, HUD_H), (vx - 40, vy - 60), (vx - 40, vy), (0, h)], fill=(231, 229, 228))
    draw.polygon([(w, HUD_H), (vx + 40, vy - 60), (vx + 40, vy), (w, h)], fill=(231, 229, 228))
    for k in range(1, 5):  # shelf boards converging to the vanishing point
        t = k / 5
        yl = HUD_H + (h - HUD_H) * t
        ye = vy - 60 + 60 * t
        draw.line([(0, yl), (vx - 40, ye)], fill=(168, 162, 158), width=2)
        draw.line([(w, yl), (vx + 40, ye)], fill=(168, 162, 158), width=2)
    draw.rectangle((vx - 40, vy - 60, vx + 40, vy), fill=(214, 211, 209))


# ---------------------------------------------------------------------------------------------------------------------
# wrist camera
# ---------------------------------------------------------------------------------------------------------------------
def render_wrist(scene: SceneView, rover: RoverView, slot: Slot | None, *, w: int = 640, h: int = 480) -> Rendered:
    img, draw = new_canvas(w, h, (231, 229, 228))
    out: list[ItemInstance] = []
    if rover.holding is not None:
        inst = rover.holding
        asset = scene.asset(inst.sku)
        uw, _, uh = asset.dims_m
        s = min((w * 0.5) / max(uw, 1e-3), (h * 0.62) / max(uh, 1e-3))
        bw, bh = uw * s, uh * s
        box = (w / 2 - bw / 2, h / 2 - bh / 2 + 10, w / 2 + bw / 2, h / 2 + bh / 2 + 10)
        draw_unit(draw, box, asset, scene.label(inst.sku), 0, True)
        jaw = (63, 63, 70)
        draw.rectangle((box[0] - 34, box[1] + bh * 0.25, box[0] - 4, box[1] + bh * 0.75), fill=jaw)
        draw.rectangle((box[2] + 4, box[1] + bh * 0.25, box[2] + 34, box[1] + bh * 0.75), fill=jaw)
        prod = scene.products.get(inst.sku)
        info = " · ".join(x for x in [prod.brand if prod else None, prod.size if prod else None, inst.sku] if x)
        f = font(14)
        draw.text((14, HUD_H + 10), fit_text(draw, f"holding {info}", f, w - 28), fill=(30, 30, 30), font=f)
        if inst.freshness is not None:
            draw.text((14, HUD_H + 30), f"freshness {inst.freshness:.2f}", fill=(22, 101, 52), font=f)
        bb = clip_box(box, w, h)
        out.append(inst.model_copy(update={"bbox": bb}))
        slot_id = inst.slot_id
    elif slot is not None:
        asset = scene.asset(slot.sku)
        units = scene.units.get(slot.slot_id, [])
        facings = max(1, min(slot.facings, len(units))) if units else max(1, slot.facings)
        front = units[:facings]
        uw, _, uh = asset.dims_m
        s = min((w * 0.8) / max(uw * 1.15 * facings, 1e-3), (h * 0.55) / max(uh, 1e-3))
        board = h * 0.78
        draw.rectangle((0, board, w, board + 10), fill=(120, 113, 108))
        if not front:
            f = font(22)
            t = "OUT OF STOCK"
            draw.text((w / 2 - text_w(draw, t, f) / 2, board - 60), t, fill=(220, 38, 38), font=f)
        for k, u in enumerate(front):
            cx = w / 2 + (k - (len(front) - 1) / 2) * uw * 1.15 * s
            box = (cx - uw * s / 2, board - uh * s, cx + uw * s / 2, board)
            draw_unit(draw, box, asset, scene.label(slot.sku), 0, True)
            out.append(u.model_copy(update={"bbox": clip_box(box, w, h)}))
        price_tag(draw, w / 2, board + 14, scene.tags.get(slot.slot_id), slot.deal is not None, 500)
        slot_id = slot.slot_id
    else:
        f = font(16)
        t = "wrist camera: nothing in view"
        draw.text((w / 2 - text_w(draw, t, f) / 2, h / 2), t, fill=(87, 83, 78), font=f)
        slot_id = None
    hud(draw, w, rover, "wrist")
    watermark(draw, w, h)
    return Rendered(encode(img), w, h, out, slot_id)


# ---------------------------------------------------------------------------------------------------------------------
# basket camera
# ---------------------------------------------------------------------------------------------------------------------
def render_basket(scene: SceneView, rover: RoverView, *, w: int = 640, h: int = 480) -> Rendered:
    img, draw = new_canvas(w, h, (39, 39, 42))
    bx, by, _ = BASKET_SIZE_M  # x forward (image up), y left (image left)
    s = min((w * 0.82) / by, (h - HUD_H - 40) * 0.92 / bx)
    cx, cy = w / 2, HUD_H + 14 + (h - HUD_H - 14) / 2
    basket = (cx - by * s / 2, cy - bx * s / 2, cx + by * s / 2, cy + bx * s / 2)
    draw.rounded_rectangle(basket, radius=12, fill=(30, 64, 175), outline=(147, 197, 253), width=4)
    for k in range(1, 6):  # wire mesh
        x = basket[0] + (basket[2] - basket[0]) * k / 6
        draw.line([(x, basket[1]), (x, basket[3])], fill=(59, 89, 196), width=1)
    out: list[ItemInstance] = []
    cell_w, cell_h = (basket[2] - basket[0]) / 3, (basket[3] - basket[1]) / 3
    for n, inst in enumerate(rover.basket):
        col, row, layer = n % 3, (n // 3) % 3, n // 9
        asset = scene.asset(inst.sku)
        uw, ud, _ = asset.dims_m
        iw = min(cell_w * 0.92, uw * s)
        ih = min(cell_h * 0.92, ud * s * 1.4)
        ccx = basket[0] + cell_w * (col + 0.5) + layer * 8
        ccy = basket[1] + cell_h * (row + 0.5) - layer * 8
        box = (ccx - iw / 2, ccy - ih / 2, ccx + iw / 2, ccy + ih / 2)
        draw_unit(draw, box, asset, scene.label(inst.sku), 0, False)
        out.append(inst.model_copy(update={"bbox": clip_box(box, w, h)}))
    f = font(14)
    draw.text((12, HUD_H + 8), f"basket {len(rover.basket)}/{rover.capacity}", fill=(241, 245, 249), font=f)
    hud(draw, w, rover, "basket")
    watermark(draw, w, h)
    return Rendered(encode(img), w, h, out, None)


# ---------------------------------------------------------------------------------------------------------------------
# ceiling camera (top-down map)
# ---------------------------------------------------------------------------------------------------------------------
FIXTURE_COLORS = {
    "shelf": (214, 196, 168),
    "fridge": (186, 230, 253),
    "freezer": (147, 197, 253),
    "produce_bin": (187, 247, 208),
    "endcap": (253, 186, 116),
    "checkout": (161, 161, 170),
}


def ceiling_window(plan: Planogram, cam: StoreCameraSpec | None) -> Rect2D:
    """Floor area a ceiling camera covers: ceiling-1 = the whole store; others a square around their mount (>= 5 m
    half-size), clamped to the store."""
    b = plan.bounds
    if cam is None or cam.camera_id.endswith(".ceiling-1"):
        return b  # ceiling-1 is the store overview
    m = cam.mount
    half = max(5.0, m.z * math.tan(math.radians(m.hfov_deg) / 2) * 2.0)
    x0, x1 = max(b.x, m.x - half), min(b.x_max, m.x + half)
    y0, y1 = max(b.y, m.y - half), min(b.y_max, m.y + half)
    if (x1 - x0) * (y1 - y0) >= 0.7 * b.w * b.h or x1 - x0 < 1 or y1 - y0 < 1:
        return b
    return Rect2D(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def render_ceiling(
    plan: Planogram,
    rovers: list[RoverView],
    cam: StoreCameraSpec | None,
    *,
    w: int = 640,
    h: int = 480,
    title: str | None = None,
) -> Rendered:
    img, draw = new_canvas(w, h, (248, 250, 252))
    win = ceiling_window(plan, cam)
    top = HUD_H + 6
    margin = 12
    s = min((w - 2 * margin) / win.w, (h - top - margin) / win.h)
    ox = (w - win.w * s) / 2
    oy = top + (h - top - margin - win.h * s) / 2

    def P(x: float, y: float) -> tuple[float, float]:
        return ox + (x - win.x) * s, oy + (win.y_max - y) * s

    def R(r: Rect2D) -> Box:
        x0, y1 = P(r.x, r.y)
        x1, y0 = P(r.x_max, r.y_max)
        return x0, y0, x1, y1

    b = plan.bounds
    draw.rectangle(R(b), fill=(241, 245, 249), outline=(71, 85, 105), width=3)
    small = font(10)
    for a in plan.aisles:
        draw.rectangle(R(a.rect), fill=(226, 232, 240))
    for fx in plan.fixtures:
        box = R(fx.rect)
        draw.rectangle(box, fill=FIXTURE_COLORS.get(fx.kind, (214, 196, 168)), outline=(120, 113, 108))
        if fx.label and (box[2] - box[0]) > 26:
            draw.text(
                (box[0] + 2, box[1] + 2),
                fit_text(draw, fx.label, small, box[2] - box[0] - 4),
                fill=(68, 64, 60),
                font=small,
            )
        elif fx.label and (box[3] - box[1]) > 40:
            lbl = fx.fixture_id
            draw.text(
                (box[0] + 1, box[1] + 2),
                fit_text(draw, lbl, small, max(8.0, box[2] - box[0])),
                fill=(68, 64, 60),
                font=small,
            )
    for ob in plan.obstacles:
        draw.rectangle(R(ob), fill=(82, 82, 91))
    for a in plan.aisles:
        x0, y0, x1, y1 = R(a.rect)
        if x1 - x0 > 30:
            draw.text((x0 + 2, y1 - 12), fit_text(draw, a.label, small, x1 - x0 - 4), fill=(100, 116, 139), font=small)
    # entrance / checkout
    for pose, label, col in ((plan.entrance, "entrance", (22, 163, 74)), (plan.checkout, "checkout", (147, 51, 234))):
        x, y = P(pose.x, pose.y)
        draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=col)
        draw.text((x + 7, y - 6), label, fill=col, font=small)
    # docks + routes + robots
    for rv in rovers:
        c = rgb(rv.color, (37, 99, 235))
        if rv.dock is not None:
            x, y = P(rv.dock.x, rv.dock.y)
            draw.rectangle((x - 6, y - 6, x + 6, y + 6), outline=c, width=2)
    for rv in rovers:
        if not rv.route:
            continue
        c = rgb(rv.color, (37, 99, 235))
        pts = [P(rv.pose.x, rv.pose.y)] + [P(p.x, p.y) for p in rv.route]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
            _dashed(draw, x0, y0, x1, y1, c)
        gx, gy = pts[-1]
        draw.ellipse((gx - 4, gy - 4, gx + 4, gy + 4), outline=c, width=2)
    from contracts.common import ROBOT_RADIUS_M

    label_f = font(12)
    placed: list[Box] = []
    for rv in rovers:
        c = rgb(rv.color, (37, 99, 235))
        x, y = P(rv.pose.x, rv.pose.y)
        r = max(5.0, ROBOT_RADIUS_M * s)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=c, outline=(15, 23, 42), width=2)
        hx, hy = x + math.cos(rv.pose.theta) * r * 1.6, y - math.sin(rv.pose.theta) * r * 1.6
        draw.line([(x, y), (hx, hy)], fill=(15, 23, 42), width=3)
        tag = f"{rv.name} {rv.status}" + (f" ·{len(rv.basket)}" if rv.basket else "")
        tw = text_w(draw, tag, label_f)
        box = (x + r + 2, y - r - 14, x + r + tw + 8, y - r + 1)
        for _ in range(6):  # nudge down until it overlaps no other robot's label
            if not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in placed):
                break
            box = (box[0], box[1] + 17, box[2], box[3] + 17)
        placed.append(box)
        draw.rectangle(box, fill=(255, 255, 255), outline=c)
        draw.text((box[0] + 3, box[1] + 1), tag, fill=(15, 23, 42), font=label_f)
    # header
    draw.rectangle((0, 0, w, HUD_H), fill=(15, 23, 42))
    hf = font(12)
    head = title or (cam.camera_id if cam else f"{plan.store_id}.ceiling")
    moving = sum(1 for rv in rovers if rv.status not in ("idle", "charging"))
    draw.text((10, 6), f"{head} · {plan.name}", fill=(241, 245, 249), font=hf)
    right = f"{len(rovers)} robots · {moving} active"
    draw.text((w - text_w(draw, right, hf) - 8, 6), right, fill=(148, 163, 184), font=hf)
    watermark(draw, w, h)
    return Rendered(encode(img), w, h, [], None)


def _dashed(draw: ImageDraw.ImageDraw, x0: float, y0: float, x1: float, y1: float, c: tuple[int, int, int]) -> None:
    d = math.hypot(x1 - x0, y1 - y0)
    if d < 1:
        return
    n = max(1, int(d / 8))
    for k in range(0, n, 2):
        a, b = k / n, min(1.0, (k + 1) / n)
        draw.line([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a), (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], fill=c, width=3)


__all__ = [
    "JPEG_COMMENT",
    "Rendered",
    "RoverView",
    "SceneView",
    "ceiling_window",
    "facing_slots",
    "nearest_facing_slot",
    "render_basket",
    "render_ceiling",
    "render_front",
    "render_wrist",
]
