"""Product thumbnails (GET /api/products/{sku}/image.jpg): what the shopper sees on a suggestion card.

1. A product photo / render in ``PICKY_PRODUCT_IMAGE_DIR`` (``<SKU>.jpg`` / ``.jpeg`` / ``.png`` / ``.webp``), e.g.
   Isaac renders of the per-item models copied from vhagar, scaled into a square.
2. Otherwise a deterministic "studio shot" of the sim prop drawn from the product master (``Product.asset``): the
   same shape (box / cylinder / sphere), proportions (``dims_m``) and label colour as the Isaac primitive, with the
   label text (brand band, name, size) on its front face, like the label texture the sim prints on each unit.

Pure Pillow (base dependency), no I/O besides the optional image folder. Synthetic images: no people, nothing to blur.
"""

from __future__ import annotations

import io
from pathlib import Path

from contracts.catalog import Product

DEFAULT_SIZE = 320
MIN_SIZE = 64
MAX_SIZE = 1024
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
JPEG_QUALITY = 88
_BG_TOP = (246, 247, 249)
_BG_BOTTOM = (226, 230, 236)


def _rgb(hex_color: str | None, default: tuple[int, int, int] = (204, 204, 204)) -> tuple[int, int, int]:
    try:
        h = (hex_color or "").lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return default


def _shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(v * f))) for v in c)  # type: ignore[return-value]


def _ink(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return (24, 24, 27) if sum(bg) > 3 * 140 else (250, 250, 250)


def _font(px: int):  # noqa: ANN202 - PIL font
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=max(6, int(px)))
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _fit(draw, text: str, px: int, max_w: float):  # noqa: ANN001, ANN202
    """(font, text) shrunk / truncated to fit ``max_w`` pixels."""
    size = px
    while size > 7:
        f = _font(size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 1
    f = _font(7)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return f, (text + "…") if text else ""


def _wrap(draw, text: str, px: int, max_w: float, max_lines: int) -> list[tuple[object, str]]:  # noqa: ANN001
    words, lines, cur = text.split(), [], ""
    f = _font(px)
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and draw.textlength(trial, font=f) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], " ".join(lines[max_lines - 1 :])]
    return [_fit(draw, ln, px, max_w) for ln in lines]


