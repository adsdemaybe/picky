"""Cut a `teleop_app.py --demo --record` run into the demo video: full cut, 45-60 s highlight, GIF preview.

Reads RUN/frames/<camera>/<n>.jpg (30 fps of sim time), RUN/frames.jsonl (rover poses + phases per frame),
RUN/events.jsonl (picks, releases, replans), RUN/plan.json (Gemini's plan) and RUN/topdown.jpg (mini-map). Adds the
title card, Gemini plan card, per-leg captions with Gemini's reasons, a live mini-map, the gripper-cam
picture-in-picture, the checkout "Paid" card and the outro. Plain pillow + numpy + imageio-ffmpeg:

    python demo/edit_video.py RUN OUT_DIR            # OUT_DIR/picky_demo_full.mp4, picky_demo_highlight.mp4, .gif
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
COLORS = {0: (255, 140, 30), 1: (60, 170, 255), 2: (180, 90, 255)}
GREEN = (60, 200, 110)
FONT_PATHS = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans{b}.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans{b}.ttf",
    "/System/Library/Fonts/Supplemental/Arial{B}.ttf",
]


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False):
    for p in FONT_PATHS:
        f = p.format(b="-Bold" if bold else "", B=" Bold" if bold else "")
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def wrap_text(draw, text, fnt, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def panel(img, box, alpha=170, radius=18):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(box, radius=radius, fill=(12, 14, 20, alpha))
    img.alpha_composite(ov)


class Run:
    def __init__(self, run: Path):
        self.dir = run
        self.frames = [json.loads(ln) for ln in (run / "frames.jsonl").read_text().splitlines() if ln.strip()]
        self.events = [json.loads(ln) for ln in (run / "events.jsonl").read_text().splitlines() if ln.strip()]
        self.plan = json.loads((run / "plan.json").read_text())
        self.ts = [f["t"] for f in self.frames]
        self.names = [r["name"] for r in self.frames[0]["rovers"]]
        self.items = self.plan["items"]
        self.total = {n: len(self.plan["robots"].get(n, [])) for n in self.names}
        self.paths = {n: [] for n in self.names}  # (t, points)
        self.legs = {n: [] for n in self.names}  # (t, event)
        for e in self.events:
            if e["kind"] == "path":
                self.paths[e["robot"]].append((e["t"], e["points"]))
            if e["kind"] in ("leg", "replan_request", "replan", "pick_failed", "released", "in_basket", "paid",
                             "unload_grab", "on_counter", "grabbed"):  # fmt: skip
                self.legs.setdefault(e.get("robot", ""), []).append((e["t"], e))
        self.cams = {p.name for p in (run / "frames").iterdir()} if (run / "frames").exists() else set()
        self.td = Image.open(run / "topdown.jpg").convert("RGB")
        self.tdm = self.plan["topdown"]

    def frame_path(self, cam, f):
        return self.dir / "frames" / cam / f"{f:06d}.jpg"

    def has(self, cam, f):
        return self.frame_path(cam, f).exists()

    def last_event(self, name, t, kinds, within=None):
        evs = self.legs.get(name, [])
        i = bisect.bisect_right([x[0] for x in evs], t) - 1
        while i >= 0:
            et, e = evs[i]
            if within is not None and t - et > within:
                return None
            if e["kind"] in kinds:
                return e
            i -= 1
        return None

    def path_at(self, name, t):
        ps = self.paths[name]
        i = bisect.bisect_right([p[0] for p in ps], t) - 1
        return ps[i][1] if i >= 0 else None


# --- mini-map ------------------------------------------------------------------------------------------------------
class MiniMap:
    def __init__(self, run: Run, size: int):
        self.r, self.size = run, size
        m = run.plan["map"]
        lo, hi = np.array(m["lo"]) - 0.3, np.array(m["hi"]) + 0.3
        span = float(max(hi - lo))
        self.lo, self.span = lo, span
        tdm = run.tdm
        # crop the square top-down render to the store box
        x0, y1, px = tdm["x0"], tdm["y1"], tdm["px"]
        c0 = int((lo[0] - x0) / px)
        r0 = int((y1 - (lo[1] + span)) / px)
        n = int(span / px)
        base = run.td.crop((c0, r0, c0 + n, r0 + n)).resize((size, size), Image.LANCZOS)
        base = Image.blend(base, Image.new("RGB", base.size, (25, 28, 35)), 0.25)
        self.base = base.convert("RGBA")
        d = ImageDraw.Draw(self.base)
        ck = run.plan["checkout"]
        d.rectangle([self.p(ck["lo"][0], ck["hi"][1]), self.p(ck["hi"][0], ck["lo"][1])], outline=GREEN, width=3)
        cx, cy = self.p((ck["lo"][0] + ck["hi"][0]) / 2, (ck["lo"][1] + ck["hi"][1]) / 2)
        d.text((cx, cy), "CHECKOUT", font=font(max(10, size // 40), True), fill=GREEN, anchor="mm")
        for s in run.plan["sections"]:
            sx, sy = self.p((s["lo"][0] + s["hi"][0]) / 2, (s["lo"][1] + s["hi"][1]) / 2)
            d.text((sx, sy), s["label"], font=font(max(10, size // 42), True), fill=(255, 255, 255), anchor="mm",
                   stroke_width=2, stroke_fill=(0, 0, 0))  # fmt: skip

    def p(self, x, y):
        return ((x - self.lo[0]) / self.span * self.size, (1 - (y - self.lo[1]) / self.span) * self.size)

    def render(self, fr, t, highlight=None, show_plan=True):
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        s = self.size / 520
        for i, rv in enumerate(fr["rovers"]):
            col = COLORS[i % 3]
            name = rv["name"]
            # planned remaining stops (dashed polyline from the rover)
            pts = [(rv["x"], rv["y"])]
            for iid in rv["queue"]:
                it = self.r.items.get(iid.split("-alt")[0]) if iid not in self.r.items else self.r.items[iid]
                if it:
                    pts.append(tuple(it["center"][:2]))
            slot = self.r.plan["slots"].get(name)
            if slot and rv["phase"] not in ("checkout", "paid"):
                pts.append((slot[0], slot[1]))
            if show_plan and len(pts) > 1:
                pp = [self.p(*q) for q in pts]
                for a, b in zip(pp, pp[1:], strict=False):
                    _dash(d, a, b, col + (200,), max(2, int(3 * s)))
            # current A* path
            path = self.r.path_at(name, t)
            if path and rv["phase"] in ("drive", "to_checkout"):
                d.line([self.p(*q) for q in path], fill=col + (255,), width=max(3, int(5 * s)))
            # stops: done (green tick) / remaining (numbered)
            for k, iid in enumerate(rv["queue"]):
                it = self.r.items.get(iid)
                if it:
                    x, y = self.p(*it["center"][:2])
                    rr = 11 * s
                    d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col + (255,), outline=(0, 0, 0), width=2)
                    d.text((x, y), str(k + 1), font=font(int(13 * s), True), fill=(0, 0, 0), anchor="mm")
            for iid in rv["done"]:
                it = self.r.items.get(iid)
                if it:
                    x, y = self.p(*it["center"][:2])
                    rr = 10 * s
                    d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=GREEN + (255,), outline=(0, 0, 0), width=2)
                    d.text((x, y), "✓", font=font(int(14 * s), True), fill=(255, 255, 255), anchor="mm")
        for i, rv in enumerate(fr["rovers"]):  # rovers on top
            col = COLORS[i % 3]
            x, y = self.p(rv["x"], rv["y"])
            a = rv["yaw"]
            L = 16 * s
            tri = [
                (x + L * math.cos(a), y - L * math.sin(a)),
                (x + L * 0.7 * math.cos(a + 2.5), y - L * 0.7 * math.sin(a + 2.5)),
                (x + L * 0.7 * math.cos(a - 2.5), y - L * 0.7 * math.sin(a - 2.5)),
            ]
            d.polygon(tri, fill=col + (255,), outline=(255, 255, 255))
            d.text((x, y - 24 * s), rv["name"], font=font(int(15 * s), True), fill=col, anchor="mm",
                   stroke_width=2, stroke_fill=(0, 0, 0))  # fmt: skip
        return img


def _dash(d, a, b, fill, width, on=10, off=7):
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    if L < 1:
        return
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    s = 0.0
    while s < L:
        e = min(s + on, L)
        d.line([(a[0] + ux * s, a[1] + uy * s), (a[0] + ux * e, a[1] + uy * e)], fill=fill, width=width)
        s += on + off


# --- shot selection -----------------------------------------------------------------------------------------------
PICKING = ("reach", "lift", "basket", "pick", "home", "unload", "checkout", "replan")


def plan_shots(run: Run, fr0: int, fr1: int, base_speed=2.0):
    """[(frame, camera, pip_camera|None)] for frames fr0..fr1 of the main phase: follow whichever rover is picking,
    otherwise rotate chase / side / overview / second rover; driving plays at base_speed."""
    out = []
    names = run.names
    cur, held_until = "over", -1
    rotation = ["chase", "over", "side"] + (["chase2"] if len(names) > 1 else [])
    rot_i = 0
    f = float(fr0)
    focus = None
    while f <= fr1:
        fi = int(f)
        fr = run.frames[fi]
        busy = [i for i, rv in enumerate(fr["rovers"]) if rv["phase"] in PICKING]
        if busy:
            if focus not in busy:
                focus = busy[0]
                held_until = fi + 75
                pick_n = sum(len(r["done"]) for r in fr["rovers"])
                sfx = "" if focus == 0 else str(focus + 1)
                cur = "side" if (focus == 0 and pick_n % 3 == 1) else "chase" + sfx
            sfx = "" if focus == 0 else str(focus + 1)
            pip = "wrist" + sfx if fr["rovers"][focus]["phase"] in ("reach", "lift", "pick", "unload") else None
            speed = 1.0 if fr["rovers"][focus]["phase"] != "checkout" else 1.5
        else:
            focus = None
            if fi >= held_until:
                cur = rotation[rot_i % len(rotation)]
                rot_i += 1
                held_until = fi + int(4.5 * FPS * base_speed)
            pip, speed = None, base_speed
        cam = cur if run.has(cur, fi) else next((c for c in ("chase", "over", "side") if run.has(c, fi)), None)
        if cam:
            out.append((fi, cam, pip if pip and run.has(pip, fi) else None))
        f += speed
    return out


# --- compositing --------------------------------------------------------------------------------------------------
class Editor:
    def __init__(self, run: Run):
        self.r = run
        self.mm = MiniMap(run, 440)
        self.mm_big = MiniMap(run, 900)
        self.n_items = len(run.items)

    @lru_cache(maxsize=8)  # noqa: B019
    def load(self, cam, f):
        im = Image.open(self.r.frame_path(cam, f)).convert("RGB")
        if im.size != (W, H):
            im = im.resize((W, H), Image.LANCZOS)
        return im

    def compose(self, fi, cam, pip, caption=None, minimap=True, hud=True):
        r = self.r
        fr = r.frames[fi]
        t = fr["t"]
        img = self.load(cam, fi).convert("RGBA")
        if hud:
            self.hud(img, fr)
        if minimap:
            mm = self.mm.render(fr, t)
            x0, y0 = W - mm.size[0] - 30, 30
            panel(img, (x0 - 10, y0 - 10, x0 + mm.size[0] + 10, y0 + mm.size[1] + 40), 190)
            img.alpha_composite(mm, (x0, y0))
            ImageDraw.Draw(img).text((x0 + mm.size[0] / 2, y0 + mm.size[1] + 15), "live map · Gemini route",
                                     font=font(20), fill=(220, 220, 220), anchor="mm")  # fmt: skip
        if pip:
            p = Image.open(r.frame_path(pip, fi)).convert("RGB").resize((560, 315), Image.LANCZOS)
            x0, y0 = W - 560 - 30, H - 315 - 190
            panel(img, (x0 - 6, y0 - 34, x0 + 566, y0 + 321), 200, 10)
            img.paste(p, (x0, y0))
            ImageDraw.Draw(img).text((x0 + 8, y0 - 30), "gripper cam", font=font(22, True), fill=(255, 255, 255))
        cap = caption or self.caption(fr, t)
        if cap:
            self.caption_bar(img, *cap)
        return img.convert("RGB")

    def hud(self, img, fr):
        d = ImageDraw.Draw(img)
        h = 70 + 46 * len(fr["rovers"])
        panel(img, (24, 24, 520, 24 + h), 175)
        d.text((44, 36), "Picky", font=font(40, True), fill=(255, 255, 255))
        d.text((170, 50), "AI shopping rover", font=font(22), fill=(200, 200, 200))
        for i, rv in enumerate(fr["rovers"]):
            y = 96 + 46 * i
            col = COLORS[i % 3]
            d.ellipse([44, y + 6, 62, y + 24], fill=col)
            tot = max(self.r.total.get(rv["name"], 0), len(rv["done"]) + len(rv["queue"]))
            d.text((74, y), f"{rv['name']}", font=font(28, True), fill=col)
            d.text((180, y + 3), f"basket {len(rv['done'])}/{tot}", font=font(26), fill=(235, 235, 235))
            st = {"drive": "driving", "to_checkout": "to checkout", "paid": "paid ✓", "replan": "replanning",
                  "checkout": "checkout", "idle": "ready"}.get(rv["phase"], "picking")  # fmt: skip
            d.text((350, y + 3), st, font=font(26), fill=GREEN if st == "paid ✓" else (200, 200, 200))

    def caption(self, fr, t):
        """(title, line2, color) from the focused rover's state + recent events."""
        r = self.r
        best = None
        for i, rv in enumerate(fr["rovers"]):
            name, col = rv["name"], COLORS[i % 3]
            e = r.last_event(name, t, ("in_basket",), within=2.5)
            if e and e.get("inside"):
                n = len(rv["done"])
                c = (f"{name}: ✓ {r.items.get(_base_id(e, r), {}).get('name', e['item'])} is in the basket",
                     f"{n} of {r.total.get(name, n)} items", GREEN)  # fmt: skip
                best = best or c
                continue
            rq = r.last_event(name, t, ("replan", "replan_request", "pick_failed"), within=7.0)
            if rq and rv["phase"] in ("replan", "drive", "home"):
                if rq["kind"] == "replan":
                    c = (f"{name}: Gemini replanned", f"“{rq.get('summary', '')}”", (255, 210, 80))
                else:
                    c = (f"{name}: stop failed — asking Gemini to replan", rq.get("why") or rq.get("event", ""),
                         (255, 120, 90))  # fmt: skip
                return c
            if rv["phase"] in ("drive",) and rv["target"]:
                it = r.items.get(rv["target"], {})
                leg = r.last_event(name, t, ("leg",))
                reason = leg.get("reason", "") if leg else ""
                c = (f"{name} → {it.get('name', rv['target'])}  ·  {it.get('section_label', '')}",
                     f"Gemini: “{reason}”" if reason else "", col)  # fmt: skip
            elif rv["phase"] == "to_checkout":
                leg = r.last_event(name, t, ("leg",))
                reason = leg.get("reason", "") if leg else ""
                c = (f"{name} → checkout with {len(rv['done'])} items", f"Gemini: “{reason}”" if reason else "", col)
            elif rv["phase"] in ("reach", "pick", "lift", "basket", "home") and rv["target"]:
                it = r.items.get(rv["target"], {})
                c = (f"{name} picking {it.get('name', rv['target'])}", rv["sub"], col)
            elif rv["phase"] in ("checkout", "unload"):
                c = (f"{name} at checkout: unloading onto the counter", rv["sub"], col)
            elif rv["phase"] == "paid":
                c = (f"{name}: paid ✓", "", GREEN)
            else:
                continue
            if best is None or (rv["phase"] in PICKING and best[2] != GREEN):
                best = c
        return best

    def caption_bar(self, img, title, sub, color):
        d = ImageDraw.Draw(img)
        f1, f2 = font(44, True), font(32)
        lines = wrap_text(d, sub, f2, W - 260)[:2] if sub else []
        h = 80 + 44 * len(lines)
        panel(img, (100, H - h - 40, W - 100, H - 40), 195)
        d.rectangle([100, H - h - 40, 112, H - 40], fill=color)
        d.text((140, H - h - 22), title, font=f1, fill=(255, 255, 255))
        for k, ln in enumerate(lines):
            d.text((140, H - h + 40 + 44 * k), ln, font=f2, fill=(225, 225, 225))

    # --- cards ---
    def title_card(self, bg_cam, fi, n, sub=True):
        bg = self.load(bg_cam, fi).filter(ImageFilter.GaussianBlur(14))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (10, 12, 18)), 0.55).convert("RGBA")
        d = ImageDraw.Draw(bg)
        d.text((W / 2, H / 2 - 70), "Picky — AI shopping robot", font=font(96, True), fill=(255, 255, 255), anchor="mm")
        if sub:
            d.text((W / 2, H / 2 + 40), "Tell it what you want. Gemini maps the store, the rovers shop.",
                   font=font(40), fill=(220, 220, 220), anchor="mm")  # fmt: skip
            d.text((W / 2, H / 2 + 110),
                   "Isaac Sim · AgiBot Genie Sim supermarket (~1,100 products) · DISCOVERSE rm2_car + AIRBOT Play arm",
                   font=font(28), fill=(170, 175, 185), anchor="mm")  # fmt: skip
        frame = bg.convert("RGB")
        return [frame] * n

    def plan_card(self, fi, n):
        r = self.r
        fr = r.frames[fi]
        bg = Image.new("RGBA", (W, H), (14, 16, 22, 255))
        mm = self.mm_big.render(fr, fr["t"])
        bg.alpha_composite(mm, (60, (H - mm.size[1]) // 2))
        d = ImageDraw.Draw(bg)
        x = 60 + mm.size[0] + 60
        d.text((x, 70), "Gemini plans the trip", font=font(56, True), fill=(255, 255, 255))
        model = r.plan.get("model") or r.plan.get("source")
        d.text((x, 140), f"{model} · {r.plan.get('latency_s', '?')} s · {len(r.items)} items · map built from the scene",  # noqa: E501
               font=font(24), fill=(170, 175, 185))  # fmt: skip
        y = 190
        for ln in wrap_text(d, f"“{r.plan.get('summary', '')}”", font(30), W - x - 60)[:4]:
            d.text((x, y), ln, font=font(30), fill=(235, 225, 160))
            y += 40
        y += 16
        for i, name in enumerate(r.names):
            col = COLORS[i % 3]
            stops = r.plan["robots"].get(name, [])
            d.text((x, y), f"{name} — {len(stops)} stops", font=font(32, True), fill=col)
            y += 46
            for k, st in enumerate(stops):
                it = r.items.get(st["item"], {})
                d.text((x + 10, y), f"{k + 1}. {it.get('name', st['item'])}", font=font(25, True), fill=(240, 240, 240))
                sec = it.get("section_label", "")
                d.text((x + 330, y + 2), sec, font=font(22), fill=(170, 175, 185))
                y += 33
                if y > H - 60:
                    break
            y += 10
        frame = bg.convert("RGB")
        return [frame] * n

    def paid_card(self, cam, fi, n):
        r = self.r
        fr = r.frames[fi]
        bg = self.load(cam, fi).filter(ImageFilter.GaussianBlur(10))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (10, 12, 18)), 0.5).convert("RGBA")
        d = ImageDraw.Draw(bg)
        panel(bg, (W / 2 - 520, 110, W / 2 + 520, H - 110), 220, 30)
        d.text((W / 2, 200), "Paid ✓", font=font(110, True), fill=GREEN, anchor="mm")
        d.text((W / 2, 290), "Checkout complete · everything unloaded at the counter (simulated payment)", font=font(30),  # noqa: E501
               fill=(210, 210, 210), anchor="mm")  # fmt: skip
        y = 350
        for i, rv in enumerate(fr["rovers"]):
            names = [r.items.get(iid, {}).get("name", iid) for iid in rv["done"]]
            d.text((W / 2 - 470, y), f"{rv['name']}: {len(names)} items", font=font(32, True), fill=COLORS[i % 3])
            y += 44
            for ln in wrap_text(d, " · ".join(names), font(26), 940)[:4]:
                d.text((W / 2 - 470, y), ln, font=font(26), fill=(235, 235, 235))
                y += 34
            y += 14
        return [bg.convert("RGB")] * n

    def end_card(self, cam, fi, n):
        img = self.load(cam, fi).convert("RGBA")
        panel(img, (W / 2 - 700, H - 300, W / 2 + 700, H - 60), 205, 26)
        d = ImageDraw.Draw(img)
        d.text((W / 2, H - 240), "Picky", font=font(72, True), fill=(255, 255, 255), anchor="mm")
        d.text((W / 2, H - 165), "DubHacks 2026 · Visa “Reimagine Shopping”", font=font(34), fill=(220, 220, 220),
               anchor="mm")  # fmt: skip
        d.text((W / 2, H - 110),
               "Store: AgiBot Genie Sim (CC BY-NC-SA 4.0) · Robot: DISCOVERSE (MIT) · Route planning: Gemini",
               font=font(24), fill=(160, 165, 175), anchor="mm")  # fmt: skip
        return img.convert("RGB")


