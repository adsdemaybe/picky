"""Composite a recorded run (``record.py``) + the worker's chase-camera frames (``arms/sim/director.py``) into the
"director" video: third-person view + live front / wrist cameras + a minimap + an overlay of what the loop is doing
(plan step, VLA tier / backend / chunk, verifier verdict + provider, where the item is according to the catalog
database, cart total).

    python arms/sim/tools/director/render.py RUN_DIR --director DIR/rover-ave-1 --out run.mp4 [--speed 4] \\
        [--start T0 --end T1] [--gif out.gif --gif-width 640 --gif-fps 6]

Needs Pillow + PyAV with libx264 (on vhagar: the SmolVLA venv). Times are wall-clock epoch seconds from the same host
(gateway events, /ws/frames frames and the worker's chase frames are all stamped on vhagar).
"""

from __future__ import annotations

import argparse
import bisect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
DIR_W, DIR_H = 840, 630
SIDE_W, SIDE_H = 440, 330
TOP = 45
BG = (16, 18, 22)
FG = (236, 238, 242)
DIM = (150, 156, 166)
GREEN = (80, 200, 120)
RED = (235, 90, 80)
AMBER = (240, 180, 60)
BLUE = (110, 170, 250)
ORANGE = (255, 140, 40)
FONT_DIRS = ["/usr/share/fonts/dejavu-sans-fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"]

STALE_S = 12.0
"""A side camera picture older than this is not shown (the camera only takes pictures while the robot works)."""
TIER_LABEL = {
    "t1_pretrained": "T1 learned policy",
    "t0_scripted": "T0 scripted",
    "t2_finetuned": "T2 fine-tuned",
}


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


F_S, F_M, F_B, F_L = font(14), font(16), font(16, True), font(20, True)


class FrameSeq:
    """Timestamped JPEG files (<epoch_ms>.jpg, one or more folders merged) with 'latest at or before t' lookup + a
    one-image decode cache."""

    def __init__(self, *folders: Path | None) -> None:
        files = [p for f in folders if f is not None and f.exists() for p in f.glob("*.jpg")]
        self.files: list[Path] = sorted(files, key=lambda p: int(p.stem))
        self.ts = [int(p.stem) / 1000.0 for p in self.files]
        self._cache: tuple[int, Image.Image] | None = None

    def at(self, t: float) -> tuple[Image.Image | None, float]:
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None, 1e9
        if self._cache is None or self._cache[0] != i:
            try:
                self._cache = (i, Image.open(self.files[i]).convert("RGB"))
            except OSError:
                return None, 1e9
        return self._cache[1], t - self.ts[i]


@dataclass
class LoopState:
    planner: str = "planning..."
    step: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    tier: str | None = None
    model: str | None = None
    chunk: int | None = None
    stage: str | None = None
    vla_phase: str | None = None
    verdict: dict[str, Any] = field(default_factory=dict)
    verdict_step: str = ""
    cart_total: int = 0
    cart_lines: int = 0
    budget: int | None = None
    location: dict[str, Any] = field(default_factory=dict)
    loc_sku: str = ""
    banner: tuple[str, tuple[int, int, int], float] | None = None
    stops: dict[str, dict[str, Any]] = field(default_factory=dict)
    done_skus: set[str] = field(default_factory=set)
    t1_grasps: int = 0
    t1_ok: int = 0
    fallbacks: int = 0
    trip_state: str = ""