def _label(draw, box: tuple[float, float, float, float], product: Product, color: tuple[int, int, int]) -> None:  # noqa: ANN001
    """Brand band + wrapped name + size inside ``box`` (the prop's front face / label area)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 24 or h < 24:
        return
    ink = _ink(color)
    pad = max(2.0, w * 0.06)
    y = y0 + h * 0.06
    if product.brand:
        band = max(9.0, h * 0.16)
        draw.rectangle((x0 + 1, y, x1 - 1, y + band), fill=(255, 255, 255))
        f, t = _fit(draw, product.brand.upper(), int(band * 0.72), w - 2 * pad)
        draw.text(((x0 + x1) / 2, y + band / 2), t, fill=(30, 30, 30), font=f, anchor="mm")
        y += band + h * 0.05
    name_px = int(max(8.0, min(h * 0.13, w * 0.16)))
    for f, line in _wrap(draw, product.name, name_px, w - 2 * pad, 3):
        draw.text(((x0 + x1) / 2, y + name_px / 2), line, fill=ink, font=f, anchor="mm")
        y += name_px * 1.12
    if product.size and product.size.lower() not in product.name.lower() and y + name_px < y1:
        f, t = _fit(draw, product.size, int(name_px * 0.8), w - 2 * pad)
        draw.text(((x0 + x1) / 2, min(y1 - name_px * 0.6, y + name_px * 0.55)), t, fill=ink, font=f, anchor="mm")


def render_product_card(product: Product, size: int = DEFAULT_SIZE) -> bytes:
    """JPEG bytes of the sim prop's studio shot (square ``size`` px)."""
    from PIL import Image, ImageDraw

    s = max(MIN_SIZE, min(MAX_SIZE, int(size)))
    ss = 2  # supersample for smooth edges
    W = s * ss
    img = Image.new("RGB", (W, W), _BG_TOP)
    d = ImageDraw.Draw(img)
    for yy in range(W):  # vertical gradient
        t = yy / W
        d.line((0, yy, W, yy), fill=tuple(round(a + (b - a) * t) for a, b in zip(_BG_TOP, _BG_BOTTOM, strict=True)))
    asset = product.asset
    color = _rgb(asset.label_color)
    dx, dy, dz = (max(0.01, float(v)) for v in asset.dims_m)
    shape = asset.shape
    # front view (width x height) + a little depth for a 3/4 look
    depth = min(dy, dx) * 0.35 if shape == "box" else 0.0
    span = max(dx + depth, dz + depth * 0.6)
    scale = (W * 0.62) / span
    fw, fh = dx * scale, dz * scale
    dd = depth * scale
    cx, base_y = W / 2, W * 0.80
    x0, x1 = cx - fw / 2 - dd / 2, cx + fw / 2 - dd / 2
    y1, y0 = base_y, base_y - fh
    # floor shadow
    d.ellipse((cx - fw * 0.62, base_y - W * 0.025, cx + fw * 0.62, base_y + W * 0.035), fill=(200, 204, 211))
    outline = _shade(color, 0.55)
    if shape == "sphere":
        r = min(fw, fh) / 2
        cy = base_y - r
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color, outline=outline, width=ss * 2)
        d.ellipse((cx - r * 0.55, cy - r * 0.65, cx - r * 0.05, cy - r * 0.2), fill=_shade(color, 1.18))
        sticker = (cx - r * 0.5, cy + r * 0.05, cx + r * 0.5, cy + r * 0.55)
        d.rounded_rectangle(sticker, radius=ss * 6, fill=(255, 255, 255), outline=(150, 150, 150), width=ss)
        f, t = _fit(d, (product.brand or product.name).upper(), int(r * 0.2), r * 0.9)
        mid = ((sticker[0] + sticker[2]) / 2, (sticker[1] + sticker[3]) / 2)
        d.text(mid, t, fill=(30, 30, 30), font=f, anchor="mm")
        caption = product.name
        if product.size and product.size.lower() not in product.name.lower():
            caption += f" · {product.size}"
        f, t = _fit(d, caption, int(W * 0.055), W * 0.9)
        d.text((W / 2, W * 0.92), t, fill=(51, 65, 85), font=f, anchor="mm")
    elif shape == "cylinder":
        ell = max(ss * 4.0, fw * 0.16)
        d.rectangle((x0, y0, x1, y1), fill=color)
        d.ellipse((x0, y1 - ell / 2, x1, y1 + ell / 2), fill=_shade(color, 0.8), outline=outline, width=ss * 2)
        d.rectangle((x0, y0, x1, y1), fill=color)
        d.line((x0, y0, x0, y1), fill=outline, width=ss * 2)
        d.line((x1, y0, x1, y1), fill=outline, width=ss * 2)
        d.ellipse((x0, y0 - ell / 2, x1, y0 + ell / 2), fill=_shade(color, 1.15), outline=outline, width=ss * 2)
        d.rectangle((x0 + fw * 0.06, y0 + fh * 0.03, x0 + fw * 0.16, y1 - fh * 0.05), fill=_shade(color, 1.12))
        _label(d, (x0 + fw * 0.08, y0 + fh * 0.18, x1 - fw * 0.08, y1 - fh * 0.12), product, color)
    else:  # box
        top = [(x0, y0), (x1, y0), (x1 + dd, y0 - dd * 0.6), (x0 + dd, y0 - dd * 0.6)]
        side = [(x1, y0), (x1 + dd, y0 - dd * 0.6), (x1 + dd, y1 - dd * 0.6), (x1, y1)]
        d.polygon(top, fill=_shade(color, 1.15), outline=outline)
        d.polygon(side, fill=_shade(color, 0.72), outline=outline)
        d.rectangle((x0, y0, x1, y1), fill=color, outline=outline, width=ss * 2)
        _label(d, (x0 + fw * 0.06, y0 + fh * 0.08, x1 - fw * 0.06, y1 - fh * 0.08), product, color)
    out = img.resize((s, s), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _from_dir(image_dir: str | Path, sku: str, size: int) -> bytes | None:
    root = Path(image_dir)
    if not root.is_dir():
        return None
    for ext in IMAGE_EXTS:
        path = root / f"{sku}{ext}"
        if not path.is_file():
            continue
        try:
            from PIL import Image

            img = Image.open(path)
            img.load()
            img = img.convert("RGB")
            s = max(MIN_SIZE, min(MAX_SIZE, int(size)))
            img.thumbnail((s, s), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (s, s), _BG_TOP)
            canvas.paste(img, ((s - img.width) // 2, (s - img.height) // 2))
            buf = io.BytesIO()
            canvas.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue()
        except Exception:  # noqa: BLE001 - a broken file falls back to the label card
            return None
    return None


def product_image_jpeg(product: Product, size: int = DEFAULT_SIZE, image_dir: str | Path | None = None) -> bytes:
    """THE product thumbnail: a photo / render from ``image_dir`` when present, else the sim prop's studio shot."""
    if image_dir:
        found = _from_dir(image_dir, product.sku, size)
        if found is not None:
            return found
    return render_product_card(product, size)


__all__ = ["DEFAULT_SIZE", "product_image_jpeg", "render_product_card"]
