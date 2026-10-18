"""Cut the final Picky demo video from the web-app capture + the on-host Isaac composite.

    uv run --no-project --with pillow --with numpy --with "imageio[ffmpeg]" python edit_final.py \
        --web WEBCAP/webcap_hq_1080p30.mp4 --web-events WEBCAP/events.json \
        --composite REC/composite_1080p30.mp4 --comp-events REC/events.jsonl --out-dir OUT

Writes OUT/final_1080p30.mp4 (libx264 crf 18, 30 fps):
  title card -> web app ordering (Home suggestions -> Order these -> /watch) -> the Isaac composite with captions for
  the key events (plan ready / each pick / photo check / unload / payment) -> web checkout (Visa payment agent) +
  receipt -> end card
and OUT/highlight_1080p30.mp4 (--highlight-s, default 75 s): the same story with the Isaac part time-compressed
(1x around each captioned event, fast-forward with an "N x" badge in between).

--web-events takes webcap.mjs's events.json, or the Python harness's report.json (timings_s) as a stand-in.
--comp-events takes rec_hifi.py's events.jsonl (t = seconds since the composite's first frame).
Inputs of any size / fps are normalised to 1920x1080 @ 30 fps (letterboxed).
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
FRAME = W * H * 3
BG, FG, DIM, ACC = (11, 31, 22), (240, 245, 241), (170, 190, 178), (200, 241, 90)


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf"]
        if bold
        else ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"]
    )
    for p in names + ["/System/Library/Fonts/Helvetica.ttc"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


F_BIG, F_SUB, F_SMALL, F_CAP, F_TAG, F_BADGE = (
    font(120, True),
    font(44),
    font(30),
    font(36, True),
    font(24, True),
    font(40, True),
)


# ---------------------------------------------------------------------------------------------------------------------
# events -> cut points and captions
# ---------------------------------------------------------------------------------------------------------------------
def web_marks(path: str | None) -> dict[str, float]:
    """Cut points (seconds into the web capture): picked, watch, checkout, paid, end."""
    if not path:
        return {}
    d = json.loads(Path(path).read_text())
    if "timings_s" in d:  # the Python harness's report.json (stand-in)
        t = d["timings_s"]
        return {k: v for k, v in {
            "home": t.get("cards_visible"), "picked": t.get("order_clicked", 0) - 1.5, "watch": t.get("live_page"),
            "checkout": t.get("checkout_approval_shown"), "paid": t.get("receipt"), "end": t.get("total"),
        }.items() if v is not None}  # fmt: skip
    m: dict[str, float] = {}
    for e in d.get("events", []):
        k = {"cards": "home", "picked": "picked", "watch": "watch", "checkout_card": "checkout", "paid": "paid",
             "end": "end"}.get(e["kind"])  # fmt: skip
        if k and k not in m:
            m[k] = float(e["t"])
    return m


def clean(text: str | None) -> str:
    t = " ".join(str(text or "").split())
    t = re.sub(r":\s*(succeeded|ok|done)\b.*$", "", t, flags=re.I)
    return t[:88] + ("…" if len(t) > 88 else "")


def comp_captions(path: str | None) -> list[tuple[float, str, str]]:
    """(t, TAG, text): plan ready / each pick / robot asks you / photo check / unload / payment."""
    if not path or not Path(path).exists():
        return []
    out: list[tuple[float, str, str]] = []
    plan = unload = paid = False
    for line in Path(path).read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("t")
        if t is None or t < 0:
            continue
        k, tool, text = e.get("kind"), e.get("tool"), str(e.get("text") or "")
        off = 0.0
        if k == "step" and not plan:
            plan, off = True, 4.0  # the goal's first step after the recorder started: the plan is ready
            out.append((t, "PLAN READY", "Route planned: your list + optional detours past suggested items"))
        if k == "step" and tool == "pick_item" and not text.startswith("Unloading"):
            if "succeeded" in text and "marked wrong" not in text:
                out.append((t + off, "PICK", clean(text)))
        elif k == "step" and tool == "ask_user" and "right" not in text.lower():
            out.append((t + off, "ASKS YOU", clean(text)))
        elif k == "photo_check":
            out.append((t, "PHOTO CHECK", f"Shopper confirms the pick: {clean(e.get('name') or e.get('sku'))}"))
        elif k == "step" and not unload and text.startswith("Unloading"):
            unload = True
            out.append((t + off, "CHECKOUT", "At the counter: the robot unloads the basket"))
        elif not paid and (
            (k == "trip" and str(e.get("state")) in {"paid", "completed", "followup"}) or k == "payment"
        ):
            paid = True
            out.append((t, "PAYMENT", "Visa payment agent (sandbox): mandate check -> approval -> paid"))
    out.sort()
    for i in range(1, len(out)):  # each caption stays readable >= 2.5 s before the next one replaces it
        if out[i][0] < out[i - 1][0] + 2.5:
            out[i] = (out[i - 1][0] + 2.5, out[i][1], out[i][2])
    return out


# ---------------------------------------------------------------------------------------------------------------------
# frames
# ---------------------------------------------------------------------------------------------------------------------
def read(path: str, start: float = 0.0, dur: float | None = None) -> Iterator[bytes]:
    """Frames of ``path`` from ``start`` normalised to 1920x1080 @ 30 fps (letterboxed)."""
    vf = f"fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    out = ["-vf", vf] + (["-t", f"{dur:.3f}"] if dur else [])
    gen = imageio_ffmpeg.read_frames(path, input_params=["-ss", f"{max(0.0, start):.3f}"], output_params=out)
    next(gen)  # meta
    for f in gen:
        if len(f) == FRAME:
            yield bytes(f)


def duration(path: str) -> float:
    n, secs = imageio_ffmpeg.count_frames_and_secs(path)
    return float(secs)


def card(lines: list[tuple[str, ImageFont.ImageFont, tuple[int, int, int]]]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    total = sum(f.size + 28 for _, f, _ in lines)
    y = (H - total) // 2
    for text, f, col in lines:
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, fill=col, font=f)
        y += f.size + 28
    return img


def caption(img: Image.Image, tag: str, text: str, where: str) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    tw = d.textlength(tag, font=F_TAG)
    xw = d.textlength(text, font=F_CAP)
    bw = int(tw + xw + 90)
    cx = 720 if where == "composite" else W // 2  # centre of the director pane in the composite
    x0 = max(10, cx - bw // 2)
    y0 = 76 if where == "composite" else H - 120
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + 66], radius=16, fill=(8, 10, 9, 215))
    d.rounded_rectangle([x0 + 14, y0 + 15, x0 + 34 + tw, y0 + 51], radius=9, fill=ACC + (255,))
    d.text((x0 + 24, y0 + 19), tag, fill=(10, 30, 15), font=F_TAG)
    d.text((x0 + tw + 56, y0 + 13), text, fill=FG, font=F_CAP)


def badge(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    w = d.textlength(text, font=F_BADGE)
    d.rounded_rectangle([1440 - w - 60, 72, 1440 - 16, 132], radius=14, fill=(0, 0, 0, 190))
    d.text((1440 - w - 38, 80), text, fill=ACC, font=F_BADGE)


class Out:
    def __init__(self, path: Path, crf: str) -> None:
        self.path = path
        self.n = 0
        self.gen = imageio_ffmpeg.write_frames(
            str(path), (W, H), fps=FPS, codec="libx264", pix_fmt_in="rgb24", pix_fmt_out="yuv420p", quality=None,
            macro_block_size=1, ffmpeg_log_level="error",
            output_params=["-crf", crf, "-preset", "medium", "-movflags", "+faststart"],
        )  # fmt: skip
        self.gen.send(None)

    def put(self, b: bytes) -> None:
        self.gen.send(b)
        self.n += 1

    def close(self) -> None:
        self.gen.close()


def fade(frames: list[bytes] | None, b: bytes, i: int, n: int, k: int = 12) -> bytes:
    """Fade in/out over k frames at the segment edges."""
    a = min(1.0, (i + 1) / k, (n - i) / k)
    if a >= 1.0:
        return b
    return (np.frombuffer(b, np.uint8).astype(np.float32) * a).astype(np.uint8).tobytes()


def still(out: Out, img: Image.Image, secs: float) -> None:
    b = img.tobytes()
    n = int(secs * FPS)
    for i in range(n):
        out.put(fade(None, b, i, n))


def segment(out: Out, path: str, start: float, end: float, speed_at=lambda t: 1.0,
            caps: list[tuple[float, str, str]] = (), where: str = "web", cap_s: float = 4.0) -> None:  # fmt: skip
    """Source [start, end) with a variable speed (speed_at(t_src)), captions shown cap_s output-seconds from their t."""
    if end <= start:
        return
    src = read(path, start, end - start + 0.5)
    # output schedule: source times for every output frame
    times, t = [], start
    while t < end:
        times.append(t)
        t += speed_at(t) / FPS
    n = len(times)
    cur, idx = None, -1
    shown: dict[int, int] = {}
    for i, ts in enumerate(times):
        want = int(round((ts - start) * FPS))
        while idx < want:
            nxt = next(src, None)
            if nxt is None:
                break
            cur, idx = nxt, idx + 1
        if cur is None:
            break
        sp = speed_at(ts)
        live = None
        for ci, (ct, tag, text) in enumerate(caps):
            if ct <= ts and (ci not in shown or i - shown[ci] < cap_s * FPS):
                shown.setdefault(ci, i)
                if i - shown[ci] < cap_s * FPS:
                    live = (tag, text)
        b = cur
        if live or sp > 1.05:
            img = Image.frombytes("RGB", (W, H), cur)
            if live:
                caption(img, live[0], live[1], where)
            if sp > 1.05 and where == "composite":
                badge(img, f"» {sp:.0f}× speed")
            b = img.tobytes()
        out.put(fade(None, b, i, n))


# ---------------------------------------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", required=True)
    ap.add_argument("--web-events")
    ap.add_argument("--composite", required=True)
    ap.add_argument("--comp-events")
    ap.add_argument("--raw-dir", help="per-camera raw files (kept for manual edits; not needed for the cut)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--title", default="Picky")
    ap.add_argument("--subtitle", default="Your personal shopping robot, live in Isaac Sim")
    ap.add_argument("--tagline", default="DubHacks 2026 · Visa Reimagine Shopping")
    ap.add_argument("--isaac-start", type=float, default=0.0)
    ap.add_argument("--isaac-end", type=float, default=0.0, help="0 = to the end of the composite")
    ap.add_argument("--isaac-speed", type=float, default=1.0, help="final cut: constant speed of the Isaac part")
    ap.add_argument("--highlight-s", type=float, default=75.0)
    ap.add_argument("--crf", default="18")
    ap.add_argument("--only", choices=["final", "highlight", "both"], default="both")
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wm = web_marks(a.web_events)
    web_len = duration(a.web)
    comp_len = duration(a.composite)
    i0, i1 = a.isaac_start, (a.isaac_end or comp_len)
    caps = [c for c in comp_captions(a.comp_events) if i0 <= c[0] < i1]
    print(f"web {web_len:.1f}s marks={wm}\ncomposite {comp_len:.1f}s captions={len(caps)}")
    for c in caps:
        print(f"  {c[0]:7.1f}s  {c[1]:<12} {c[2]}")
    w_home = max(0.0, wm.get("home", 0.0) - 0.5)
    w_watch = wm.get("watch", min(25.0, web_len))
    w_check = wm.get("checkout", max(0.0, web_len - 25))
    w_paid = wm.get("paid", web_len - 8)
    w_end = min(web_len, wm.get("end", web_len), w_paid + 12)
    web_caps_order = [(wm.get("picked", w_home + 3), "ORDER", "Tap what's running low -> Order these"),
                      (w_watch, "LIVE", "The fleet assigns a robot; the live view opens")]  # fmt: skip
    web_caps_pay = [(w_check, "PAYMENT", "Visa payment agent (sandbox): mandate check, then your approval"),
                    (w_paid, "RECEIPT", "Paid. Receipt + what the robot verified")]  # fmt: skip
    title = card([(a.title, F_BIG, FG), (a.subtitle, F_SUB, ACC), ("", F_SMALL, DIM), (a.tagline, F_SMALL, DIM)])
    credits = "Isaac Sim · SO-101 rover · Gemini planner · Perceptron VLA + verifier · Visa sandbox"
    end = card(
        [(a.title, F_BIG, FG), ("Shop by asking. The robot does the aisles.", F_SUB, ACC), ("", F_SMALL, DIM)]
        + [(credits, F_SMALL, DIM)]
    )

    if a.only in ("final", "both"):
        o = Out(out_dir / "final_1080p30.mp4", a.crf)
        still(o, title, 4.0)
        segment(o, a.web, w_home, w_watch + 6.0, caps=web_caps_order)
        segment(o, a.composite, i0, i1, speed_at=lambda t: a.isaac_speed, caps=caps, where="composite")
        segment(o, a.web, max(w_watch + 6.0, w_check - 3.0), w_end, caps=web_caps_pay)
        still(o, end, 4.0)
        o.close()
        print(f"wrote {o.path} ({o.n / FPS:.1f} s)")

    if a.only in ("highlight", "both"):
        budget = a.highlight_s - 2.5 - 2.5 - 8.0 - min(14.0, w_end - w_check + 1)
        # 1x windows around captioned events (priority: plan, picks, photo checks, unload, payment), fast in between
        pri = {"PLAN READY": 0, "PICK": 1, "PHOTO CHECK": 2, "CHECKOUT": 3, "PAYMENT": 4, "ASKS YOU": 5}
        chosen = sorted(caps, key=lambda c: (pri.get(c[1], 9), c[0]))
        wins: list[tuple[float, float]] = []
        for c in chosen:
            trial = merge(wins + [(max(i0, c[0] - 1.5), min(i1, c[0] + 4.5))])
            if sum(b - x for x, b in trial) <= 0.7 * budget:
                wins = trial
        win_total = sum(b - x for x, b in wins)
        gap_total = (i1 - i0) - win_total
        fast = max(1.0, gap_total / max(1.0, budget - win_total))
        hcaps = [c for c in caps if any(x <= c[0] < b for x, b in wins)]

        def speed_at(t: float) -> float:
            return 1.0 if any(x <= t < b for x, b in wins) else fast

        print(f"highlight: {len(wins)} 1x windows ({win_total:.1f}s), fast-forward {fast:.1f}x, budget {budget:.1f}s")
        o = Out(out_dir / "highlight_1080p30.mp4", a.crf)
        still(o, title, 2.5)
        order_speed = max(1.0, (w_watch + 2.0 - w_home) / 8.0)
        segment(o, a.web, w_home, w_watch + 2.0, speed_at=lambda t: order_speed, caps=web_caps_order, cap_s=2.5)
        segment(o, a.composite, i0, i1, speed_at=speed_at, caps=hcaps, where="composite", cap_s=3.5)
        segment(o, a.web, max(w_check - 1.0, w_watch + 2.0), min(w_end, w_check + 13.0), caps=web_caps_pay, cap_s=3.5)
        still(o, end, 2.5)
        o.close()
        print(f"wrote {o.path} ({o.n / FPS:.1f} s)")


def merge(wins: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, b in sorted(wins):
        if out and x <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((x, b))
    return out


if __name__ == "__main__":
    main()
