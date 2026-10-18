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
