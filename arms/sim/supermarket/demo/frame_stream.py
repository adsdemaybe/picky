"""Stream the Isaac supermarket render (SO-101 rover `--demo`) to the web app: live MJPEG per camera + finished MP4s.

The render writes JPEG frames live into RUNS_ROOT/<run>/frames/<camera>/<NNNNNN>.jpg (cameras like director,
birdseye, side, wrist; RUNS_ROOT/<run>/<camera>/ works too). This relay serves the newest run (the run whose camera
folders changed last, or --run) to any browser; the web app's Live page shows it under "Direct streams". Stdlib only
(ThreadingHTTPServer), so it runs with the GPU host's system python3; it never touches the GPU or the render.

    python3 frame_stream.py --runs-root /data/advaiv2/picky-so101/runs --videos /data/advaiv2/picky-so101 --port 8120
    ssh -f -N -o ExitOnForwardFailure=yes -L 8120:localhost:8120 vhagar      # then, on the Mac:
    open http://localhost:8120/                                            # index page with every camera

Endpoints (CORS `*` on all of them):
    GET /health                  {"ok", "run", "cameras", "videos"}
    GET /cams                    {"run", "cameras": [{"name", "frames", "latest"}]} for the newest run (or --run)
    GET /stream/<cam>.mjpg       multipart/x-mixed-replace MJPEG of the newest complete JPEG (~12 fps, ?fps=N);
                                 resends the last frame once a second when the render is idle
    GET /frame/<cam>.jpg         the newest complete JPEG
    GET /videos                  [{"name", "size", "mtime", "url"}] for every *.mp4 under the --videos dirs
    GET /video/<name>.mp4        the MP4, with HTTP Range support (so <video> can seek)
A frame counts as complete once it is older than 0.2 s and is a whole JPEG (SOI ... EOI), so half-written files are
never sent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("frame_stream")

BOUNDARY = "pickyframe"
STABLE_S = 0.2  # a frame file must be this old before it is served (the writer may still be filling it)
IDLE_RESEND_S = 1.0  # resend the last frame this often when nothing new arrives (keeps <img> + proxies alive)
RUN_CACHE_S = 1.0
VIDEO_CACHE_S = 10.0
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CHUNK = 64 * 1024


class FrameStore:
    """Finds the newest run and the newest complete JPEG per camera; finds MP4s. Thread-safe, cached briefly."""

    def __init__(self, runs_root: Path, run: str | None = None, video_dirs: list[Path] | None = None) -> None:
        self.runs_root = runs_root
        self.run = run
        self.video_dirs = video_dirs or []
        self._lock = threading.Lock()
        self._run_cache: tuple[float, Path | None] = (0.0, None)
        self._video_cache: tuple[float, dict[str, Path]] = (0.0, {})

    # -- runs and cameras -------------------------------------------------------------------------------------------
    @staticmethod
    def _cam_root(run_dir: Path) -> Path:
        frames = run_dir / "frames"
        return frames if frames.is_dir() else run_dir

    def _cam_dirs(self, run_dir: Path) -> list[Path]:
        root = self._cam_root(run_dir)
        try:
            dirs = [d for d in root.iterdir() if d.is_dir() and NAME_RE.match(d.name)]
        except OSError:
            return []
        return sorted(d for d in dirs if any(n.endswith(".jpg") for n in _names(d)))

    def run_dir(self) -> Path | None:
        if self.run:
            p = Path(self.run)
            return p if p.is_absolute() else self.runs_root / self.run
        now = time.monotonic()
        with self._lock:
            ts, cached = self._run_cache
            if cached is not None and now - ts < RUN_CACHE_S:
                return cached
        best: tuple[float, Path] | None = None
        try:
            runs = [d for d in self.runs_root.iterdir() if d.is_dir()]
        except OSError:
            runs = []
        for d in runs:
            cams = self._cam_dirs(d)
            if not cams:
                continue
            mtime = max(_mtime(c) for c in cams)
            if best is None or mtime > best[0]:
                best = (mtime, d)
        found = best[1] if best else None
        with self._lock:
            self._run_cache = (now, found)
        return found

    def cameras(self) -> dict:
        run = self.run_dir()
        if run is None:
            return {"run": None, "cameras": []}
        out = []
        for d in self._cam_dirs(run):
            jpgs = sorted(n for n in _names(d) if n.endswith(".jpg"))
            out.append({"name": d.name, "frames": len(jpgs), "latest": jpgs[-1] if jpgs else None})
        return {"run": run.name, "cameras": out}

    def cam_dir(self, cam: str) -> Path | None:
        run = self.run_dir()
        if run is None or not NAME_RE.match(cam):
            return None
        d = self._cam_root(run) / cam
        return d if d.is_dir() else None

    def latest_frame(self, cam: str) -> tuple[str, bytes] | None:
        """(file name, JPEG bytes) of the newest complete frame for `cam`, or None."""
        d = self.cam_dir(cam)
        if d is None:
            return None
        names = sorted((n for n in _names(d) if n.endswith(".jpg")), reverse=True)
        now = time.time()
        for name in names[:6]:
            path = d / name
            try:
                if now - path.stat().st_mtime < STABLE_S:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > 4 and data[:2] == b"\xff\xd8" and data.rstrip(b"\x00")[-2:] == b"\xff\xd9":
                return name, data
        return None

    # -- videos -----------------------------------------------------------------------------------------------------
    def videos(self) -> dict[str, Path]:
        now = time.monotonic()
        with self._lock:
            ts, cached = self._video_cache
            if cached and now - ts < VIDEO_CACHE_S:
                return cached
        found: dict[str, Path] = {}
        for root in self.video_dirs:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [n for n in dirnames if n not in SKIP_DIRS and not n.startswith(".")]
                for fn in filenames:
                    if not fn.lower().endswith(".mp4") or not NAME_RE.match(fn):
                        continue
                    p = Path(dirpath) / fn
                    if fn not in found or _mtime(p) > _mtime(found[fn]):  # same name twice: the newest wins
                        found[fn] = p
        with self._lock:
            self._video_cache = (now, found)
        return found


def _names(d: Path) -> list[str]:
    try:
        return os.listdir(d)
    except OSError:
        return []


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """Byte range [start, end] from a Range header; None = whole file; False = unsatisfiable."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":  # suffix: the last N bytes
            n = int(end_s)
            if n <= 0:
                return False
            return max(0, size - n), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return False
    return start, min(end, size - 1)