def _base_id(e, r):
    for iid, it in r.items.items():
        if it.get("product") == e.get("item"):
            return iid
    return e.get("item")


# --- assembly -----------------------------------------------------------------------------------------------------
def phase_range(run: Run, phase: str):
    idx = [i for i, f in enumerate(run.frames) if f["phase"] == phase]
    return (idx[0], idx[-1]) if idx else (None, None)


def write(path: Path, frames_iter, gif: Path | None = None, gif_range=None):
    import imageio_ffmpeg

    w = imageio_ffmpeg.write_frames(str(path), (W, H), fps=FPS, codec="libx264", quality=None,
                                    output_params=["-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p"],
                                    macro_block_size=8)  # fmt: skip
    w.send(None)
    n = 0
    gif_frames = []
    for im in frames_iter:
        w.send(np.ascontiguousarray(np.asarray(im, dtype=np.uint8)))
        if gif and gif_range and gif_range[0] <= n < gif_range[1] and n % 3 == 0:
            gif_frames.append(im.resize((640, 360), Image.LANCZOS))
        n += 1
    w.close()
    if gif and gif_frames:
        gif_frames[0].save(gif, save_all=True, append_images=gif_frames[1:], duration=100, loop=0, optimize=True)
    print(f"wrote {path} ({n} frames, {n / FPS:.1f} s)")
    return n


