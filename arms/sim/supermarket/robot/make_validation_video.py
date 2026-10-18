"""Turn a validate_so101_rover.py run into videos: a 4-camera review cut of the whole run and a short real-time
highlight clip of the pick (approach -> grasp -> basket), optionally sped up (labelled). Needs pillow + imageio[ffmpeg].

    python make_validation_video.py runs/val1 [--clip-start LABEL] [--clip-end EVENT] [--speed 1.5]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("--fps", type=int, default=30)
ap.add_argument("--clip-start", default="to_pregrasp", help="move label where the highlight clip starts")
ap.add_argument("--speed", type=float, default=1.0, help="highlight playback speed (1 = real time)")
ap.add_argument("--clip-end", default="released", help="event kind where it ends (+1 s)")
ap.add_argument("--max-clip-s", type=float, default=30.0, help="cap on the clip length in sim seconds")
args = ap.parse_args()

run = Path(args.run)
m = json.loads((run / "metrics.json").read_text())
fr = run / "frames"
n = min(len(list((fr / c).glob("*.jpg"))) for c in ("director", "chase", "wrist", "basket"))
dt_frame = 2 / 60
try:
    font = ImageFont.truetype("DejaVuSans.ttf", 26)
    small = ImageFont.truetype("DejaVuSans.ttf", 20)
except OSError:
    font = small = ImageFont.load_default()

# phase captions over time (move events are logged at their end with their duration)
spans = []
for e in m["events"]:
    if e["kind"] in ("move", "path"):
        spans.append((e["t_sim"] - e.get("T", 0), e["t_sim"], e["label"].replace("_", " ")))
    elif e["kind"] in ("grasp_check", "released", "after_drive", "sweep", "lift"):
        txt = {
            "grasp_check": f"grasp check: between jaws = {e.get('ok')}",
            "released": f"in basket = {e.get('in_basket')}",
            "after_drive": f"still in basket after driving = {e.get('in_basket')}",
            "sweep": f"sweep {e.get('joint')}",
            "lift": "",
        }[e["kind"]]
        spans.append((e["t_sim"], e["t_sim"] + 1.5, txt))


def caption(t: float) -> str:
    cur = [s for s in spans if s[0] <= t <= s[1]]
    return cur[-1][2] if cur else ""


def frame(cam: str, i: int, size) -> Image.Image:
    p = fr / cam / f"{i:06d}.jpg"
    if not p.exists():
        return Image.new("RGB", size, (20, 20, 20))
    return Image.open(p).convert("RGB").resize(size)


def label(img, txt, xy=(10, 8)):
    d = ImageDraw.Draw(img)
    d.rectangle([xy[0] - 4, xy[1] - 4, xy[0] + 10 + 11 * len(txt), xy[1] + 26], fill=(0, 0, 0))
    d.text(xy, txt, fill=(255, 255, 255), font=small)


def review(i: int) -> np.ndarray:
    W = Image.new("RGB", (1920, 1080), (12, 12, 14))
    W.paste(frame("director", i, (1280, 720)), (0, 0))
    for k, cam in enumerate(("chase", "wrist", "basket")):
        im = frame(cam, i, (640, 360))
        label(im, cam + " cam")
        W.paste(im, (1280, 360 * k))
    d = ImageDraw.Draw(W)
    t = i * dt_frame
    d.text((24, 740), f"Picky SO-101 rover - validation run   t = {t:5.1f} s", fill=(235, 235, 235), font=font)
    d.text((24, 790), caption(t), fill=(255, 200, 60), font=font)
    d.text(
        (24, 1040),
        "Isaac Sim 6.0 | Genie Sim supermarket_06 | PAL PMB2 base + long-reach Hiwonder SO-ARM101 + wire basket",
        fill=(150, 150, 150),
        font=small,
    )
    return np.asarray(W)


w = imageio.get_writer(run / "validation_review.mp4", fps=args.fps, codec="libx264", quality=7, macro_block_size=8)
for i in range(n):
    w.append_data(review(i))
w.close()

# highlight clip: director view, real time, from the clip-start move to clip-end + 1 s (capped)
t0 = next((e["t_sim"] - e.get("T", 0) for e in m["events"] if e.get("label") == args.clip_start), 0.0)
t1 = next((e["t_sim"] + 1.0 for e in m["events"] if e["kind"] == args.clip_end), n * dt_frame)
t1 = min(t1, t0 + args.max_clip_s)
i0, i1 = int(t0 / dt_frame), min(n, int(t1 / dt_frame))
w = imageio.get_writer(run / "pick_clip.mp4", fps=args.fps, codec="libx264", quality=8, macro_block_size=8)
for i in np.arange(i0, i1, args.speed).astype(int):
    im = frame("director", i, (1280, 720))
    wr = frame("wrist", i, (384, 288))
    label(wr, "wrist cam")
    im.paste(wr, (1280 - 384 - 12, 720 - 288 - 12))
    d = ImageDraw.Draw(im)
    d.text((20, 18), caption(i * dt_frame), fill=(255, 210, 70), font=font)
    if args.speed != 1.0:
        d.text((1280 - 110, 18), f"{args.speed:g}x", fill=(235, 235, 235), font=font)
    w.append_data(np.asarray(im))
w.close()
print(
    f"frames={n} review={run / 'validation_review.mp4'} clip={run / 'pick_clip.mp4'} "
    f"({(i1 - i0) * dt_frame:.1f} s sim, {(i1 - i0) * dt_frame / args.speed:.1f} s video)"
)