def _loc_of(step: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(step.get("location"), dict):
        return step["location"]
    target = step.get("target")
    return target.get("location") if isinstance(target, dict) else None


class Timeline:
    def __init__(self, events: list[dict[str, Any]], robot: str, plan: dict[str, Any] | None = None) -> None:
        self.events = sorted(events, key=lambda r: r["t"])
        self.robot = robot
        self.i = 0
        self.s = LoopState()
        self.items: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, dict[str, Any]] = {sl["slot_id"]: sl for sl in (plan or {}).get("slots", [])}
        self.aisles = {a["aisle_id"]: a.get("label") for a in (plan or {}).get("aisles", [])}

    def _slot_loc(self, slot_id: str | None) -> dict[str, Any] | None:
        sl = self.slots.get(slot_id or "")
        if sl is None:
            return None
        return {
            "slot_id": sl["slot_id"],
            "aisle_label": self.aisles.get(sl.get("aisle_id") or "", sl.get("aisle_id")),
            "level": sl.get("level"),
            "item_position": sl.get("item_position"),
        }

    def advance(self, t: float) -> LoopState:
        s = self.s
        while self.i < len(self.events) and self.events[self.i]["t"] <= t:
            r = self.events[self.i]
            self.i += 1
            self._apply(r["t"], r["e"])
        if s.banner and s.banner[2] < t:
            s.banner = None
        return s

    def _apply(self, t: float, e: dict[str, Any]) -> None:
        s = self.s
        k = e.get("type")
        if e.get("robot_id") not in (None, self.robot) and k not in ("cart.updated", "trip.state_changed"):
            return
        if k == "plan.created" or k == "plan.revised":
            plan = e.get("plan") or {}
            pl = plan.get("planner") or {}
            if pl.get("origin") == "llm":
                s.planner = f"LLM planner: Gemini {pl.get('model') or ''}".strip()
            else:
                why = str(pl.get("fallback_reason") or "")
                why = "quota 429" if "429" in why else (why[:40] or "no LLM")
                s.planner = f"scripted planner (LLM fallback: {why})"
            for it in plan.get("items", []):
                self.items[it.get("item_id", "")] = it
                loc = self._slot_loc(it.get("slot_id"))
                if it.get("sku") and loc:
                    s.stops.setdefault(it["sku"], {"pos": loc.get("item_position"), "slot": it.get("slot_id")})
            if plan.get("budget_cents"):
                s.budget = plan["budget_cents"]
        elif k == "step.started":
            st = e.get("step") or {}
            s.step = st
            s.attempt = e.get("attempt") or 1
            s.tier = e.get("tier")
            s.chunk = None
            s.stage = None
            item = self.items.get(st.get("item_id") or "", {})
            tgt = st.get("target") if isinstance(st.get("target"), dict) else {}
            sku = item.get("sku") or tgt.get("sku") or st.get("expected_sku")
            loc = _loc_of(st) or self._slot_loc(item.get("slot_id") or st.get("slot_id"))
            if loc:
                s.location, s.loc_sku = loc, sku or s.loc_sku
            if st.get("kind") == "grasp" and s.tier == "t1_pretrained":
                s.t1_grasps += 1
        elif k == "vla.status":
            st = e.get("status") or {}
            s.tier = st.get("tier") or s.tier
            s.chunk = st.get("chunk_no") if st.get("chunk_no") is not None else s.chunk
            s.stage = st.get("stage") or s.stage
            s.vla_phase = st.get("phase")
            if st.get("fell_back_from"):
                s.banner = (f"VLA fell back from {st['fell_back_from']} to {st.get('tier')}", AMBER, t + 4)
        elif k == "step.verified":
            s.verdict = e.get("verdict") or {}
            s.verdict_step = f"{e.get('step_id')} {e.get('phase')}"
        elif k == "step.failed":
            d = e.get("decision") or {}
            s.banner = (d.get("reason") or "step failed", RED, t + 5)
            if (s.step.get("kind") == "grasp") and s.tier == "t1_pretrained":
                s.fallbacks += 1 if d.get("next_tier") == "t0_scripted" or d.get("open_breaker") else 0
        elif k == "vla.tier_changed":
            s.banner = (f"{e.get('from_tier')} -> {e.get('to_tier')}: {e.get('reason', '')}", AMBER, t + 5)
        elif k == "step.completed":
            st = s.step
            if st.get("step_id") == e.get("step_id") and st.get("kind") == "grasp" and e.get("status") == "succeeded":
                if s.tier == "t1_pretrained":
                    s.t1_ok += 1
            if st.get("kind") == "place" and e.get("status") == "succeeded":
                sku = self.items.get(st.get("item_id") or "", {}).get("sku")
                if sku:
                    s.done_skus.add(sku)
        elif k == "item.picked":
            sku = e.get("sku") or (e.get("item") or {}).get("sku")
            if sku:
                s.done_skus.add(sku)
        elif k == "cart.updated":
            c = e.get("cart") or {}
            s.cart_total = c.get("total_cents") or c.get("subtotal_cents") or 0
            s.cart_lines = len(c.get("lines") or [])
            s.budget = c.get("budget_cents") or s.budget
        elif k == "trip.state_changed":
            s.trip_state = e.get("to_state") or s.trip_state
            if e.get("to_state") in ("paid", "done"):
                s.banner = ("Checked out: paid (sandbox) - receipt issued", GREEN, t + 6)


def fit(img: Image.Image, w: int, h: int) -> Image.Image:
    return img.resize((w, h), Image.BILINEAR)


def box(d: ImageDraw.ImageDraw, xy: tuple[int, int, int, int], alpha: int = 170) -> None:
    d.rounded_rectangle(xy, radius=8, fill=(10, 12, 16, alpha))


