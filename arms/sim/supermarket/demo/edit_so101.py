"""Cut a `teleop_app.py --demo --record` run of the SO-101 rover (demo/so101_demo.py) into the demo videos.

Reads RUN/frames/<director|birdseye|side|wrist|front|basket>/<n>.jpg (30 fps of sim time; any camera folder may be
missing), RUN/frames.jsonl, RUN/events.jsonl,
RUN/plan.json and RUN/topdown.jpg. Composes: title card, intro + shopping list, the Gemini plan card, the main run
(director camera + HUD + live mini-map with Gemini's route, visited / remaining stops, the trail and the robot +
wrist-cam and front/basket/side/chase-cam picture-in-picture + captions with Gemini's reason per leg and each pick),
the "Paid" card and the outro. Camera views: bird's-eye full screen in the middle of long drives (chase cam as PiP),
side cam PiP during reach/grasp and the checkout unload, side cam full screen while the arm swings the product into
the basket (basket cam as PiP); every view is labelled. Playback speed varies by phase (driving faster than
grasping). Works on a run still in progress
(--early: cut what exists, end on a "to be continued" card).

    python demo/edit_so101.py RUN OUT_DIR [--only full|highlight|early] [--tag _v2]
    # -> OUT/picky_so101_supermarket_full.mp4, picky_so101_highlight.mp4, picky_so101_preview.gif
"""

from __future__ import annotations

import argparse
import bisect
import json
from functools import lru_cache
from pathlib import Path

import edit_video
import numpy as np
from edit_video import GREEN, MiniMap, font, panel, wrap_text
from PIL import Image, ImageDraw, ImageFilter

# a font with ✓ / → on macOS too (DejaVu on Linux has them)
for _p in ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"):
    if _p not in edit_video.FONT_PATHS:
        edit_video.FONT_PATHS.insert(2, _p)
W, H, FPS = 1920, 1080, 30
HL_PICKS = 4  # picks shown at near real time in the highlight
ORANGE = (255, 150, 40)
BLUE = (80, 170, 255)
YELLOW = (255, 214, 90)
WHITE = (255, 255, 255)
GREY = (200, 204, 212)
PICK_PHASES = ("reach", "grasp", "lift", "basket")
BADGE = [""]  # e.g. "offline preview" for runs without Isaac


class Run:
    def __init__(self, run: Path):
        self.dir = run
        self.frames = [json.loads(ln) for ln in (run / "frames.jsonl").read_text().splitlines() if ln.strip()]
        self.events = [json.loads(ln) for ln in (run / "events.jsonl").read_text().splitlines() if ln.strip()]
        self.plan = json.loads((run / "plan.json").read_text())
        self.items = self.plan["items"]
        self.names = [r["name"] for r in self.frames[0]["rovers"]]
        self.name = self.names[0]
        self.total = {n: len(self.plan["robots"].get(n, [])) for n in self.names}
        self.paths = {n: [] for n in self.names}
        self.legs = {n: [] for n in self.names}
        self.ets = [e["t"] for e in self.events]
        for e in self.events:
            if e["kind"] == "path":
                self.paths[e["robot"]].append((e["t"], e["points"]))
            self.legs.setdefault(e.get("robot", ""), []).append((e["t"], e))
        self.cams = {p.name for p in (run / "frames").iterdir()} if (run / "frames").exists() else set()
        self.td = Image.open(run / "topdown.jpg").convert("RGB")
        self.tdm = self.plan["topdown"]
        # wrist camera upside down for flipped-roll grasps
        self.flip_from = []
        for e in self.events:
            if e["kind"] == "ik_grasp":
                self.flip_from.append((e["t"], bool(e.get("wrist_flip"))))
        self.trail = [(f["t"], f["rovers"][0]["x"], f["rovers"][0]["y"]) for f in self.frames]
        dts = np.diff([f["t"] for f in self.frames[: min(len(self.frames), 400)]])
        # sim frames per 1/30 s (1 for a 30 fps recording, 2 for --capture-every 4) to keep playback speeds in sim time
        self.ratio = max(1.0, float(np.median(dts)) * FPS) if len(dts) else 1.0
        self.drives = []  # (t0, t1) of each contiguous driving stretch
        for f in self.frames:
            drv = f["rovers"][0]["phase"] in ("drive", "to_checkout")
            if drv and self.drives and self.drives[-1][2]:
                self.drives[-1][1] = f["t"]
            elif drv:
                self.drives.append([f["t"], f["t"], True])
            elif self.drives:
                self.drives[-1][2] = False

    def frame_path(self, cam, f):
        return self.dir / "frames" / cam / f"{f:06d}.jpg"

    def has(self, cam, f):
        return self.frame_path(cam, f).exists()

    def last_event(self, t, kinds, within=None):
        i = bisect.bisect_right(self.ets, t) - 1
        while i >= 0:
            e = self.events[i]
            if within is not None and t - e["t"] > within:
                return None
            if e["kind"] in kinds:
                return e
            i -= 1
        return None

    def path_at(self, name, t):
        ps = self.paths[name]
        i = bisect.bisect_right([p[0] for p in ps], t) - 1
        return ps[i][1] if i >= 0 else None

    def in_long_drive(self, t, min_len=4.0, pad0=1.2, pad1=1.0):
        return any(t1 - t0 >= min_len and t0 + pad0 <= t <= t1 - pad1 for t0, t1, _ in self.drives)

    def wrist_flipped(self, t):
        cur = False
        for tt, fl in self.flip_from:
            if tt <= t:
                cur = fl
        return cur