def full_cut(ed: Editor):
    r = ed.r
    i0, i1 = phase_range(r, "intro")
    m0, m1 = phase_range(r, "main")
    o0, o1 = phase_range(r, "outro")
    first_main = m0
    yield from ed.title_card("over" if r.has("over", i0) else "orbit", i0, int(3.5 * FPS))
    for fi in range(i0, i1 + 1):  # intro orbit, shopping list
        names = ", ".join(it["name"] for it in list(r.items.values())[:8])
        yield ed.compose(fi, "orbit", None, (f"Shopping list: {len(r.items)} items", names + ("…" if len(r.items) > 8 else ""),  # noqa: E501
                                             (255, 255, 255)), minimap=False)  # fmt: skip
    yield from ed.plan_card(first_main, int(7 * FPS))
    shots = plan_shots(r, m0, m1)
    for fi, cam, pip in shots:
        yield ed.compose(fi, cam, pip)
    last = m1
    yield from ed.paid_card("chase" if r.has("chase", last) else "over", last, int(4 * FPS))
    if o0 is not None:
        for fi in range(o0, o1 + 1):
            yield (
                ed.end_card("orbit", fi)
                if fi > o0 + FPS
                else ed.compose(fi, "orbit", None, ("", "", GREEN), minimap=False)
            )