def clip(d: ImageDraw.ImageDraw, text: str, f: ImageFont.ImageFont, max_w: float) -> str:
    """``text`` cut (with an ellipsis) to fit ``max_w`` pixels."""
    if d.textlength(text, font=f) <= max_w:
        return text
    while text and d.textlength(text + "...", font=f) > max_w:
        text = text[:-1]
    return text + "..."


def money(c: int) -> str:
    return f"${c / 100:.2f}"


class Minimap:
    def __init__(self, plan: dict[str, Any] | None, w: int = 200) -> None:
        self.plan = plan or {}
        b = self.plan.get("bounds") or {"x": 0, "y": 0, "w": 14, "h": 22}
        self.b = b
        self.scale = w / b["w"]
        self.w, self.h = w, int(b["h"] * self.scale)

    def xy(self, x: float, y: float, ox: int, oy: int) -> tuple[float, float]:
        return ox + (x - self.b["x"]) * self.scale, oy + self.h - (y - self.b["y"]) * self.scale

    def draw(self, d: ImageDraw.ImageDraw, ox: int, oy: int, s: LoopState, pose: dict[str, Any] | None) -> None:
        box(d, (ox - 6, oy - 24, ox + self.w + 6, oy + self.h + 6), 190)
        d.text((ox, oy - 21), "store map (catalog DB slots)", font=F_S, fill=DIM)
        d.rectangle((ox, oy, ox + self.w, oy + self.h), outline=(90, 96, 106))
        for f in self.plan.get("fixtures", []):
            r = f.get("rect") or {}
            x0, y1 = self.xy(r.get("x", 0), r.get("y", 0), ox, oy)
            x1, y0 = self.xy(r.get("x", 0) + r.get("w", 0), r.get("y", 0) + r.get("h", 0), ox, oy)
            d.rectangle((x0, y0, x1, y1), fill=(70, 76, 88))
        cur = s.loc_sku
        for sku, st in s.stops.items():
            p = st.get("pos") or {}
            if "x" not in p:
                continue
            x, y = self.xy(p["x"], p["y"], ox, oy)
            col = GREEN if sku in s.done_skus else (AMBER if sku == cur else FG)
            rr = 5 if sku == cur else 4
            d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=col, outline=(0, 0, 0))
        if pose:
            import math

            x, y = self.xy(pose["x"], pose["y"], ox, oy)
            th = pose.get("theta", 0.0)
            pts = [
                (x + 9 * math.cos(-th), y + 9 * math.sin(-th)),
                (x + 6 * math.cos(-th + 2.5), y + 6 * math.sin(-th + 2.5)),
                (x + 6 * math.cos(-th - 2.5), y + 6 * math.sin(-th - 2.5)),
            ]
            d.polygon(pts, fill=ORANGE, outline=(0, 0, 0))


def load_states(run: Path, robot: str) -> tuple[list[float], list[dict[str, Any]]]:
    """Robot poses over time: full-rate state.jsonl (live frames socket) if recorded, else /ws robot.state (2 Hz)."""
    rows: list[tuple[float, dict[str, Any]]] = []
    for name in ("state.jsonl", "events.jsonl"):
        p = run / name
        if not p.exists():
            continue
        for line in p.open():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            e = r["e"]
            if e.get("type") != "robot.state":
                continue
            st = e.get("state") or e
            if st.get("robot_id", e.get("robot_id")) not in (None, robot) or not st.get("pose"):
                continue
            rows.append((r["t"], st["pose"]))
        if rows:
            break
    rows.sort(key=lambda x: x[0])
    return [t for t, _ in rows], [p for _, p in rows]


