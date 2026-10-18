"""Record the gateway's MJPEG camera streams into one composite MP4 (wall-clock timed, 15 fps).

usage: rec_streams.py OUT.mp4 SECONDS GATEWAY ROBOT
Waits until /api/sim/status lists ROBOT as an Isaac robot, then records
<robot>.director (left, 1280x720), <robot>.front.perceived and <robot>.wrist.perceived (right column),
with a caption bar showing the latest trip/loop status from /api/sim/status.
"""

import json
import os
import sys
import threading
import time
import urllib.request
from io import BytesIO

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

out, seconds, gw, robot = sys.argv[1], float(sys.argv[2]), sys.argv[3].rstrip("/"), sys.argv[4]
FPS = 15
latest: dict[str, Image.Image] = {}
stop = False


def reader(cam: str) -> None:
    url = f"{gw}/api/cameras/{cam}/stream.mjpg"
    while not stop:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                buf = b""
                while not stop:
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2)
                    while a != -1 and b != -1:
                        try:
                            latest[cam] = Image.open(BytesIO(buf[a : b + 2])).convert("RGB")
                        except Exception:  # noqa: BLE001
                            pass
                        buf = buf[b + 2 :]
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2)
                    if len(buf) > 8_000_000:
                        buf = b""
        except Exception as e:  # noqa: BLE001
            print(cam, "reconnect:", e, flush=True)
            time.sleep(1)


steps: list[str] = []


def step_poller() -> None:
    while not stop:
        try:
            with urllib.request.urlopen(f"{gw}/api/fleet/robots/{robot}/steps", timeout=5) as r:
                rows = json.load(r)
            rows = sorted(rows, key=lambda x: x.get("ts", ""))[-9:]
            out = []
            for x in rows:
                tag = x.get("tool") or x.get("kind") or ""
                mark = "" if x.get("ok", True) else "  [FAILED]"
                out.append(f"{x.get('ts', '')[11:19]}  {tag:<16} {str(x.get('text') or '')[:120]}{mark}")
            steps[:] = out
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)


def isaac_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{gw}/api/sim/status", timeout=5) as r:
            return robot in json.load(r).get("isaac_robot_ids", [])
    except Exception:  # noqa: BLE001
        return False


print("waiting for Isaac robot", robot, flush=True)
while os.environ.get("REC_NOWAIT") != "1" and not isaac_ready():
    time.sleep(5)
print("Isaac connected; recording", seconds, "s ->", out, flush=True)
cams = [f"{robot}.director", f"{robot}.front.perceived", f"{robot}.wrist.perceived"]
for c in cams:
    threading.Thread(target=reader, args=(c,), daemon=True).start()
threading.Thread(target=step_poller, daemon=True).start()
font = None
for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc"):
    try:
        font = ImageFont.truetype(p, 26)
        small = ImageFont.truetype(p, 20)
        break
    except OSError:
        pass
if font is None:
    font = small = ImageFont.load_default()
W, H = 1920, 1080
w = imageio.get_writer(out, fps=FPS, codec="libx264", quality=7, macro_block_size=1)
t0 = time.time()
n = 0
while time.time() - t0 < seconds:
    canvas = Image.new("RGB", (W, H), (14, 16, 15))
    d = ImageDraw.Draw(canvas)
    slots = [(cams[0], (0, 60, 1280, 720), "Isaac Sim - chase cam"),
             (cams[1], (1280, 60, 640, 360), "On-board - Perceptron Mk1.5 boxes"),
             (cams[2], (1280, 420, 640, 360), "Wrist - Perceptron verifier view")]  # fmt: skip
    for cam, (x, y, cw, ch), label in slots:
        img = latest.get(cam)
        if img is not None:
            canvas.paste(img.resize((cw, ch)), (x, y))
        d.rectangle([x, y + ch - 30, x + cw, y + ch], fill=(0, 0, 0))
        d.text((x + 10, y + ch - 28), label, fill=(255, 255, 255), font=small)
    d.text((20, 16), "Picky - live loop: Gemini planner -> Perceptron VLA -> Perceptron verifier (Isaac Sim on vhagar)",
           fill=(255, 255, 255), font=font)  # fmt: skip
    d.text((20, 792), time.strftime("%H:%M:%S PT") + "   robot steps (GET /api/fleet/robots/" + robot + "/steps):",
           fill=(190, 210, 200), font=small)  # fmt: skip
    for i, line in enumerate(steps):
        d.text((20, 824 + i * 27), line, fill=(255, 120, 120) if "[FAILED]" in line else (235, 240, 236), font=small)
    w.append_data(np.asarray(canvas))
    n += 1
    time.sleep(max(0.0, t0 + n / FPS - time.time()))
stop = True
w.close()
print("wrote", out, n, "frames", flush=True)
