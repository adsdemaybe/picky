"""Readable product label textures (Pillow): brand, name, size, price and a real EAN-13 barcode.

The Isaac backend writes one PNG per SKU (``label_png``) and maps it onto a label quad on each unit's -y face (the face
that points out of the shelf, ``scene_spec``), so the robot's cameras see readable text and a decodable barcode
(perception's zxing-cpp barcode stage works on sim frames). The Pillow sketch renderer uses ``label_lines`` for the
same text. Pillow is a base dependency; every function degrades to None / text-only when it is missing.
"""

import io
from collections.abc import Sequence

from contracts.common import money

# EAN-13 encoding tables (left half odd/even parity, right half), and the first-digit parity patterns.
_L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
_G = ["0100111", "0110011", "0011011", "0100001", "0011101", "0111001", "0000101", "0010001", "0001001", "0010111"]
_R = ["1110010", "1100110", "1101100", "1000010", "1011100", "1001110", "1010000", "1000100", "1001000", "1110100"]
_PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def ean13_modules(code: str) -> str:
    """EAN-13 digits -> the 95 bar modules ('1' = bar). Raises ValueError for anything but 13 digits."""
    if len(code) != 13 or not code.isdigit():
        raise ValueError(f"EAN-13 needs 13 digits, got {code!r}")
    first, left, right = int(code[0]), code[1:7], code[7:]
    bits = "101"
    for d, parity in zip(left, _PARITY[first], strict=True):
        bits += (_L if parity == "L" else _G)[int(d)]
    bits += "01010"
    for d in right:
        bits += _R[int(d)]
    return bits + "101"


def label_lines(
    name: str, brand: str | None = None, size: str | None = None, price_cents: int | None = None
) -> list[str]:
    """Text printed on a label, top to bottom."""
    lines = [brand.upper()] if brand else []
    lines.append(name)
    if size:
        lines.append(size)
    if price_cents is not None:
        lines.append(money(price_cents))
    return lines