def compose(
    t: float,
    t0: float,
    speed: float,
    tl: Timeline,
    cams: dict[str, FrameSeq],
    mm: Minimap,
    st_ts: list[float],
    st_poses: list[dict[str, Any]],
    request: str,
    model: str,
) -> Image.Image:
    s = tl.advance(t)
    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas, "RGBA")
    # header
    d.text(
        (14, 11),
        "PICKY  |  words -> LLM plan -> VLA grasps -> Perceptron verifier  |  Isaac Sim on vhagar",
        font=F_L,
        fill=FG,
    )
    el = t - t0
    d.text((W - 210, 13), f"t+{int(el // 60):02d}:{int(el % 60):02d}   x{speed:g} speed", font=F_M, fill=DIM)
    # director view
    img, age = cams["director"].at(t)
    if img is not None:
        canvas.paste(fit(img, DIR_W, DIR_H), (0, TOP))
    else:
        d.text((300, 330), "(waiting for the chase camera)", font=F_M, fill=DIM)
    # right column cameras
    for k, (name, label) in enumerate(
        (("front", "front camera / policy side view (blurred)"), ("wrist", "wrist camera (blurred)"))
    ):
        y = TOP + k * SIDE_H
        im, a = cams[name].at(t)
        if im is not None and a <= STALE_S:
            canvas.paste(fit(im, SIDE_W, SIDE_H), (DIR_W, y))
        else:
            d.text((DIR_W + 120, y + SIDE_H // 2), "(no picture right now)", font=F_M, fill=DIM)
        box(d, (DIR_W + 6, y + 6, DIR_W + 12 + int(d.textlength(label, font=F_S)) + 8, y + 28), 160)
        d.text((DIR_W + 12, y + 9), label, font=F_S, fill=FG)
        if a < 0.6:
            d.ellipse(
                (DIR_W + SIDE_W - 22, y + 10, DIR_W + SIDE_W - 10, y + 22), fill=RED
            )  # live dot: picture < 0.6 s old
    d.line((DIR_W, TOP, DIR_W, H), fill=(40, 44, 52), width=2)
    # request + planner (top-left)
    box(d, (10, TOP + 10, 560, TOP + 62))
    d.text((20, TOP + 15), f"“{request}”", font=F_B, fill=FG)
    d.text((20, TOP + 39), s.planner, font=F_S, fill=BLUE)
    # minimap (top-right of the director view)
    i = bisect.bisect_right(st_ts, t) - 1
    pose = st_poses[i] if i >= 0 else None
    mm.draw(d, DIR_W - mm.w - 16, TOP + 40, s, pose)
    # loop panel (bottom-left)
    y0 = TOP + DIR_H - 150
    box(d, (10, y0, DIR_W - 10, TOP + DIR_H - 10))
    st = s.step
    title = st.get("title") or st.get("kind") or "-"
    d.text(
        (22, y0 + 8),
        f"STEP {st.get('step_id', '')}  {st.get('kind', '')}: {title}"
        + (f"  (attempt {s.attempt})" if s.attempt and s.attempt > 1 else ""),
        font=F_B,
        fill=FG,
    )
    tier = s.tier or (
        "(no motion: verifier / catalog step)"
        if st.get("kind") in ("ground_target", "scan_shelf", "ask_user", "verify")
        else "-"
    )
    tl_label = TIER_LABEL.get(tier, tier)
    if tier == "t1_pretrained":
        tl_label += f" · {model}"
    vla = f"VLA  {tl_label}"
    if s.chunk is not None:
        vla += f" · chunk {s.chunk}"
    if s.stage:
        vla += f" · {s.stage}"
    d.text((22, y0 + 34), vla, font=F_M, fill=ORANGE if tier == "t1_pretrained" else FG)
    v = s.verdict
    if v:
        out = (v.get("outcome") or "").upper()
        col = GREEN if out == "PASS" else (RED if out == "FAIL" else AMBER)
        prov = v.get("provider") or {}
        by = v.get("decided_by") or ""
        who = f"verified by Perceptron {prov.get('model') or ''}".strip() if by == "provider" else f"decided by {by}"
        head = f"VERIFIER {out}"
        d.text((22, y0 + 58), head, font=F_B, fill=col)
        x = 22 + d.textlength(head, font=F_B) + 12
        d.text((x, y0 + 58), clip(d, f"{who}: {v.get('reason') or ''}", F_M, DIR_W - 30 - x), font=F_M, fill=FG)
    loc = s.location
    if loc:
        p = loc.get("item_position") or {}
        d.text(
            (22, y0 + 82),
            clip(
                d,
                f"WHERE (catalog DB)  {s.loc_sku or ''} -> slot {loc.get('slot_id')} · "
                f"{(loc.get('aisle_label') or '').split(' - ')[0]} · level {loc.get('level')} · "
                f"({p.get('x', 0):.2f}, {p.get('y', 0):.2f}, {p.get('z', 0):.2f}) m",
                F_S,
                DIR_W - 52,
            ),
            font=F_S,
            fill=DIM,
        )
    cart = f"CART  {s.cart_lines} lines · {money(s.cart_total)}" + (f" / {money(s.budget)} budget" if s.budget else "")
    d.text((22, y0 + 106), cart, font=F_B, fill=FG)
    d.text(
        (360, y0 + 106),
        f"T1 grasps {s.t1_ok}/{s.t1_grasps} ok · {len(s.done_skus)}/{len(s.stops)} items in basket"
        + (f" · trip {s.trip_state}" if s.trip_state else ""),
        font=F_M,
        fill=DIM,
    )
    # banner
    if s.banner:
        text, col, _ = s.banner
        text = text if len(text) < 110 else text[:107] + "..."
        wtxt = int(d.textlength(text, font=F_B))
        bx = max(10, (DIR_W - wtxt) // 2 - 14)
        d.rounded_rectangle((bx, TOP + 74, bx + wtxt + 28, TOP + 106), radius=8, fill=(*col, 215))
        d.text((bx + 14, TOP + 81), text, font=F_B, fill=(10, 10, 10))
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run")
    ap.add_argument("--director", required=True, help="<PICKY_SIM_DIRECTOR_DIR>/<robot> (chase + tee'd frames)")
    ap.add_argument("--robot", default="rover-ave-1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--speed", type=float, default=4.0)
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--start", type=float, default=None, help="seconds after the request (default 0)")
    ap.add_argument("--end", type=float, default=None, help="seconds after the request (default: end of run)")
    ap.add_argument("--model", default="Perceptron Mk1.5 (tool policy)", help="T1 policy label")
    ap.add_argument("--gif", default=None)
    ap.add_argument("--gif-width", type=int, default=640)
    ap.add_argument("--gif-fps", type=float, default=5.0)
    ap.add_argument("--stills", type=int, default=0, help="also write N evenly spaced JPEG stills next to --out")
    args = ap.parse_args()

    import av

    run = Path(args.run)
    events = [json.loads(line) for line in (run / "events.jsonl").open() if line.strip()]
    summary = json.loads((run / "summary.json").read_text()) if (run / "summary.json").exists() else {}
    req = json.loads((run / "request.json").read_text()) if (run / "request.json").exists() else {}
    plan = json.loads((run / "planogram.json").read_text()) if (run / "planogram.json").exists() else None
    request_text = (req.get("request") or {}).get("text") or req.get("text") or ""
    if not request_text:
        rec = next((r["e"] for r in events if r["e"].get("type") == "request.received"), {})
        request_text = (rec.get("request") or {}).get("text", "")
    t_start = summary.get("started") or events[0]["t"]
    t_end = summary.get("ended") or events[-1]["t"]
    a = t_start + (args.start or 0.0)
    b = t_start + args.end if args.end is not None else t_end
    root = Path(args.director)  # <PICKY_SIM_DIRECTOR_DIR>/<robot>: director/ + the tee'd camera folders
    live = run / "frames"
    cams = {
        "director": FrameSeq(root / "director"),
        "front": FrameSeq(root / "front", root / "policy_side", live / f"{args.robot}.front"),
        "wrist": FrameSeq(root / "wrist", root / "policy_wrist", live / f"{args.robot}.wrist"),
    }
    tl = Timeline(events, args.robot, plan)
    st_ts, st_poses = load_states(run, args.robot)
    mm = Minimap(plan)
    out = av.open(args.out, "w")
    stream = out.add_stream("libx264", rate=int(args.fps))
    stream.width, stream.height, stream.pix_fmt = W, H, "yuv420p"
    stream.options = {"crf": "21", "preset": "veryfast"}
    gif_frames: list[Image.Image] = []
    step = args.speed / args.fps
    n = int((b - a) / step) + 1
    gif_every = max(1, round(args.fps / args.gif_fps))
    still_at = {int(n * (k + 0.5) / args.stills) for k in range(args.stills)} if args.stills else set()
    for k in range(n):
        t = a + k * step
        img = compose(t, t_start, args.speed, tl, cams, mm, st_ts, st_poses, request_text, args.model)
        for pkt in stream.encode(av.VideoFrame.from_image(img)):
            out.mux(pkt)
        if args.gif and k % gif_every == 0:
            gif_frames.append(img.resize((args.gif_width, int(H * args.gif_width / W)), Image.BILINEAR))
        if k in still_at:
            img.save(Path(args.out).with_suffix("").as_posix() + f"_still{k:05d}.jpg", quality=88)
    for pkt in stream.encode():
        out.mux(pkt)
    out.close()
    if args.gif and gif_frames:
        pal = [f.convert("P", palette=Image.ADAPTIVE, colors=128) for f in gif_frames]
        pal[0].save(
            args.gif, save_all=True, append_images=pal[1:], duration=int(1000 / args.gif_fps), loop=0, optimize=True
        )
    print(
        f"wrote {args.out}: {n} frames ({n / args.fps:.0f} s at {args.fps:g} fps, x{args.speed:g}) "
        f"from {b - a:.0f} s of run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
