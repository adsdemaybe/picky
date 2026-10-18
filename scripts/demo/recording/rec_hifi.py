"""High-fidelity on-host recorder for the Picky Isaac Sim trip (runs ON vhagar, reads the gateway on localhost).

    cd /data/advaiv2/picky-rec && uv run --no-project --with pillow --with numpy --with "imageio[ffmpeg]" \
        python rec_hifi.py

What it does
  1. waits until GET /api/sim/status lists an Isaac robot (REC_ROBOT if set),
  2. snapshots the store's goal ids, then waits for a NEW goal with an Isaac robot assigned,
  3. opens that robot's MJPEG streams (<robot>.director, .front.perceived, .wrist.perceived) + one ceiling camera,
     reading them with read1() (read() blocks on multipart bodies),
  4. writes, all on ONE wall-clock timeline at REC_FPS (default 30; the latest frame is duplicated when a stream is
     slower, black until a camera's first frame): one RAW mp4 per camera at its native size + a 1920x1080 composite
     (director big, on-board / wrist / bird's-eye PiPs, step log), libx264 crf REC_CRF (16),
  5. logs events.jsonl (brain steps, goal / trip state changes, approvals, photo checks, payment agent steps, with
     t = seconds since the first recorded frame) for the editor's captions, and stats.json (per-camera native size,
     unique frames, real fps),
  6. stops REC_TAIL_S (10) s after that goal is completed / failed / cancelled (REC_STOP_ON=trip: after the trip
     reaches a paid/terminal state), or after REC_MAX_S, or on Ctrl-C (files are always closed cleanly).

Env (all optional)
  REC_GW=http://localhost:8100   REC_STORE=store_ave   REC_ROBOT=           (restrict to / pre-warm one robot)
  REC_CEILING=                   (camera id; default the store's first ceiling camera; 'none' = skip it)
  REC_OUT=/data/advaiv2/picky-rec/<YYYYmmdd-HHMMSS>   REC_FPS=30   REC_CRF=16   REC_PRESET=veryfast
  REC_MAX_S=1500   REC_TAIL_S=10   REC_STOP_ON=goal|trip   REC_DIRECTOR_FPS=15   REC_CAM_FPS=15
  REC_NOWAIT=1     record right away for REC_MAX_S using REC_ROBOT (default rover-ave-1): the dry test
  REC_JPEG=1       also keep every received JPEG (<out>/jpeg/<camera>/<ms>.jpg)
GET requests only; it never posts anything to the gateway.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

E = os.environ
GW = E.get("REC_GW", "http://localhost:8100").rstrip("/")
STORE = E.get("REC_STORE", "store_ave")
ROBOT = E.get("REC_ROBOT", "").strip()
NOWAIT = E.get("REC_NOWAIT") == "1"
FPS = int(E.get("REC_FPS", "30"))
CRF = E.get("REC_CRF", "16")
PRESET = E.get("REC_PRESET", "veryfast")
MAX_S = float(E.get("REC_MAX_S", "1500"))
TAIL_S = float(E.get("REC_TAIL_S", "10"))
STOP_ON = E.get("REC_STOP_ON", "goal")
DIRECTOR_FPS = E.get("REC_DIRECTOR_FPS", "15")
CAM_FPS = E.get("REC_CAM_FPS", "15")
SAVE_JPEG = E.get("REC_JPEG") == "1"
OUT = Path(E.get("REC_OUT") or f"/data/advaiv2/picky-rec/{time.strftime('%Y%m%d-%H%M%S')}")
ACTIVE = {"assigned", "active", "running", "waiting"}
TERMINAL = {"completed", "failed", "cancelled", "canceled", "aborted"}
TRIP_DONE = {"paid", "completed", "done", "followup", "receipt", "cancelled", "failed"}
W, H = 1920, 1080

stop_evt = threading.Event()
OUT.mkdir(parents=True, exist_ok=True)
_logf = open(OUT / "rec.log", "a", buffering=1)


def log(*a: object) -> None:
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    _logf.write(line + "\n")


def get(path: str, timeout: float = 5.0) -> object | None:
    try:
        with urllib.request.urlopen(GW + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001
        return None


def as_list(d: object, key: str) -> list:
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and isinstance(d.get(key), list):
        return d[key]
    return []


# ---------------------------------------------------------------------------------------------------------------------
# MJPEG reader (one thread per camera)
# ---------------------------------------------------------------------------------------------------------------------
class Cam:
    def __init__(self, cid: str, label: str, size: tuple[int, int], fps: str) -> None:
        self.cid, self.label, self.fps = cid, label, fps
        self.size = (size[0] // 2 * 2, size[1] // 2 * 2)  # writer size; libx264 yuv420p needs even dims
        self.img: Image.Image | None = None
        self.seq = 0
        self.native: tuple[int, int] | None = None
        self.sizes: dict[str, int] = {}
        self.arrivals: list[float] = []  # wall-clock arrival time of every unique frame while recording
        self.lock = threading.Lock()
        self.recording = False
        self.reconnects = 0

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"rd-{self.cid}", daemon=True).start()

    def _run(self) -> None:
        q = urllib.parse.urlencode({"fps": self.fps})
        url = f"{GW}/api/cameras/{urllib.parse.quote(self.cid)}/stream.mjpg?{q}"
        jdir = OUT / "jpeg" / self.cid
        if SAVE_JPEG:
            jdir.mkdir(parents=True, exist_ok=True)
        while not stop_evt.is_set():
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    buf = b""
                    while not stop_evt.is_set():
                        chunk = r.read1(1 << 20)
                        if not chunk:
                            break
                        buf += chunk
                        while True:
                            jpg, buf = _next_part(buf)
                            if jpg is None:
                                break
                            self._frame(jpg, jdir)
                        if len(buf) > 32_000_000:
                            buf = b""
            except Exception as e:  # noqa: BLE001
                if not stop_evt.is_set():
                    self.reconnects += 1
                    log(f"[{self.cid}] reconnect: {e}")
                    time.sleep(1.0)

    def _frame(self, jpg: bytes, jdir: Path) -> None:
        now = time.time()
        try:
            img = Image.open(BytesIO(jpg))
            img.load()
            img = img.convert("RGB")
        except Exception:  # noqa: BLE001
            return
        with self.lock:
            self.img = img
            self.seq += 1
            self.native = img.size
            k = f"{img.size[0]}x{img.size[1]}"
            self.sizes[k] = self.sizes.get(k, 0) + 1
            if self.recording:
                self.arrivals.append(now)
        if SAVE_JPEG and self.recording:
            (jdir / f"{int(now * 1000)}.jpg").write_bytes(jpg)

    def latest(self) -> tuple[int, Image.Image | None]:
        with self.lock:
            return self.seq, self.img


def _next_part(buf: bytes) -> tuple[bytes | None, bytes]:
    """Pop one JPEG from a multipart buffer: Content-Length when present, else SOI..EOI."""
    hi = buf.find(b"Content-Length:")
    soi = buf.find(b"\xff\xd8")
    if hi != -1 and (soi == -1 or hi < soi):
        he = buf.find(b"\r\n\r\n", hi)
        if he == -1:
            return None, buf
        try:
            n = int(buf[hi + 15 : buf.find(b"\r\n", hi)].strip())
        except ValueError:
            n = -1
        if n > 0:
            s = he + 4
            if len(buf) < s + n:
                return None, buf
            return buf[s : s + n], buf[s + n :]
    if soi == -1:
        return None, buf[-2:] if len(buf) > 2 else buf
    eoi = buf.find(b"\xff\xd9", soi + 2)
    if eoi == -1:
        return None, buf[soi:]
    return buf[soi : eoi + 2], buf[eoi + 2 :]


# ---------------------------------------------------------------------------------------------------------------------
# Writers (ffmpeg via imageio-ffmpeg; one queue + thread each so the clock never blocks on a pipe)
# ---------------------------------------------------------------------------------------------------------------------
class Writer:
    def __init__(self, path: Path, size: tuple[int, int]) -> None:
        self.path, self.size = path, size
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=FPS * 20)
        self.n = 0
        self.gen = imageio_ffmpeg.write_frames(
            str(path),
            size,
            fps=FPS,
            codec="libx264",
            pix_fmt_in="rgb24",
            pix_fmt_out="yuv420p",
            quality=None,
            macro_block_size=1,
            output_params=["-crf", CRF, "-preset", PRESET, "-movflags", "+faststart"],
            ffmpeg_log_level="error",
        )
        self.gen.send(None)
        self.t = threading.Thread(target=self._run, name=f"wr-{path.name}", daemon=True)
        self.t.start()

    def _run(self) -> None:
        while True:
            b = self.q.get()
            if b is None:
                break
            self.gen.send(b)
            self.n += 1

    def put(self, b: bytes) -> None:
        self.q.put(b)

    def close(self) -> None:
        self.q.put(None)
        self.t.join()
        self.gen.close()


# ---------------------------------------------------------------------------------------------------------------------
# Pollers: steps + goal / trip / approvals / photo checks / payment -> events.jsonl + the composite's text panels
# ---------------------------------------------------------------------------------------------------------------------
class State:
    def __init__(self) -> None:
        self.t0: float | None = None
        self.steps: list[dict] = []
        self.goal: dict = {}
        self.trip: dict = {}
        self.seen: set[str] = set()
        self.version = 0
        self.lock = threading.Lock()
        self.ev = open(OUT / "events.jsonl", "a", buffering=1)

    def event(self, kind: str, **data: object) -> None:
        t = None if self.t0 is None else round(time.time() - self.t0, 3)
        self.ev.write(json.dumps({"t": t, "wall": time.time(), "kind": kind, **data}) + "\n")


def poller(st: State, robot: str, goal_id: str | None) -> None:
    last_goal_status = last_trip_state = None
    while not stop_evt.is_set():
        q = f"?goal_id={goal_id}&limit=50" if goal_id else "?limit=50"
        rows = as_list(get(f"/api/fleet/robots/{robot}/steps{q}"), "steps")
        for x in sorted(rows, key=lambda x: x.get("ts") or ""):
            sid = x.get("step_id")
            if sid and sid not in st.seen:
                st.seen.add(sid)
                st.event("step", step_kind=x.get("kind"), tool=x.get("tool"), text=x.get("text"), ok=x.get("ok"),
                         ts=x.get("ts"), camera_id=x.get("camera_id"))  # fmt: skip
        if rows:
            with st.lock:
                st.steps = sorted(rows, key=lambda x: x.get("ts") or "")[-8:]
                st.version += 1
        if goal_id:
            g = get(f"/api/goals/{goal_id}")
            if isinstance(g, dict) and g.get("goal_id"):
                with st.lock:
                    st.goal = g
                    st.version += 1
                if g.get("status") != last_goal_status:
                    last_goal_status = g.get("status")
                    st.event("goal", status=last_goal_status, title=g.get("title"), trip_id=g.get("trip_id"))
                tid = g.get("trip_id")
                if tid:
                    t = get(f"/api/trips/{tid}")
                    if isinstance(t, dict) and t.get("trip_id"):
                        with st.lock:
                            st.trip = t
                        if t.get("state") != last_trip_state:
                            last_trip_state = t.get("state")
                            st.event("trip", state=last_trip_state)
                    for a in as_list(get(f"/api/approvals?trip_id={tid}"), "approvals"):
                        k = "approval:" + str(a.get("approval_id") or a.get("id"))
                        if k not in st.seen:
                            st.seen.add(k)
                            st.event("approval", kind_=a.get("kind"), title=a.get("title") or a.get("question"),
                                     sku=a.get("sku"))  # fmt: skip
                    for v in as_list(get(f"/api/verifications?trip_id={tid}"), "verifications"):
                        k = "verify:" + str(v.get("verification_id") or v.get("id"))
                        if k not in st.seen:
                            st.seen.add(k)
                            st.event("photo_check", sku=v.get("sku"), name=v.get("name") or v.get("title"),
                                     status=v.get("status"))  # fmt: skip
                    for p in as_list(get(f"/api/trips/{tid}/payment"), "steps"):
                        k = "pay:" + str(p.get("step_id") or p.get("id") or p.get("name") or json.dumps(p)[:80])
                        if k not in st.seen:
                            st.seen.add(k)
                            st.event("payment", step=p.get("name") or p.get("step") or p.get("title"),
                                     status=p.get("status"), text=p.get("text") or p.get("detail"))  # fmt: skip
        stop_evt.wait(1.0)


# ---------------------------------------------------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------------------------------------------------
def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        ["/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
        if bold
        else ["/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]
    )
    for p in names + ["/usr/share/fonts/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc"]:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


BG, FG, DIM, ACC, BAD = (12, 14, 13), (238, 242, 239), (150, 165, 156), (74, 222, 128), (255, 120, 120)
F_TITLE, F_LABEL, F_STEP, F_STEPB = font(30, True), font(22, True), font(23), font(23, True)
MAIN = (0, 56, 1440, 810)  # x, y, w, h  director
PIPS = [(1446, 56, 468, 340), (1446, 402, 468, 340), (1446, 748, 468, 326)]  # front.perceived, wrist, ceiling
LOGBOX = (0, 872, 1440, 208)


def _local(ts: str | None) -> str:
    """ISO UTC ('...Z') -> local HH:MM:SS (the gateway stamps steps in UTC)."""
    from datetime import datetime

    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return (ts or "")[11:19]


def fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Letterbox ``img`` into w x h (aspect kept)."""
    s = min(w / img.width, h / img.height)
    nw, nh = max(2, round(img.width * s)), max(2, round(img.height * s))
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(img.resize((nw, nh), Image.Resampling.BICUBIC),
              ((w - nw) // 2, (h - nh) // 2))  # fmt: skip
    return out


class Composer:
    def __init__(self, cams: list[Cam], st: State, robot: str, robot_name: str, store_name: str) -> None:
        self.cams, self.st, self.robot = cams, st, robot
        self.cache: dict[str, tuple[int, Image.Image]] = {}
        self.base = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(self.base)
        d.text((22, 12), f"Picky  ·  {robot_name} ({robot}) shopping live in Isaac Sim  ·  {store_name}", fill=FG,
               font=F_TITLE)  # fmt: skip
        self.log_ver = -1
        self.log_img = Image.new("RGB", LOGBOX[2:], BG)

    def _pane(self, cam: Cam, box: tuple[int, int, int, int]) -> Image.Image:
        seq, img = cam.latest()
        c = self.cache.get(cam.cid)
        if c is not None and c[0] == seq:
            return c[1]
        x, y, w, h = box
        if img is None:
            pane = Image.new("RGB", (w, h), (24, 26, 25))
            ImageDraw.Draw(pane).text((16, h // 2 - 12), f"waiting for {cam.cid} ...", fill=DIM, font=F_LABEL)
        else:
            pane = fit(img, w, h)
        d = ImageDraw.Draw(pane)
        tw = d.textlength(cam.label, font=F_LABEL)
        d.rectangle([0, h - 34, tw + 24, h], fill=(0, 0, 0))
        d.text((12, h - 31), cam.label, fill=FG, font=F_LABEL)
        self.cache[cam.cid] = (seq, pane)
        return pane

    def _log(self) -> Image.Image:
        with self.st.lock:
            ver, steps, goal = self.st.version, list(self.st.steps), dict(self.st.goal)
        if ver == self.log_ver:
            return self.log_img
        self.log_ver = ver
        img = Image.new("RGB", LOGBOX[2:], (18, 21, 19))
        d = ImageDraw.Draw(img)
        head = "What the robot is doing"
        if goal:
            note = goal.get("progress_note") or ""
            head += f"  ·  {goal.get('title') or goal.get('kind')}  ·  {goal.get('status')}" + (
                f"  ·  {note}" if note else ""
            )
        while head and d.textlength(head, font=F_STEPB) > LOGBOX[2] - 32:
            head = head[:-2]
        d.text((16, 8), head, fill=ACC, font=F_STEPB)
        for i, x in enumerate(steps[-5:]):
            tag = x.get("tool") or x.get("kind") or ""
            txt = " ".join(str(x.get("text") or "").split())
            ok = x.get("ok", True)
            y = 44 + i * 32
            d.text((16, y), _local(x.get("ts")), fill=DIM, font=F_STEP)
            d.text((130, y), tag[:16], fill=(125, 211, 252), font=F_STEPB)
            d.text((356, y), (txt[:90] + ("…" if len(txt) > 90 else "")) + ("" if ok else "  [FAILED]"),
                   fill=FG if ok else BAD, font=F_STEP)  # fmt: skip
        self.log_img = img
        return img

    def frame(self) -> bytes:
        el = 0 if self.st.t0 is None else time.time() - self.st.t0
        key = (tuple(c.seq for c in self.cams), self.st.version, int(el))
        if key == getattr(self, "_key", None):
            return self._bytes  # nothing changed since the last tick: reuse (cheap 30 fps when streams are slower)
        self._key = key
        c = self.base.copy()
        c.paste(self._pane(self.cams[0], MAIN), MAIN[:2])
        for cam, box in zip(self.cams[1:], PIPS, strict=False):
            c.paste(self._pane(cam, box), box[:2])
        c.paste(self._log(), LOGBOX[:2])
        d = ImageDraw.Draw(c)
        clock = time.strftime("%H:%M:%S")
        s = f"● REC {int(el // 60):02d}:{int(el % 60):02d}   {clock}"
        d.text((W - 22 - d.textlength(s, font=F_LABEL), 16), s, fill=(255, 90, 90), font=F_LABEL)
        self._bytes = c.tobytes()
        return self._bytes


# ---------------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------------
def isaac_ids() -> list[str]:
    s = get("/api/sim/status")
    return list(s.get("isaac_robot_ids", [])) if isinstance(s, dict) else []


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())
    log(f"rec_hifi -> {OUT}  gw={GW} store={STORE} robot={ROBOT or 'auto'} fps={FPS} crf={CRF} preset={PRESET}"
        f" nowait={NOWAIT} stop_on={STOP_ON}")  # fmt: skip
    cams_info = {c["camera_id"]: c for c in as_list(get(f"/api/cameras?store_id={STORE}"), "cameras")}
    ceiling = E.get("REC_CEILING") or next((k for k, c in sorted(cams_info.items()) if c.get("kind") == "ceiling"), "")
    goal_id: str | None = None
    robot = ROBOT or "rover-ave-1"
    if not NOWAIT:
        log("waiting for an Isaac robot", ROBOT or "(any)")
        while not stop_evt.is_set():
            ids = isaac_ids()
            if ids and (not ROBOT or ROBOT in ids):
                break
            stop_evt.wait(3)
        log("Isaac robots:", isaac_ids())
        baseline = {g.get("goal_id") for g in as_list(get(f"/api/goals?store_id={STORE}&limit=100"), "goals")}
        log(f"baseline: {len(baseline)} goals in {STORE}; waiting for a NEW goal on an Isaac robot")
        while not stop_evt.is_set():
            ids = isaac_ids()
            for g in as_list(get(f"/api/goals?store_id={STORE}&limit=20"), "goals"):
                rid = g.get("robot_id")
                if g.get("goal_id") in baseline or not rid or rid not in ids or (ROBOT and rid != ROBOT):
                    continue
                if g.get("status") in ACTIVE or g.get("status") in TERMINAL:
                    goal_id, robot = g["goal_id"], rid
                    break
            if goal_id:
                break
            stop_evt.wait(0.5)
        if stop_evt.is_set():
            return 1
        log(f"NEW goal {goal_id} on {robot}: recording")
    fleet = {r.get("robot_id"): r for r in as_list(get("/api/fleet/robots"), "robots")}
    robot_name = (fleet.get(robot) or {}).get("name") or robot
    store_name = {"store_ave": "Picky Market - The Ave", "store_hill": "Picky Market - Capitol Hill"}.get(STORE, STORE)

    def size(cid: str, dflt: tuple[int, int]) -> tuple[int, int]:
        m = (cams_info.get(cid) or {}).get("mount") or {}
        return (int(m.get("width") or dflt[0]), int(m.get("height") or dflt[1]))

    cams = [
        Cam(f"{robot}.director", "Isaac Sim  ·  chase cam", size(f"{robot}.director", (1920, 1080)), DIRECTOR_FPS),
        Cam(f"{robot}.front.perceived", "On-board  ·  Perceptron Mk1.5", size(f"{robot}.front.perceived", (640, 480)),
            CAM_FPS),
        Cam(f"{robot}.wrist.perceived", "Wrist  ·  grasp + verifier view", size(f"{robot}.wrist.perceived", (640, 480)),
            CAM_FPS),
        Cam(ceiling, "Bird's-eye  ·  store ceiling", size(ceiling, (640, 480)), CAM_FPS),
    ]  # fmt: skip
    cams = [c for c in cams if c.cid and c.cid != "none"]  # REC_CEILING=none: one render product fewer on Isaac
    for c in cams:
        c.start()
    # give the streams a moment so the first second isn't all black (the director needs ~1 s to start rendering)
    for _ in range(30):
        if all(c.native for c in cams):
            break
        time.sleep(0.1)
    for c in cams:  # writer size = the first real frame's size (native), else the advertised mount size
        if c.native:
            c.size = (c.native[0] // 2 * 2, c.native[1] // 2 * 2)
    st = State()
    threading.Thread(target=poller, args=(st, robot, goal_id), daemon=True).start()
    comp = Composer(cams, st, robot, robot_name, store_name)
    writers = {c.cid: Writer(OUT / f"{c.cid}.mp4", c.size) for c in cams}
    wcomp = Writer(OUT / "composite_1080p30.mp4", (W, H))
    blank = {c.cid: bytes(c.size[0] * c.size[1] * 3) for c in cams}
    conv: dict[str, tuple[int, bytes]] = {}
    st.t0 = t0 = time.time()
    for c in cams:
        c.recording = True
    st.event("start", robot=robot, goal_id=goal_id, cameras=[c.cid for c in cams], sizes={c.cid: c.size for c in cams})
    n = dups = 0
    stop_at: float | None = None
    last_report = t0
    rep_seq = {c.cid: c.seq for c in cams}
    try:
        while not stop_evt.is_set():
            now = time.time()
            if now - t0 >= MAX_S:
                log("max duration reached")
                break
            if stop_at is None and not NOWAIT:
                with st.lock:
                    gs, ts = st.goal.get("status"), st.trip.get("state")
                done = (gs in TERMINAL) if STOP_ON == "goal" else (ts in TRIP_DONE or gs in {"failed", "cancelled"})
                if done:
                    stop_at = now + TAIL_S
                    log(f"goal {gs} / trip {ts}: stopping in {TAIL_S:.0f} s")
                    st.event("stop_scheduled", goal_status=gs, trip_state=ts)
            if stop_at is not None and now >= stop_at:
                break
            target = int((now - t0) * FPS) + 1
            if n < target:
                frame_bytes = {}
                for c in cams:
                    seq, img = c.latest()
                    if img is None:
                        frame_bytes[c.cid] = blank[c.cid]
                        continue
                    cc = conv.get(c.cid)
                    if cc is None or cc[0] != seq:
                        im = img if img.size == c.size else img.resize(c.size, Image.Resampling.LANCZOS)
                        cc = (seq, im.tobytes())
                        conv[c.cid] = cc
                    frame_bytes[c.cid] = cc[1]
                cb = comp.frame()
                if target - n > 1:
                    dups += target - n - 1
                while n < target:  # catch up with duplicates if a tick ran long: the timeline stays wall-clock true
                    for c in cams:
                        writers[c.cid].put(frame_bytes[c.cid])
                    wcomp.put(cb)
                    n += 1
            if now - last_report >= 5:
                parts = []
                for c in cams:
                    parts.append(f"{c.cid}={c.native[0] if c.native else '?'}x{c.native[1] if c.native else '?'}"
                                 f"@{(c.seq - rep_seq[c.cid]) / (now - last_report):.1f}fps")  # fmt: skip
                    rep_seq[c.cid] = c.seq
                log(f"t={now - t0:6.1f}s frames={n} late_dups={dups} qcomp={wcomp.q.qsize()} " + " ".join(parts))
                last_report = now
            time.sleep(max(0.0, t0 + n / FPS - time.time()))
    except KeyboardInterrupt:
        log("interrupted")
    dur = time.time() - t0
    stop_evt.set()
    for c in cams:
        c.recording = False
    st.event("stop", frames=n, seconds=round(dur, 2))
    log(f"closing writers ({n} frames, {dur:.1f} s)...")
    for wr in list(writers.values()) + [wcomp]:
        wr.close()
    stats = {
        "out": str(OUT),
        "robot": robot,
        "goal_id": goal_id,
        "seconds": round(dur, 2),
        "frames_written": n,
        "clock_catchup_dups": dups,
        "fps_out": FPS,
        "crf": CRF,
        "cameras": {},
    }
    for c in cams:
        a = c.arrivals
        real = (len(a) - 1) / (a[-1] - a[0]) if len(a) > 1 else 0.0
        gaps = [b - x for x, b in zip(a, a[1:], strict=False)]
        stats["cameras"][c.cid] = {
            "file": f"{c.cid}.mp4",
            "written_size": f"{c.size[0]}x{c.size[1]}",
            "native_sizes_seen": c.sizes,
            "unique_frames": len(a),
            "real_fps": round(real, 2),
            "max_gap_s": round(max(gaps), 2) if gaps else None,
            "reconnects": c.reconnects,
        }
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2))
    log("stats:", json.dumps(stats["cameras"]))
    log("done ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