def _font(size: int):  # noqa: ANN202 - PIL font object
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(text: str, width_chars: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [text]


def _hex(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _ink(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return (20, 20, 20) if sum(bg) > 3 * 140 else (250, 250, 250)


def label_image(
    name: str,
    *,
    brand: str | None = None,
    size: str | None = None,
    price_cents: int | None = None,
    barcode: str | None = None,
    color: str = "#cccccc",
    px: Sequence[int] = (512, 384),
):  # noqa: ANN201 - PIL.Image.Image | None
    """Render a label as a PIL RGB image (None when Pillow is missing). Layout: brand band, wrapped name, size,
    price tag, EAN-13 bars with digits."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    w, h = int(px[0]), int(px[1])
    bg = _hex(color)
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    ink = _ink(bg)
    y = int(h * 0.04)
    if brand:
        band_h = int(h * 0.14)
        d.rectangle([0, y, w, y + band_h], fill=(255, 255, 255))
        d.text((w // 2, y + band_h // 2), brand.upper(), fill=(30, 30, 30), font=_font(int(band_h * 0.7)), anchor="mm")
        y += band_h + int(h * 0.03)
    name_font_px = int(h * 0.11)
    for line in _wrap(name, max(8, int(w / (name_font_px * 0.55))))[:3]:
        d.text((w // 2, y + name_font_px // 2), line, fill=ink, font=_font(name_font_px), anchor="mm")
        y += int(name_font_px * 1.1)
    if size:
        d.text((w // 2, y + int(h * 0.04)), size, fill=ink, font=_font(int(h * 0.07)), anchor="mm")
    bars_top = int(h * 0.68)
    if price_cents is not None:
        tag = money(price_cents)
        tw, th = int(w * 0.34), int(h * 0.16)
        d.rectangle([w - tw - 8, bars_top - th - 6, w - 8, bars_top - 6], fill=(255, 221, 0), outline=(0, 0, 0))
        d.text((w - tw // 2 - 8, bars_top - th // 2 - 6), tag, fill=(0, 0, 0), font=_font(int(th * 0.7)), anchor="mm")
    if barcode:
        try:
            bits = ean13_modules(barcode)
        except ValueError:
            bits = ""
        if bits:
            quiet = 9
            mod = max(1, int((w * 0.55) / (95 + 2 * quiet)))
            bw = (95 + 2 * quiet) * mod
            x0 = int(w * 0.04)
            d.rectangle([x0, bars_top, x0 + bw, h - 6], fill=(255, 255, 255))
            bar_bottom = h - 6 - int(h * 0.07)
            for i, bit in enumerate(bits):
                if bit == "1":
                    bx = x0 + (quiet + i) * mod
                    d.rectangle([bx, bars_top + 4, bx + mod - 1, bar_bottom], fill=(0, 0, 0))
            d.text(
                (x0 + bw // 2, bar_bottom + int(h * 0.035)),
                barcode,
                fill=(0, 0, 0),
                font=_font(int(h * 0.055)),
                anchor="mm",
            )
    return img


def label_png(name: str, **kw: object) -> bytes | None:
    """``label_image`` encoded as PNG bytes (None when Pillow is missing)."""
    img = label_image(name, **kw)  # type: ignore[arg-type]
    if img is None:
        return None
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def sign_png(text: str, *, color: str = "#e20074", text_color: str = "#ffffff", aspect: float = 4.0) -> bytes | None:
    """A store sign: ``text`` centered and as large as fits, on a solid ``color`` panel (PNG bytes; None without
    Pillow). ``aspect`` = width / height of the sign face."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    h = 256
    w = max(h, int(h * aspect))
    img = Image.new("RGB", (w, h), _hex(color))
    d = ImageDraw.Draw(img)
    px = int(h * 0.62)
    while px > 12:
        box = d.textbbox((0, 0), text, font=_font(px))
        if box[2] - box[0] <= w * 0.9 and box[3] - box[1] <= h * 0.8:
            break
        px -= 4
    d.text((w // 2, h // 2), text, fill=_hex(text_color), font=_font(px), anchor="mm")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _draw_item(d, kind: str, box: tuple[int, int, int, int], body: tuple[int, int, int]) -> None:  # noqa: ANN001
    """Product silhouette for a package window: a simple flat drawing per accessory kind inside ``box``."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    dark, edge = (40, 42, 46), _mix(body, (0, 0, 0), 0.35)
    if kind in ("cases", "screen_protectors", "phones", "tablets"):
        pw = min(w * 0.55, h * 0.47)
        ph = pw * 2.05
        r = int(pw * 0.16)
        px0, py0 = int(cx - pw / 2), int(cy - ph / 2)
        if kind == "screen_protectors":  # glass sheet over a dark phone, with a glare stripe
            d.rounded_rectangle([px0, py0, px0 + pw, py0 + ph], r, fill=dark)
            d.rounded_rectangle([px0 + 4, py0 + 4, px0 + pw - 4, py0 + ph - 4], r, fill=(170, 205, 230))
            d.polygon(
                [
                    (px0 + pw * 0.2, py0 + ph),
                    (px0 + pw * 0.55, py0 + ph),
                    (px0 + pw, py0 + ph * 0.45),
                    (px0 + pw, py0 + ph * 0.25),
                ],
                fill=(225, 240, 250),
            )
            d.ellipse([cx - 4, py0 + 8, cx + 4, py0 + 16], fill=dark)
            return
        d.rounded_rectangle(
            [px0, py0, px0 + pw, py0 + ph], r, fill=_mix(body, (255, 255, 255), 0.15), outline=edge, width=3
        )
        bump = pw * 0.42  # camera bump, top left
        bx, by = px0 + pw * 0.1, py0 + pw * 0.1
        d.rounded_rectangle([bx, by, bx + bump, by + bump], int(bump * 0.25), fill=edge)
        lens = bump * 0.32
        for lx, ly in ((0.28, 0.28), (0.28, 0.72), (0.72, 0.5)):
            ex, ey = bx + bump * lx, by + bump * ly
            d.ellipse([ex - lens / 2, ey - lens / 2, ex + lens / 2, ey + lens / 2], fill=dark)
        if kind == "cases":  # MagSafe ring
            rr = pw * 0.26
            d.ellipse([cx - rr, cy + ph * 0.05 - rr, cx + rr, cy + ph * 0.05 + rr], outline=edge, width=3)
        return
    if kind == "power_banks":
        s = min(w, h) * 0.32
        d.rounded_rectangle(
            [cx - s * 0.75, cy - s * 1.35, cx + s * 0.75, cy + s * 1.35],
            int(s * 0.25),
            fill=body,
            outline=_mix(body, (255, 255, 255), 0.4),
            width=3,
        )
        for i in range(4):  # charge LEDs
            d.ellipse(
                [cx - s * 0.45 + i * s * 0.3 - 3, cy + s * 0.9 - 3, cx - s * 0.45 + i * s * 0.3 + 3, cy + s * 0.9 + 3],
                fill=(80, 200, 255),
            )
        return
    if kind == "chargers":
        s = min(w, h) * 0.5
        d.rounded_rectangle(
            [cx - s / 2, cy - s * 0.35, cx + s / 2, cy + s * 0.65],
            int(s * 0.18),
            fill=(250, 250, 250),
            outline=(150, 150, 150),
            width=3,
        )
        for dx in (-0.18, 0.18):  # prongs
            d.rectangle([cx + s * dx - 4, cy - s * 0.65, cx + s * dx + 4, cy - s * 0.35], fill=(170, 170, 170))
        d.rounded_rectangle([cx - s * 0.12, cy + s * 0.1, cx + s * 0.12, cy + s * 0.2], 3, fill=dark)  # USB-C port
        return
    if kind == "audio":
        s = min(w, h) * 0.3
        d.rounded_rectangle(
            [cx - s * 1.3, cy - s * 0.2, cx + s * 1.3, cy + s * 1.3],
            int(s * 0.6),
            fill=(250, 250, 250),
            outline=(160, 160, 160),
            width=3,
        )  # case
        for dx in (-0.55, 0.55):  # buds
            ex = cx + s * dx
            d.ellipse(
                [ex - s * 0.4, cy - s * 1.2, ex + s * 0.4, cy - s * 0.4],
                fill=body if sum(body) < 300 else (245, 245, 245),
                outline=(120, 120, 120),
                width=2,
            )
            d.rounded_rectangle(
                [ex - s * 0.12, cy - s * 0.6, ex + s * 0.12, cy + s * 0.1],
                4,
                fill=(235, 235, 235),
                outline=(120, 120, 120),
                width=2,
            )
        return
    if kind == "wearables":
        s = min(w, h) * 0.32
        d.rounded_rectangle([cx - s * 0.45, cy - s * 1.5, cx + s * 0.45, cy + s * 1.5], 6, fill=_mix(body, dark, 0.4))
        d.rounded_rectangle(
            [cx - s * 0.7, cy - s * 0.8, cx + s * 0.7, cy + s * 0.8],
            int(s * 0.3),
            fill=dark,
            outline=(150, 150, 150),
            width=3,
        )
        d.text((cx, cy), "10:09", fill=(240, 240, 240), font=_font(int(s * 0.45)), anchor="mm")
        return
    if kind == "home_internet":
        s = min(w, h) * 0.3
        d.rounded_rectangle(
            [cx - s * 0.6, cy - s * 1.4, cx + s * 0.6, cy + s * 1.4],
            int(s * 0.4),
            fill=(248, 248, 248),
            outline=(160, 160, 160),
            width=3,
        )
        d.ellipse([cx - 6, cy - s * 0.9, cx + 6, cy - s * 0.9 + 12], fill=(226, 0, 116))
        return
    # cables and anything else: a coiled cable
    s = min(w, h) * 0.22
    for i in range(3):
        d.ellipse([cx - s - i * 6, cy - s - i * 6, cx + s + i * 6, cy + s + i * 6], outline=(245, 245, 245), width=5)
    d.rectangle([cx + s + 10, cy - 8, cx + s + 34, cy + 8], fill=(245, 245, 245), outline=(150, 150, 150))


def package_kind(subcategory: str | None, tags: Sequence[str] = ()) -> str | None:
    """Package style for a product, or None for a plain shelf label (groceries, phones drawn as models)."""
    if subcategory in (None, "phones") or subcategory not in PACKAGED:
        return None
    if subcategory == "chargers":
        return "cables" if "cable" in tags else "power_banks" if "battery" in tags else "chargers"
    return subcategory


PACKAGED = ("cases", "screen_protectors", "chargers", "audio", "wearables", "home_internet", "tablets")


def package_image(
    name: str,
    *,
    kind: str,
    brand: str | None = None,
    size: str | None = None,
    price_cents: int | None = None,
    barcode: str | None = None,
    color: str = "#cccccc",
    px: Sequence[int] = (320, 600),
):  # noqa: ANN201 - PIL.Image.Image | None
    """Retail package front (portrait): brand wordmark, a product window showing the item, name, a small price sticker
    and a small EAN-13 at the bottom (still decodable). ``kind`` = the product subcategory (cases, chargers, ...)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    w, h = int(px[0]), int(px[1])
    bg = _hex(color)
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    for y in range(h):  # soft vertical gradient: printed card, not a flat paint chip
        d.line([(0, y), (w, y)], fill=_mix(bg, _mix(bg, (0, 0, 0), 0.18), y / h))
    ink = _ink(bg)
    m = int(w * 0.07)
    fh = min(h, int(w * 1.9))  # font scale: tall-box proportions, so square boxes don't get tiny text
    if brand:
        d.text((m, int(h * 0.055)), brand, fill=ink, font=_font(int(fh * 0.055)), anchor="lm")
    win = (m, int(h * 0.1), w - m, int(h * 0.6))
    d.rounded_rectangle(
        win, int(w * 0.06), fill=_mix(bg, (255, 255, 255), 0.55), outline=_mix(bg, (0, 0, 0), 0.2), width=2
    )
    _draw_item(d, kind, win, bg)
    y = int(h * 0.635)
    nf = int(fh * 0.04)
    for line in _wrap(name, max(10, int((w - 2 * m) / (nf * 0.55))))[:3]:
        d.text((m, y + nf // 2), line, fill=ink, font=_font(nf), anchor="lm")
        y += int(nf * 1.15)
    if size:
        d.text((m, y + nf // 2), size, fill=ink, font=_font(int(nf * 0.8)), anchor="lm")
    bottom = h - int(h * 0.02)
    if barcode:
        try:
            bits = ean13_modules(barcode)
        except ValueError:
            bits = ""
        if bits:
            quiet, mod = 9, max(1, int((w * 0.5) / 113))
            bw, bh = 113 * mod, int(fh * 0.1)
            x0, top = m, bottom - bh
            d.rectangle([x0, top, x0 + bw, bottom], fill=(255, 255, 255))
            for i, bit in enumerate(bits):
                if bit == "1":
                    bx = x0 + (quiet + i) * mod
                    d.rectangle([bx, top + 3, bx + mod - 1, bottom - int(bh * 0.25)], fill=(0, 0, 0))
            d.text(
                (x0 + bw // 2, bottom - int(bh * 0.12)),
                barcode,
                fill=(0, 0, 0),
                font=_font(int(bh * 0.18)),
                anchor="mm",
            )
    if price_cents is not None:
        tw, th = int(w * 0.34), int(fh * 0.06)
        x1 = w - m
        d.rounded_rectangle([x1 - tw, bottom - th, x1, bottom], 6, fill=(255, 255, 255), outline=(0, 0, 0))
        d.text(
            (x1 - tw // 2, bottom - th // 2),
            money(price_cents),
            fill=(0, 0, 0),
            font=_font(int(th * 0.62)),
            anchor="mm",
        )
    return img


def package_png(name: str, **kw: object) -> bytes | None:
    """``package_image`` encoded as PNG bytes (None when Pillow is missing)."""
    img = package_image(name, **kw)  # type: ignore[arg-type]
    if img is None:
        return None
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