def highlight(ed: Editor, target_s=55):
    r = ed.r
    i0, i1 = phase_range(r, "intro")
    m0, m1 = phase_range(r, "main")
    o0, o1 = phase_range(r, "outro")
    yield from ed.title_card("over" if r.has("over", i0) else "orbit", i0, int(2.5 * FPS))
    for fi in range(i0, i1 + 1, 3):
        yield ed.compose(fi, "orbit", None, (f"Shopping list: {len(r.items)} items", "Gemini maps the store and plans the route",  # noqa: E501
                                             (255, 255, 255)), minimap=False)  # fmt: skip
    yield from ed.plan_card(m0, int(4 * FPS))
    # 3 picks from different sections at 1.5x, everything else at 5x
    picks = [e for e in r.events if e["kind"] == "grabbed"]
    chosen, secs = [], set()
    for e in picks:
        it = next((v for v in r.items.values() if v.get("product") == e["item"]), None)
        sec = it.get("section") if it else None
        if sec not in secs:
            secs.add(sec)
            chosen.append(e["t"])
        if len(chosen) == 3:
            break
    windows = [(t - 9.0, t + 6.5) for t in chosen]
    shots = plan_shots(r, m0, m1, base_speed=1.0)
    for fi, cam, pip in shots:
        t = r.frames[fi]["t"]
        inside = any(a <= t <= b for a, b in windows)
        keep_every = 1 if inside else 6
        if inside and fi % 3 == 0:  # 1.5x
            continue
        if not inside and fi % keep_every:
            continue
        yield ed.compose(fi, cam, pip)
    yield from ed.paid_card("chase" if r.has("chase", m1) else "over", m1, int(3 * FPS))
    if o0 is not None:
        for fi in range(o0, o1 + 1, 2):
            yield ed.end_card("orbit", fi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--only", choices=["full", "highlight"], default=None)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    ed = Editor(Run(a.run))
    if a.only in (None, "highlight"):
        write(a.out / f"picky_demo_highlight{a.tag}.mp4", highlight(ed), gif=a.out / f"picky_demo_preview{a.tag}.gif",
              gif_range=(0, 15 * FPS))  # fmt: skip
    if a.only in (None, "full"):
        write(a.out / f"picky_demo_full{a.tag}.mp4", full_cut(ed))


if __name__ == "__main__":
    main()