class Map(MiniMap):
    """edit_video's mini-map + the robot's trail (where it has been)"""

    def render(self, fr, t, highlight=None, show_plan=True):
        img = super().render(fr, t, highlight, show_plan)
        d = ImageDraw.Draw(img)
        k = bisect.bisect_right([p[0] for p in self.r.trail], t)
        pts = [self.p(x, y) for _, x, y in self.r.trail[:k:3]]
        if len(pts) > 1:
            d.line(pts, fill=(255, 255, 255, 150), width=max(2, self.size // 220))
        return img


class Editor:
    def __init__(self, run: Run):
        self.r = run
        self.mm = Map(run, 380)
        self.mm_big = Map(run, 900)

    @lru_cache(maxsize=16)  # noqa: B019
    def load(self, cam, f, size=(W, H)):
        p = self.r.frame_path(cam, f)
        if not p.exists():
            return None
        im = Image.open(p).convert("RGB")
        if im.size != size:
            im = im.resize(size, Image.LANCZOS)
        return im

    def nearest(self, cam, f, back=6):
        for k in range(back):
            if self.r.has(cam, f - k):
                return f - k
        return None

    # ---- overlays
    def hud(self, img, fr):
        d = ImageDraw.Draw(img)
        rv = fr["rovers"][0]
        panel(img, (24, 24, 560, 142), 178)
        d.text((44, 32), "Picky", font=font(44, True), fill=WHITE)
        d.text((180, 46), "SO-101 shopping rover", font=font(24), fill=GREY)
        tot = max(self.r.total.get(rv["name"], 0), len(rv["done"]) + len(rv["queue"]))
        st = {
            "drive": "driving",
            "to_checkout": "to checkout",
            "paid": "paid ✓",
            "replan": "replanning",
            "checkout": "checkout",
            "unload": "checkout",
            "idle": "ready",
        }.get(rv["phase"], "picking")
        d.text((44, 92), f"basket {rv['cargo']}/{tot}", font=font(28, True), fill=ORANGE)
        if rv.get("counter"):
            d.text((250, 92), f"counter {rv['counter']}", font=font(28, True), fill=GREEN)
        d.text((430, 92), st, font=font(28), fill=GREEN if st == "paid ✓" else GREY)

    def pip(self, img, cam, fi, box, label, flip=False):
        x0, y0, w, h = box
        f = self.nearest(cam, fi)
        if f is None:
            return
        im = Image.open(self.r.frame_path(cam, f)).convert("RGB").resize((w, h), Image.LANCZOS)
        if flip:
            im = im.rotate(180)
        panel(img, (x0 - 6, y0 - 36, x0 + w + 6, y0 + h + 6), 205, 10)
        img.paste(im, (x0, y0))
        ImageDraw.Draw(img).text((x0 + 6, y0 - 33), label, font=font(22, True), fill=WHITE)

    def view_label(self, img, label, y=160):
        d = ImageDraw.Draw(img)
        tw = d.textlength(label, font=font(24, True))
        panel(img, (24, y, 24 + tw + 64, y + 44), 190, 10)
        d.ellipse([40, y + 15, 54, y + 29], fill=(255, 70, 70))
        d.text((64, y + 22), label, font=font(24, True), fill=WHITE, anchor="lm")

    def caption_bar(self, img, title, sub, color):
        d = ImageDraw.Draw(img)
        f1, f2 = font(40, True), font(30)
        right = 1340
        lines = wrap_text(d, sub, f2, right - 180)[:2] if sub else []
        h = 72 + 40 * len(lines)
        panel(img, (40, H - h - 36, right, H - 36), 200)
        d.rectangle([40, H - h - 36, 52, H - 36], fill=color)
        d.text((76, H - h - 22), title, font=f1, fill=WHITE)
        for k, ln in enumerate(lines):
            d.text((76, H - h + 34 + 40 * k), ln, font=f2, fill=(228, 228, 228))

    def caption(self, fr, t):
        r = self.r
        rv = fr["rovers"][0]
        name = rv["name"]
        it = r.items.get(rv.get("target") or "", {})
        tot = r.total.get(name, len(r.items))
        e = r.last_event(t, ("in_basket",), within=2.6)
        if e:
            if e.get("inside"):
                return (
                    f"✓ {e.get('name', e['item'])} is in the basket",
                    f"{e.get('n', '')} of {tot} · dropped by gravity, rides on the basket floor",
                    GREEN,
                )
            return (f"{e.get('name', e['item'])} missed the basket", "", (255, 120, 90))
        rq = r.last_event(t, ("replan", "replan_request", "pick_failed", "replan_failed"), within=6.0)
        if rq and rv["phase"] in ("replan", "drive", "idle"):
            if rq["kind"] == "replan":
                return ("Gemini replanned", f"“{rq.get('summary', '')}”", YELLOW)
            return ("Stop failed: asking Gemini to replan", rq.get("why") or rq.get("event", ""), (255, 120, 90))
        if rv["phase"] == "drive" and rv["target"]:
            leg = r.last_event(t, ("leg",))
            reason = leg.get("reason", "") if leg else ""
            return (
                f"{name} → {it.get('name', rv['target'])}  ·  {it.get('section_label', '')}",
                f"Gemini: “{reason}”" if reason else "",
                BLUE,
            )
        if rv["phase"] in PICK_PHASES and rv["target"]:
            g = r.last_event(t, ("grasp_check", "attached"), within=40)
            nm = it.get("name", rv["target"])
            if rv["phase"] in ("lift", "basket") or (g and g["kind"] == "attached" and g.get("item") == rv["target"]):
                sub = {"lift": "lift off the shelf, retract", "basket": "tool down, swing 90° to the rear basket"}.get(
                    rv["phase"], ""
                )
                return (f"SO-101 grasp ✓ {nm} → basket", sub, GREEN)
            if rv["phase"] == "grasp":
                return (
                    f"SO-101 closing the jaws on {nm}",
                    "attach only after the moving jaw touches it, with it between the jaws",
                    ORANGE,
                )
            return (
                f"SO-101 reaching for {nm}",
                f"IK pre-grasp → straight slide in · {it.get('width_cm', '?')} cm across the jaws",
                ORANGE,
            )
        if rv["phase"] == "to_checkout":
            return (
                f"{name} → checkout with {rv['cargo']} items",
                "Gemini: “" + (r.plan.get("checkout_reason", {}).get(name) or "every item is in the basket") + "”",
                BLUE,
            )
        if rv["phase"] in ("checkout", "unload"):
            return (
                "Checkout: the SO-101 unloads the basket onto the counter",
                f"{rv.get('counter', 0)} of {tot} on the counter · {rv['sub']}",
                ORANGE,
            )
        if rv["phase"] == "paid":
            return ("Paid ✓", f"{rv.get('counter', 0)} items on the counter (simulated payment)", GREEN)
        return None

    def compose(self, fi, caption=None, overlays=True):
        r = self.r
        fr = r.frames[fi]
        t = fr["t"]
        rv = fr["rovers"][0]
        main_cam = "director"
        if overlays and fr["phase"] == "main":
            if rv["phase"] in ("drive", "to_checkout") and r.in_long_drive(t):
                main_cam = "birdseye"
            elif rv["phase"] == "basket":
                main_cam = "side"
        base = None
        if main_cam != "director":
            f = self.nearest(main_cam, fi)
            base = self.load(main_cam, f) if f is not None else None
            if base is None:
                main_cam = "director"
        if base is None:
            base = self.load("director", fi)
        if base is None:
            f = self.nearest("director", fi, 10)
            base = self.load("director", f) if f is not None else Image.new("RGB", (W, H), (20, 20, 24))
        img = base.convert("RGBA")
        if overlays:
            self.hud(img, fr)
            mm = self.mm.render(fr, t)
            x0, y0 = W - mm.size[0] - 30, 26
            panel(img, (x0 - 8, y0 - 8, x0 + mm.size[0] + 8, y0 + mm.size[1] + 34), 196)
            img.alpha_composite(mm, (x0, y0))
            ImageDraw.Draw(img).text(
                (x0 + mm.size[0] / 2, y0 + mm.size[1] + 14),
                "live map · Gemini route",
                font=font(19),
                fill=(220, 220, 220),
                anchor="mm",
            )
            mode = fr.get("mode", "chase")
            label = {
                "birdseye": "bird's-eye",
                "side": "side cam",
            }.get(main_cam, {"hold": "close-up cam", "orbit": "orbit cam"}.get(mode, "chase cam"))
            self.view_label(img, label)
            slot = (W - 384 - 30, 500, 384, 216)
            basket_view = rv["phase"] == "basket" or r.last_event(t, ("released", "in_basket"), within=1.6)
            if basket_view and r.has("basket", fi):
                self.pip(img, "basket", fi, slot, "basket cam")
            elif main_cam == "birdseye":
                self.pip(img, "director", fi, slot, "chase cam")
            elif rv["phase"] in PICK_PHASES + ("checkout", "unload") and self.nearest("side", fi) is not None:
                self.pip(img, "side", fi, slot, "side cam")
            elif main_cam == "director" and rv["phase"] in ("drive", "to_checkout") and self.nearest("birdseye", fi):
                self.pip(img, "birdseye", fi, slot, "bird's-eye")
            else:
                self.pip(img, "front", fi, slot, "front cam")
            self.pip(
                img,
                "wrist",
                fi,
                (W - 512 - 30, H - 288 - 30, 512, 288),
                "SO-101 wrist cam",
                flip=r.wrist_flipped(t) and rv["phase"] in PICK_PHASES,
            )
        cap = caption if caption is not None else self.caption(fr, t)
        if cap and cap[0]:
            self.caption_bar(img, *cap)
        if BADGE[0]:
            d = ImageDraw.Draw(img)
            tw = d.textlength(BADGE[0], font=font(22, True))
            panel(img, (W / 2 - tw / 2 - 16, 20, W / 2 + tw / 2 + 16, 58), 200, 10)
            d.text((W / 2, 39), BADGE[0], font=font(22, True), fill=YELLOW, anchor="mm")
        return img.convert("RGB")

    # ---- cards
    def title_card(self, fi, n, sub=True):
        bg = self.load("director", fi) or Image.new("RGB", (W, H), (20, 20, 24))
        bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(14)), Image.new("RGB", (W, H), (10, 12, 18)), 0.55)
        d = ImageDraw.Draw(bg)
        d.text((W / 2, H / 2 - 90), "Picky — AI shopping robot", font=font(92, True), fill=WHITE, anchor="mm")
        if sub:
            d.text(
                (W / 2, H / 2 + 20),
                "Gemini maps the store and plans the route · an SO-101 arm does the shopping",
                font=font(38),
                fill=(222, 222, 222),
                anchor="mm",
            )
            d.text(
                (W / 2, H / 2 + 90),
                "Isaac Sim · AgiBot Genie Sim supermarket · PAL PMB2 round base + long-reach Hiwonder SO-101 + "
                "round wire basket",
                font=font(26),
                fill=(170, 175, 185),
                anchor="mm",
            )
        return [bg] * n

    def plan_card(self, fi, n):
        r = self.r
        fr = r.frames[fi]
        bg = Image.new("RGBA", (W, H), (14, 16, 22, 255))
        mm = self.mm_big.render(fr, fr["t"])
        bg.alpha_composite(mm, (50, (H - mm.size[1]) // 2))
        d = ImageDraw.Draw(bg)
        x = 50 + mm.size[0] + 110
        d.text((x, 60), "Gemini plans the trip", font=font(54, True), fill=WHITE)
        model = r.plan.get("model") or r.plan.get("source")
        d.text(
            (x, 128),
            f"{model} · {r.plan.get('latency_s', '?')} s · {len(r.items)} items · map built from the scene",
            font=font(23),
            fill=(170, 175, 185),
        )
        y = 172
        for ln in wrap_text(d, f"“{r.plan.get('summary', '')}”", font(28), W - x - 50)[:4]:
            d.text((x, y), ln, font=font(28), fill=(235, 225, 160))
            y += 37
        y += 12
        stops = r.plan["robots"].get(r.name, [])
        step = min(58, max(40, (H - 40 - y) // max(len(stops), 1)))
        for k, st in enumerate(stops):
            it = r.items.get(st["item"], {})
            th = r.dir / "items" / f"{st['item'].split('-alt')[0]}.jpg"
            if th.exists():
                tim = Image.open(th).convert("RGB")
                c = tim.size[0] // 4
                tim = tim.crop((c, c, tim.size[0] - c, tim.size[1] - c)).resize((step - 8, step - 8), Image.LANCZOS)
                bg.paste(tim, (x - step, y - 2))
            d.text((x, y), f"{k + 1}. {it.get('name', st['item'])}", font=font(24, True), fill=(240, 240, 240))
            d.text((x + 330, y + 2), it.get("section_label", ""), font=font(21), fill=(170, 175, 185))
            if step >= 50:
                ln = wrap_text(d, st.get("reason", ""), font(19), W - x - 90)
                if ln:
                    d.text((x + 34, y + 28), ln[0] + ("…" if len(ln) > 1 else ""), font=font(19), fill=(150, 160, 175))
            y += step
            if y > H - 40:
                break
        return [bg.convert("RGB")] * n

    def paid_card(self, fi, n):
        r = self.r
        fr = r.frames[fi]
        bg = self.load("director", fi) or Image.new("RGB", (W, H), (20, 20, 24))
        bg = Image.blend(bg.filter(ImageFilter.GaussianBlur(10)), Image.new("RGB", (W, H), (10, 12, 18)), 0.5)
        bg = bg.convert("RGBA")
        d = ImageDraw.Draw(bg)
        panel(bg, (W / 2 - 560, 120, W / 2 + 560, H - 120), 222, 30)
        d.text((W / 2, 210), "Paid ✓", font=font(108, True), fill=GREEN, anchor="mm")
        d.text(
            (W / 2, 300),
            "every item taken out of the basket by the arm and laid on the counter (simulated payment)",
            font=font(27),
            fill=(210, 210, 210),
            anchor="mm",
        )
        rv = fr["rovers"][0]
        names = [r.items.get(i, {}).get("name", i) for i in rv["done"]]
        y = 370
        d.text(
            (W / 2 - 500, y),
            f"{len(names)} items · {rv.get('counter', 0)} on the counter",
            font=font(32, True),
            fill=ORANGE,
        )
        y += 50
        for ln in wrap_text(d, " · ".join(names), font(27), 1000)[:6]:
            d.text((W / 2 - 500, y), ln, font=font(27), fill=(235, 235, 235))
            y += 38
        stats = r.last_event(1e9, ("done",))
        llm = (stats or {}).get("llm_calls")
        if llm is not None:
            d.text(
                (W / 2 - 500, H - 190),
                f"Gemini calls: {llm} · sim time {stats.get('sim_s', '?')} s · grasps "
                f"{stats.get('grasp_ok', '?')} ok / {stats.get('grasp_fail', '?')} retried",
                font=font(24),
                fill=(170, 175, 185),
            )
        return [bg.convert("RGB")] * n

    def end_card(self, fi):
        img = (self.load("director", fi) or Image.new("RGB", (W, H), (20, 20, 24))).convert("RGBA")
        panel(img, (W / 2 - 760, H - 300, W / 2 + 760, H - 60), 205, 26)
        d = ImageDraw.Draw(img)
        d.text((W / 2, H - 240), "Picky", font=font(70, True), fill=WHITE, anchor="mm")
        d.text(
            (W / 2, H - 168),
            "DubHacks 2026 · Visa “Reimagine Shopping”",
            font=font(32),
            fill=(220, 220, 220),
            anchor="mm",
        )
        d.text(
            (W / 2, H - 112),
            "Store: AgiBot Genie Sim (CC BY-NC-SA 4.0) · Arm: SO-ARM100/SO-101 (Apache-2.0) · "
            "Base: PAL PMB2 (Apache-2.0) · Route planning: Gemini",
            font=font(22),
            fill=(160, 165, 175),
            anchor="mm",
        )
        return img.convert("RGB")

    def todo_card(self, fi, n):
        img = (self.load("director", fi) or Image.new("RGB", (W, H), (20, 20, 24))).filter(ImageFilter.GaussianBlur(8))
        d = ImageDraw.Draw(img)
        d.text((W / 2, H / 2), "early cut · the run continues", font=font(64, True), fill=WHITE, anchor="mm")
        return [img] * n


# --- assembly -----------------------------------------------------------------------------------------------------
def phase_range(run: Run, phase: str):
    idx = [i for i, f in enumerate(run.frames) if f["phase"] == phase]
    return (idx[0], idx[-1]) if idx else (None, None)


def speed_for(run: Run, fi: int, picks_seen: int, mode: str = "full") -> float:
    rv = run.frames[fi]["rovers"][0]
    ph = rv["phase"]
    if mode == "highlight":
        return {
            "drive": 6.0,
            "to_checkout": 6.0,
            "reach": 1.5,
            "grasp": 1.25,
            "lift": 2.0,
            "basket": 2.5,
            "checkout": 4.0,
            "unload": 4.0,
            "replan": 1.0,
        }.get(ph, 4.0)
    early = picks_seen < 3
    return {
        "drive": 2.5,
        "to_checkout": 2.5,
        "replan": 1.0,
        "reach": 1.0 if early else 1.5,
        "grasp": 1.0 if early else 1.25,
        "lift": 1.25 if early else 2.0,
        "basket": 1.5 if early else 2.5,
        "checkout": 2.0 if rv.get("counter", 0) < 2 else 4.0,
        "unload": 2.0 if rv.get("counter", 0) < 2 else 4.0,
        "paid": 1.0,
    }.get(ph, 2.0)


def main_frames(run: Run, m0: int, m1: int, mode="full", windows=None):
    f = float(m0)
    while f <= m1:
        fi = int(f)
        fr = run.frames[fi]
        done = len(fr["rovers"][0]["done"])
        sp = speed_for(run, fi, done, mode) / run.ratio
        if windows is not None:
            t = fr["t"]
            if not any(a <= t <= b for a, b in windows):
                sp = max(sp, 30.0)
        yield fi
        f += sp


def write(path: Path, frames_iter, gif: Path | None = None, gif_range=None):
    import imageio_ffmpeg

    w = imageio_ffmpeg.write_frames(
        str(path),
        (W, H),
        fps=FPS,
        codec="libx264",
        quality=None,
        output_params=["-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        macro_block_size=8,
    )
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
    print(f"wrote {path} ({n} frames, {n / FPS:.1f} s)", flush=True)
    return n


def full_cut(ed: Editor, early=False):
    r = ed.r
    i0, i1 = phase_range(r, "intro")
    m0, m1 = phase_range(r, "main")
    o0, o1 = phase_range(r, "outro")
    first = i0 if i0 is not None else 0
    yield from ed.title_card(first, int(3.2 * FPS))
    if i0 is not None:
        names = ", ".join(it["name"] for it in list(r.items.values())[:7])
        for fi in range(i0, i1 + 1):
            yield ed.compose(
                fi,
                (f"Shopping list: {len(r.items)} items", names + ("…" if len(r.items) > 7 else ""), WHITE),
                overlays=False,
            )
    if m0 is None:
        return
    yield from ed.plan_card(m0, int(7 * FPS))
    for fi in main_frames(r, m0, m1):
        yield ed.compose(fi)
    if early:
        yield from ed.todo_card(m1, int(2 * FPS))
        return
    yield from ed.paid_card(m1, int(4 * FPS))
    if o0 is not None:
        for fi in range(o0, o1 + 1):
            yield ed.end_card(fi) if fi > o0 + FPS else ed.compose(fi, ("", "", GREEN), overlays=False)


def highlight(ed: Editor):
    r = ed.r
    i0, _ = phase_range(r, "intro")
    m0, m1 = phase_range(r, "main")
    o0, o1 = phase_range(r, "outro")
    yield from ed.title_card(i0 or 0, int(2.5 * FPS))
    yield from ed.plan_card(m0, int(3.5 * FPS))
    # four picks from different sections (slide-in -> in the basket), driving in between fast, the first and the
    # last unload at the checkout (60-90 s in all)
    att = [e for e in r.events if e["kind"] == "attached"]
    secs, wins = set(), []
    for e in att:
        sec = r.items.get(e["item"], {}).get("section")
        if sec in secs:
            continue
        secs.add(sec)
        t_in = next((x["t"] for x in r.events if x["kind"] == "in_basket" and x["item"] == e["item"]), e["t"] + 10)
        wins.append((e["t"] - 5.0, t_in + 0.8))
        if len(wins) == HL_PICKS:
            break
    uns = [e for e in r.events if e["kind"] == "unload_attached"]
    for un in uns[:1] + uns[-1:] if len(uns) > 1 else uns:
        wins.append((un["t"] - 4.0, un["t"] + 9.0))
    for fi in main_frames(r, m0, m1, "highlight", wins):
        yield ed.compose(fi)
    yield from ed.paid_card(m1, int(3 * FPS))
    if o0 is not None:
        for fi in range(o0, o1 + 1, 3):
            yield ed.end_card(fi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--only", choices=["full", "highlight", "early"], default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--stills", type=int, default=0, help="only write N composed stills (+ the cards) to OUT/stills")
    ap.add_argument("--badge", default="", help="small label at the top of every main frame")
    a = ap.parse_args()
    BADGE[0] = a.badge
    a.out.mkdir(parents=True, exist_ok=True)
    ed = Editor(Run(a.run))
    if a.stills:
        st = a.out / "stills"
        st.mkdir(exist_ok=True)
        r = ed.r
        m0, m1 = phase_range(r, "main")
        picks = sorted(
            {
                int(round(e["t"] * FPS / 2))
                for e in r.events
                if e["kind"] in ("attached", "in_basket", "grasp_check", "leg")
            }
        )
        idx = sorted(set(np.linspace(m0, m1, a.stills).astype(int).tolist()))
        tmap = [f["t"] for f in r.frames]
        for e in r.events:
            if e["kind"] in ("grasp_check", "in_basket", "leg", "on_counter", "replan"):
                idx.append(min(len(tmap) - 1, bisect.bisect_left(tmap, e["t"] + 0.5)))
        for fi in sorted(set(idx))[: a.stills * 3]:
            ed.compose(fi).save(st / f"main_{fi:06d}.jpg", quality=88)
        ed.plan_card(m0, 1)[0].save(st / "card_plan.jpg", quality=88)
        ed.title_card(0, 1)[0].save(st / "card_title.jpg", quality=88)
        ed.paid_card(m1, 1)[0].save(st / "card_paid.jpg", quality=88)
        print(f"stills in {st}", flush=True)
        del picks
        return
    if a.only == "early":
        write(a.out / f"early_cut{a.tag}.mp4", full_cut(ed, early=True))
        return
    if a.only in (None, "highlight"):
        write(
            a.out / f"picky_so101_highlight{a.tag}.mp4",
            highlight(ed),
            gif=a.out / f"picky_so101_preview{a.tag}.gif",
            gif_range=(int(6 * FPS), int(20 * FPS)),
        )
    if a.only in (None, "full"):
        write(a.out / f"picky_so101_supermarket_full{a.tag}.mp4", full_cut(ed))


if __name__ == "__main__":
    main()