class Handler(BaseHTTPRequestHandler):
    server_version = "PickyFrameStream/1.0"
    store: FrameStore  # set by make_server
    fps: float = 12.0

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (stdlib signature)
        log.debug("%s %s", self.address_string(), format % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: object, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _not_found(self, what: str) -> None:
        self._json({"error": f"not found: {what}"}, HTTPStatus.NOT_FOUND)

    def do_OPTIONS(self) -> None:  # noqa: N802 (stdlib naming)
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path == "/health":
                cams = self.store.cameras()
                self._json(
                    {
                        "ok": True,
                        "run": cams["run"],
                        "cameras": len(cams["cameras"]),
                        "videos": len(self.store.videos()),
                    }
                )
            elif path == "/cams":
                self._json(self.store.cameras())
            elif path == "/videos":
                vids = self.store.videos()
                rows = [
                    {"name": n, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime), "url": f"/video/{n}"}
                    for n, p in sorted(vids.items(), key=lambda kv: -_mtime(kv[1]))
                ]
                self._json(rows)
            elif path.startswith("/stream/") and path.endswith(".mjpg"):
                self._stream(path[len("/stream/") : -len(".mjpg")], query)
            elif path.startswith("/frame/") and path.endswith(".jpg"):
                frame = self.store.latest_frame(path[len("/frame/") : -len(".jpg")])
                if frame is None:
                    self._not_found(path)
                else:
                    self._send(200, frame[1], "image/jpeg", {"X-Frame": frame[0]})
            elif path.startswith("/video/"):
                self._video(path[len("/video/") :])
            elif path in ("/", "/index.html"):
                self._index()
            else:
                self._not_found(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser went away; nothing to do

    def _stream(self, cam: str, query: dict[str, list[str]]) -> None:
        if self.store.cam_dir(cam) is None:
            self._not_found(f"camera {cam}")
            return
        try:
            fps = min(30.0, max(1.0, float(query.get("fps", [self.fps])[0])))
        except ValueError:
            fps = self.fps
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        self.wfile.write(f"--{BOUNDARY}\r\n".encode())
        last_name, last_sent = None, 0.0
        while True:
            t0 = time.monotonic()
            frame = self.store.latest_frame(cam)
            if frame is not None and (frame[0] != last_name or t0 - last_sent >= IDLE_RESEND_S):
                name, data = frame
                head = f"Content-Type: image/jpeg\r\nContent-Length: {len(data)}\r\nX-Frame: {name}\r\n\r\n"
                # the trailing boundary ends this part right away, so the browser paints it without waiting
                self.wfile.write(head.encode() + data + f"\r\n--{BOUNDARY}\r\n".encode())
                self.wfile.flush()
                last_name, last_sent = name, t0
            time.sleep(max(0.0, 1.0 / fps - (time.monotonic() - t0)))

    def _video(self, name: str) -> None:
        p = self.store.videos().get(name)
        if p is None or not p.is_file():
            self._not_found(name)
            return
        size = p.stat().st_size
        rng = parse_range(self.headers.get("Range"), size)
        if rng is False:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._cors()
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = rng if rng else (0, size - 1)
        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if rng else HTTPStatus.OK)
        self._cors()
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with p.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _index(self) -> None:
        cams = self.store.cameras()
        tiles = "".join(
            f'<figure><img src="/stream/{n}.mjpg" alt="{n}"><figcaption>{n}</figcaption></figure>'
            for n in (c["name"] for c in cams["cameras"])
        )
        vids = "".join(f'<li><a href="/video/{n}">{n}</a></li>' for n in self.store.videos())
        html = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Picky sim streams</title><style>body{font:14px system-ui;margin:16px;background:#111;color:#eee}"
            "main{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}"
            "img{width:100%;background:#000;border-radius:8px}a{color:#9cf}</style>"
            f"<h1>Run {cams['run']}</h1><main>{tiles}</main><h2>Videos</h2><ul>{vids}</ul>"
        )
        self._send(200, html.encode(), "text/html; charset=utf-8")


def make_server(store: FrameStore, host: str = "127.0.0.1", port: int = 8120, fps: float = 12.0) -> ThreadingHTTPServer:
    """A ready-to-serve server (port 0 = pick a free port; see server.server_address)."""
    handler = type("BoundHandler", (Handler,), {"store": store, "fps": fps})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs-root", type=Path, required=True, help="folder with one sub-folder per render run")
    ap.add_argument("--run", default=None, help="serve this run (name under --runs-root or a path); default: newest")
    ap.add_argument("--videos", type=Path, action="append", default=[], help="folder searched for *.mp4 (repeatable)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8120)
    ap.add_argument("--fps", type=float, default=12.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = FrameStore(args.runs_root, args.run, args.videos)
    server = make_server(store, args.host, args.port, args.fps)
    log.info("serving %s on http://%s:%d (run=%s)", args.runs_root, args.host, args.port, store.cameras()["run"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
