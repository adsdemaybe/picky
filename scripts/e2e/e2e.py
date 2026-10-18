#!/usr/bin/env python3
"""Picky judges'-flow E2E harness (Playwright + httpx).

Drives the web app like a judge would (Home -> tap suggestion cards -> "Order these" -> live page -> approve checkout
-> receipt), records every gateway call the browser makes and every /ws event it receives, checks that the camera
streams on the live page are really updating, reads the MJPEG / snapshot endpoints directly (fps + resolution), and
checks the real SO-101 robot when asked. Writes report.md + report.json + screenshots + a video + a HAR to
runs/<timestamp>/.

Run:  ./run.sh            (or: uv run --no-project --with playwright==1.64.0 --with httpx==0.28.1 --with websockets==15.0.1 python e2e.py)
Env:  APP_URL, GATEWAY_URL, ROBOT_ID, EXPECT_REAL_ROBOT (0/1) and the knobs in Config.from_env below.
Exit: 0 when no check failed, 1 otherwise, 2 when the app / gateway is unreachable.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import re
import struct
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import websockets
from playwright.async_api import Error as PWError
from playwright.async_api import Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent

# /ws event types the judges' trip must produce (contracts/events.py)
REQUIRED_EVENTS = [
    "route.planned",
    "robot.state",
    "item.picked",
    "cart.updated",
    "approval.requested",
    "checkout.completed",
]
STATUSES = ("pass", "fail", "warn", "not_available", "gap", "skipped", "info")


# ------------------------------------------------------------------------------------------------ config


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or not v.strip() else v.strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_tri(name: str) -> bool | None:
    v = os.environ.get(name, "").strip().lower()
    if v in ("", "auto"):
        return None
    return v in ("1", "true", "yes", "on")


@dataclass
class Config:
    app_url: str
    gateway_url: str
    robot_id: str
    store_id: str
    user_id: str
    expect_real_robot: bool
    real_robot_id: str
    real_robot_pages: list[str]
    expect_isaac: bool | None  # None = auto (GET /api/sim/status connected)
    picks: int
    trip_timeout_s: float | None  # None = auto: 300 s on the 2D fake, 900 s with Isaac
    headed: bool
    slow_mo_ms: int
    viewport: tuple[int, int]
    mjpeg_seconds: float
    mjpeg_min_parts: int
    detour_answer: str
    runs_dir: Path
    har: bool
    skip_ui: bool
    verify_wrong: int  # pick photo cards answered "Wrong item" (the first N), the rest "Looks right"
    extras_answer: str  # checkout extras card: "none" (decline) or "first"

    @classmethod
    def from_env(cls) -> Config:
        vw, _, vh = _env("VIEWPORT", "1600x1000").partition("x")
        return cls(
            app_url=_env("APP_URL", "http://localhost:3001").rstrip("/"),
            gateway_url=_env("GATEWAY_URL", "http://localhost:8100").rstrip("/"),
            robot_id=_env("ROBOT_ID", "rover-ave-1"),
            store_id=_env("STORE_ID", "store_ave"),
            user_id=_env("USER_ID", "maya"),
            expect_real_robot=_env_bool("EXPECT_REAL_ROBOT", False),
            real_robot_id=_env("REAL_ROBOT_ID", "so101-real"),
            real_robot_pages=[p.strip() for p in _env("REAL_ROBOT_PAGES", "/,/live,/watch").split(",") if p.strip()],
            expect_isaac=_env_tri("EXPECT_ISAAC"),
            picks=int(_env("PICKS", "3")),
            trip_timeout_s=float(os.environ["TRIP_TIMEOUT_S"])
            if os.environ.get("TRIP_TIMEOUT_S", "").strip()
            else None,
            headed=_env_bool("HEADED", False),
            slow_mo_ms=int(_env("SLOW_MO_MS", "0")),
            viewport=(int(vw), int(vh or 1000)),
            mjpeg_seconds=float(_env("MJPEG_SECONDS", "5")),
            mjpeg_min_parts=int(_env("MJPEG_MIN_PARTS", "3")),
            detour_answer=_env("DETOUR_ANSWER", "no").lower(),
            runs_dir=Path(_env("RUNS_DIR", str(HERE.parent / "runs"))),
            har=_env_bool("HAR", True),
            skip_ui=_env_bool("SKIP_UI", False),
            verify_wrong=int(_env("VERIFY_WRONG", "1")),
            extras_answer=_env("EXTRAS_ANSWER", "none").lower(),
        )


# ------------------------------------------------------------------------------------------------ report


@dataclass
class Check:
    id: str
    group: str
    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert self.status in STATUSES, self.status


class Report:
    def __init__(self, cfg: Config, run_dir: Path) -> None:
        self.cfg = cfg
        self.run_dir = run_dir
        self.t0 = time.monotonic()
        self.started_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.checks: list[Check] = []
        self.timings: dict[str, float] = {}  # seconds since harness start
        self.screens: list[str] = []
        self.streams_http: list[dict[str, Any]] = []
        self.streams_page: list[dict[str, Any]] = []
        self.panels: list[str] = []
        self.approvals: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self.artifacts: dict[str, str] = {}

    def add(self, id: str, group: str, name: str, status: str, detail: str = "", **data: Any) -> Check:
        c = Check(id, group, name, status, detail, data)
        # one row per id: a later result replaces an earlier one
        self.checks = [x for x in self.checks if x.id != id] + [c]
        print(f"  [{status.upper():>13}] {name}{(' - ' + detail) if detail else ''}", flush=True)
        return c

    def mark(self, key: str) -> float:
        t = round(time.monotonic() - self.t0, 2)
        self.timings.setdefault(key, t)
        return t

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]


# ------------------------------------------------------------------------------------------------ network capture


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


_TEMPLATES = [
    (re.compile(r"^/api/products/[^/]+/image\.jpg$"), "/api/products/{sku}/image.jpg"),
    (re.compile(r"^/api/approvals/[^/]+$"), "/api/approvals/{approval_id}"),
    (re.compile(r"^/api/trips/[^/]+(/.*)?$"), "/api/trips/{trip_id}\\1"),
    (re.compile(r"^/api/requests/[^/]+$"), "/api/requests/{request_id}"),
    (re.compile(r"^/api/goals/[^/]+(/.*)?$"), "/api/goals/{goal_id}\\1"),
    (re.compile(r"^/api/plans/[^/]+(/.*)?$"), "/api/plans/{plan_id}\\1"),
]


def path_template(path: str) -> str:
    for rx, tpl in _TEMPLATES:
        m = rx.match(path)
        if m:
            return rx.sub(tpl, path) if "\\1" in tpl else tpl
    return path


class Net:
    """Records the browser's gateway calls (REST, images, MJPEG) and /ws traffic."""

    def __init__(self, app_url: str, gateway_url: str, t0: float) -> None:
        self.app_origin = _origin(app_url)
        self.gw_origin = _origin(gateway_url)
        self.t0 = t0
        self.calls: list[dict[str, Any]] = []
        self._by_req: dict[int, dict[str, Any]] = {}
        self.ws: list[dict[str, Any]] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.poses: list[tuple[float, str, float, float, str]] = []  # (t, robot_id, x, y, status) from robot.state
        self.events: list[dict[str, Any]] = []  # first-seen of each event type + approval ids
        self.first_event: dict[str, float] = {}
        self.tasks: list[asyncio.Task[Any]] = []
        # judges' flow: compact payloads of the events the unload / photo-check / payment checks read
        self.judges: list[dict[str, Any]] = []
        self._step_info: dict[str, dict[str, Any]] = {}
        self._last_basket: list[str] | None = None

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def is_gateway(self, url: str) -> bool:
        p = urlparse(url)
        if p.scheme not in ("http", "https", "ws", "wss") or p.path.startswith("/_next"):
            return False
        if _origin(url) == self.app_origin and p.path.startswith("/api/chat"):
            return False  # the Next.js assistant route, not the gateway
        return p.path == "/health" or p.path.startswith("/api/") or p.path == "/ws" or p.path.startswith("/ws/")

    def attach(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)
        page.on("websocket", self._on_ws)
        page.on(
            "console",
            lambda m: m.type == "error" and len(self.console_errors) < 200 and self.console_errors.append(m.text[:400]),
        )
        page.on("pageerror", lambda e: len(self.page_errors) < 100 and self.page_errors.append(str(e)[:400]))

    def _on_request(self, r: Any) -> None:
        if not self.is_gateway(r.url):
            return
        p = urlparse(r.url)
        rec: dict[str, Any] = {
            "method": r.method,
            "url": r.url,
            "origin": _origin(r.url),
            "path": p.path,
            "query": p.query,
            "template": path_template(p.path),
            "t": self.now(),
            "status": None,
            "ctype": None,
            "failure": None,
            "post": None,
            "json": None,
        }
        if r.method in ("POST", "PUT", "PATCH"):
            try:
                rec["post"] = r.post_data
            except Exception:  # noqa: BLE001 - binary body
                rec["post"] = None
        self.calls.append(rec)
        self._by_req[id(r)] = rec

    def _on_response(self, resp: Any) -> None:
        rec = self._by_req.get(id(resp.request))
        if rec is None:
            return
        rec["status"] = resp.status
        rec["ctype"] = resp.headers.get("content-type")
        rec["t_resp"] = self.now()
        wants_body = (rec["ctype"] or "").startswith("application/json") and (
            rec["template"] in ("/api/approvals/{approval_id}",)
            or rec["path"] == "/api/requests"
            or rec["path"].endswith("/suggestions")
        )
        if wants_body:
            self.tasks.append(asyncio.ensure_future(self._read_json(resp, rec)))

    async def _read_json(self, resp: Any, rec: dict[str, Any]) -> None:
        try:
            rec["json"] = await resp.json()
        except Exception as exc:  # noqa: BLE001
            rec["json_error"] = str(exc)[:200]

    def _on_failed(self, r: Any) -> None:
        rec = self._by_req.get(id(r))
        if rec is not None:
            rec["failure"] = r.failure

    def _on_ws(self, ws: Any) -> None:
        if not self.is_gateway(ws.url):
            return
        p = urlparse(ws.url)
        rec: dict[str, Any] = {
            "url": ws.url,
            "path": p.path,
            "params": {k: v for k, v in parse_qs(p.query).items()},
            "opened_t": self.now(),
            "closed_t": None,
            "frames": 0,
            "types": Counter(),
            "error": None,
        }
        self.ws.append(rec)

        def on_frame(payload: Any) -> None:
            if isinstance(payload, dict):  # older bindings pass {"payload": ...}
                payload = payload.get("payload")
            rec["frames"] += 1
            if isinstance(payload, (bytes, bytearray)):
                try:
                    payload = payload.decode()
                except UnicodeDecodeError:
                    return
            try:
                ev = json.loads(payload)
            except (TypeError, ValueError):
                return
            if not isinstance(ev, dict):
                return
            typ = str(ev.get("type", "?"))
            rec["types"][typ] += 1
            if rec["path"] == "/ws":
                if typ not in self.first_event:
                    self.first_event[typ] = self.now()
                if typ == "robot.state":
                    st = ev.get("state") or {}
                    pose = st.get("pose") or {}
                    if "x" in pose and "y" in pose:
                        self.poses.append(
                            (
                                self.now(),
                                str(st.get("robot_id")),
                                float(pose["x"]),
                                float(pose["y"]),
                                str(st.get("status")),
                            )
                        )
                elif typ in ("approval.requested", "approval.resolved", "checkout.completed"):
                    self.events.append({"t": self.now(), "type": typ, "summary": _event_summary(ev)})
                try:
                    self._judges_event(typ, ev)
                except Exception as exc:  # noqa: BLE001 - never break the capture
                    self.judges.append({"t": self.now(), "type": "harness.error", "error": f"{typ}: {exc}"[:200]})

        ws.on("framereceived", on_frame)
        ws.on("close", lambda *_: rec.__setitem__("closed_t", self.now()))
        ws.on("socketerror", lambda err: rec.__setitem__("error", str(err)[:200]))

    def _judges_event(self, typ: str, ev: dict[str, Any]) -> None:
        t = self.now()
        row: dict[str, Any] | None = None
        if typ == "robot.state":
            st = ev.get("state") or {}
            b = st.get("basket_skus")
            if isinstance(b, list) and b != self._last_basket:
                self._last_basket = list(b)
                row = {"robot_id": st.get("robot_id"), "basket": list(b)}
        elif typ == "step.started":
            st = ev.get("step") or {}
            tgt = st.get("target")
            sku = st.get("sku") or (tgt.get("sku") if isinstance(tgt, dict) else None)
            info = {
                "kind": st.get("kind"),
                "source": st.get("source"),
                "destination": st.get("destination"),
                "sku": sku,
                "group": st.get("group_id"),
            }
            self._step_info[str(st.get("step_id"))] = info
            if info["kind"] in ("grasp", "place", "handoff") or str(info["group"] or "").startswith("unload:"):
                row = {"step_id": st.get("step_id"), "tier": ev.get("tier"), "attempt": ev.get("attempt"), **info}
        elif typ == "step.completed":
            info = self._step_info.get(str(ev.get("step_id")))
            if info and (
                info["kind"] in ("grasp", "place", "handoff") or str(info["group"] or "").startswith("unload:")
            ):
                row = {"step_id": ev.get("step_id"), "status": ev.get("status"), **info}
        elif typ == "checkout.unload":
            row = {k: ev.get(k) for k in ("n", "total", "sku", "status", "counter_spot", "text", "step_id")}
        elif typ == "item.verification":
            v = ev.get("verification") or {}
            row = {
                k: v.get(k)
                for k in (
                    "verification_id",
                    "sku",
                    "status",
                    "decided_by",
                    "attempt",
                    "question",
                    "photo_url",
                    "camera_id",
                )
            }
        elif typ == "payment.agent_step":
            st = ev.get("step") or {}
            row = {k: st.get(k) for k in ("n", "name", "status", "title", "simulated", "label", "provider")}
        elif typ == "approval.requested":
            a = ev.get("approval") or ev.get("request") or {}
            row = {
                "kind": a.get("kind"),
                "approval_id": a.get("approval_id"),
                "sku": a.get("sku"),
                "title": str(a.get("title") or "")[:90],
            }
        elif typ == "approval.resolved":
            r = ev.get("response") or {}
            row = {"approval_id": r.get("approval_id"), "decision": r.get("decision"), "choice": r.get("choice")}
        elif typ in ("pick.choices", "checkout.completed", "item.picked"):
            row = {"sku": ev.get("sku") or (ev.get("item") or {}).get("sku")}
        elif typ == "trip.state_changed":
            row = {"from": ev.get("from_state"), "to": ev.get("to_state")}
        if row is not None:
            self.judges.append({"t": t, "type": typ, **row})

    def jfind(self, typ: str, **match: Any) -> list[dict[str, Any]]:
        return [e for e in self.judges if e["type"] == typ and all(e.get(k) == v for k, v in match.items())]

    # ---- queries
    def find(
        self, method: str | None = None, path: str | None = None, template: str | None = None, rx: str | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for c in self.calls:
            if method and c["method"] != method:
                continue
            if path and c["path"] != path:
                continue
            if template and c["template"] != template:
                continue
            if rx and not re.search(rx, c["path"]):
                continue
            out.append(c)
        return out

    def trip_sockets(self) -> list[dict[str, Any]]:
        return [w for w in self.ws if w["path"] == "/ws" and ("trip_id" in w["params"] or "request_id" in w["params"])]

    def ws_count(self, typ: str) -> int:
        return sum(w["types"][typ] for w in self.trip_sockets()) or sum(
            w["types"][typ] for w in self.ws if w["path"] == "/ws"
        )

    def robot_moved(self, t_from: float, t_to: float, robot_id: str | None) -> bool:
        pts = [
            (x, y)
            for (t, r, x, y, _s) in self.poses
            if t_from - 1.0 <= t <= t_to + 1.0 and (robot_id is None or r == robot_id)
        ]
        return any(abs(a[0] - b[0]) + abs(a[1] - b[1]) > 0.02 for a, b in zip(pts, pts[1:], strict=False))


def _event_summary(ev: dict[str, Any]) -> str:
    typ = ev.get("type")
    if typ == "approval.requested":
        a = ev.get("approval") or ev.get("request") or {}
        return f"{a.get('kind', '?')}: {str(a.get('title') or a.get('prompt') or a.get('question') or '')[:80]} ({a.get('approval_id', '')})"
    if typ == "approval.resolved":
        r = ev.get("response") or {}
        return f"{r.get('decision', '?')} ({r.get('approval_id', '')})"
    if typ == "checkout.completed":
        r = ev.get("receipt") or {}
        return f"{r.get('status', '?')} amount_cents={r.get('amount_cents', r.get('total_cents', '?'))}"
    return ""


# ------------------------------------------------------------------------------------------------ MJPEG / JPEG helpers


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, without decoding."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        i += 2 + length
    return None


async def read_mjpeg(client: httpx.AsyncClient, url: str, seconds: float, min_parts: int) -> dict[str, Any]:
    """Read an MJPEG stream for `seconds` (or until 3x min_parts frames) and parse the multipart JPEG parts."""
    res: dict[str, Any] = {
        "url": url,
        "status": None,
        "ctype": None,
        "parts": 0,
        "fps": None,
        "resolution": None,
        "first_frame_s": None,
        "kb_per_frame": None,
        "distinct": 0,
        "error": None,
    }
    t0 = time.monotonic()
    times: list[float] = []
    hashes: set[str] = set()
    sizes: list[int] = []
    try:
        async with client.stream("GET", url, timeout=httpx.Timeout(10.0, read=seconds + 5.0)) as r:
            res["status"] = r.status_code
            res["ctype"] = r.headers.get("content-type", "")
            if r.status_code != 200:
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > 400:
                        break
                res["error"] = body[:200].decode(errors="replace")
                return res
            m = re.search(r'boundary="?([^";]+)"?', res["ctype"] or "")
            boundary = ("--" + m.group(1)).encode() if m else None
            buf = b""

            def take(jpg: bytes) -> None:
                if jpg[:2] != b"\xff\xd8":
                    return
                times.append(time.monotonic() - t0)
                hashes.add(hashlib.sha1(jpg).hexdigest())
                sizes.append(len(jpg))
                if res["resolution"] is None:
                    wh = jpeg_size(jpg)
                    res["resolution"] = f"{wh[0]}x{wh[1]}" if wh else None

            async for chunk in r.aiter_bytes():
                buf += chunk
                while True:
                    if boundary is not None:
                        s = buf.find(boundary)
                        if s < 0:
                            buf = buf[-len(boundary) :]
                            break
                        h_end = buf.find(b"\r\n\r\n", s)
                        if h_end < 0:
                            buf = buf[s:]
                            break
                        headers = buf[s + len(boundary) : h_end].decode(errors="replace")
                        cl = re.search(r"content-length:\s*(\d+)", headers, re.I)
                        body_start = h_end + 4
                        if cl:
                            end = body_start + int(cl.group(1))
                            if len(buf) < end:
                                buf = buf[s:]
                                break
                            take(buf[body_start:end])
                            buf = buf[end:]
                        else:
                            nxt = buf.find(boundary, body_start)
                            if nxt < 0:
                                buf = buf[s:]
                                break
                            take(buf[body_start:nxt].rstrip(b"\r\n"))
                            buf = buf[nxt:]
                    else:  # no boundary: SOI ... EOI
                        s = buf.find(b"\xff\xd8")
                        e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                        if s < 0 or e < 0:
                            break
                        take(buf[s : e + 2])
                        buf = buf[e + 2 :]
                if time.monotonic() - t0 >= seconds or len(times) >= max(3 * min_parts, 30):
                    break
    except (httpx.HTTPError, OSError) as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:200]
    res["parts"] = len(times)
    res["distinct"] = len(hashes)
    if times:
        res["first_frame_s"] = round(times[0], 2)
        res["kb_per_frame"] = round(sum(sizes) / len(sizes) / 1024, 1)
    if len(times) >= 2 and times[-1] > times[0]:
        res["fps"] = round((len(times) - 1) / (times[-1] - times[0]), 2)
    res["read_s"] = round(time.monotonic() - t0, 2)
    return res


async def read_snapshot(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    res: dict[str, Any] = {
        "url": url,
        "status": None,
        "ctype": None,
        "resolution": None,
        "kb": None,
        "ms": None,
        "sha1": None,
        "error": None,
    }
    t = time.monotonic()
    try:
        r = await client.get(url, timeout=10.0)
        res["status"] = r.status_code
        res["ctype"] = r.headers.get("content-type", "")
        res["ms"] = round((time.monotonic() - t) * 1000)
        if r.status_code == 200:
            wh = jpeg_size(r.content)
            res["resolution"] = f"{wh[0]}x{wh[1]}" if wh else None
            res["kb"] = round(len(r.content) / 1024, 1)
            res["sha1"] = hashlib.sha1(r.content).hexdigest()
        else:
            res["error"] = r.text[:200]
    except (httpx.HTTPError, OSError) as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return res


PLACEHOLDER_PREFIX = "synthetic-offline"  # arms/perception/cameras.py: the gateway's "camera offline" card


async def ws_frames(gateway_url: str, cam: str, seconds: float = 4.0) -> dict[str, Any]:
    """Read camera.frame events from WS /ws/cameras/{cam}: the frame ids tell real frames from the gateway's offline
    placeholder card (frame_id 'synthetic-offline-...'), which MJPEG / snapshot serve with a normal 200 image/jpeg."""
    url = gateway_url.replace("http", "ws", 1) + f"/ws/cameras/{cam}?fps=3&w=160"
    res: dict[str, Any] = {"frames": 0, "placeholder": 0, "frame_ids": [], "size": None, "error": None}
    t0 = time.monotonic()
    try:
        async with websockets.connect(url, open_timeout=6, max_size=8 * 1024 * 1024) as ws:
            while time.monotonic() - t0 < seconds and res["frames"] < 6:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, seconds - (time.monotonic() - t0)))
                except TimeoutError:
                    break
                try:
                    ev = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                fr = ev.get("frame") if isinstance(ev, dict) and ev.get("type") == "camera.frame" else None
                if not isinstance(fr, dict):
                    continue
                res["frames"] += 1
                fid = str(fr.get("frame_id", ""))
                res["placeholder"] += fid.startswith(PLACEHOLDER_PREFIX)
                if len(res["frame_ids"]) < 3:
                    res["frame_ids"].append(fid)
                res["size"] = f"{fr.get('w')}x{fr.get('h')}"
    except Exception as exc:  # noqa: BLE001 - 404 / refused / closed: reported, not fatal
        res["error"] = f"{type(exc).__name__}: {exc}"[:160]
    return res


# ------------------------------------------------------------------------------------------------ page helpers

JS_CAMERA_IMGS = """() => [...document.querySelectorAll('img')]
  .map((i, idx) => ({idx, src: i.currentSrc || i.src || '', alt: i.alt || '', nw: i.naturalWidth, nh: i.naturalHeight,
                     complete: i.complete, w: Math.round(i.getBoundingClientRect().width), h: Math.round(i.getBoundingClientRect().height)}))
  .filter(x => x.src.includes('/api/cameras/'))"""

JS_SAMPLE_EL = """(el) => {
  try {
    if (!el.naturalWidth) return {ok: false, err: 'no frame (naturalWidth 0)'};
    const c = document.createElement('canvas'); c.width = 96; c.height = 54;
    const g = c.getContext('2d'); g.drawImage(el, 0, 0, 96, 54);
    const d = g.getImageData(0, 0, 96, 54).data; const gray = [];
    for (let i = 0; i < d.length; i += 4) gray.push((d[i] * 3 + d[i + 1] * 6 + d[i + 2]) / 10 | 0);
    const full = document.createElement('canvas'); full.width = el.naturalWidth; full.height = el.naturalHeight;
    const fg = full.getContext('2d'); fg.drawImage(el, 0, 0); const fd = fg.getImageData(0, 0, full.width, full.height).data;
    let h = 2166136261 >>> 0; for (let i = 0; i < fd.length; i += 3) { h ^= fd[i]; h = Math.imul(h, 16777619) >>> 0; }
    return {ok: true, gray, hash: String(h)};
  } catch (e) { return {ok: false, err: String(e)}; }
}"""

JS_SAMPLE_PNG = """async (b64) => {
  const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const c = document.createElement('canvas'); c.width = 96; c.height = 54;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0, 96, 54);
  const d = g.getImageData(0, 0, 96, 54).data; const gray = [];
  for (let i = 0; i < d.length; i += 4) gray.push((d[i] * 3 + d[i + 1] * 6 + d[i + 2]) / 10 | 0);
  return gray;
}"""

JS_PANELS = """() => [...document.querySelectorAll('section[aria-label]')]
  .filter(s => s.querySelector('img, canvas, svg'))
  .map(s => s.getAttribute('aria-label'))"""


def camera_of(src: str) -> tuple[str | None, str | None]:
    m = re.search(r"/api/cameras/([^/?#]+)/(stream\.mjpg|snapshot\.jpg)", src)
    if not m:
        return None, None
    return m.group(1), "stream" if m.group(2).startswith("stream") else "snapshot"


async def sample_img(page: Page, loc: Locator) -> dict[str, Any]:
    """Pixel sample of a camera <img>: draw it into a canvas (same-origin) or, when the canvas is tainted
    (cross-origin gateway without CORS-enabled <img>), an element screenshot with sibling overlays masked."""
    r = await loc.evaluate(JS_SAMPLE_EL)
    if r.get("ok"):
        return {"method": "canvas", "gray": r["gray"], "hash": r["hash"]}
    siblings = loc.locator("xpath=preceding-sibling::* | following-sibling::*")
    png = await loc.screenshot(mask=[siblings], animations="disabled", caret="hide", timeout=5000)
    gray = await page.evaluate(JS_SAMPLE_PNG, base64.b64encode(png).decode())
    return {"method": "screenshot", "gray": gray, "hash": hashlib.sha1(png).hexdigest(), "canvas_error": r.get("err")}


def gray_diff(a: list[int], b: list[int]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / (255.0 * len(a))


# ------------------------------------------------------------------------------------------------ the run


class Run:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = cfg.runs_dir / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rep = Report(cfg, self.dir)
        self.net = Net(cfg.app_url, cfg.gateway_url, self.rep.t0)
        self.shot_no = 0
        self.isaac_expected = False
        self.gw_cameras: dict[str, dict[str, Any]] = {}
        self.fleet: list[dict[str, Any]] = []
        self.trip_id: str | None = None
        self.request_id: str | None = None
        self.assigned_robot: str | None = None
        self.t_order: float | None = None
        self.t_live: float | None = None
        self._probes: dict[str, str] = {}
        self.unload_texts: list[str] = []
        self.verify_answers: list[dict[str, Any]] = []
        self._unload_shots: set[str] = set()

    # ---- utilities
    async def shot(self, page: Page, name: str, full: bool = False) -> None:
        self.shot_no += 1
        fn = f"{self.shot_no:02d}_{name}.png"
        try:
            await page.screenshot(path=str(self.dir / fn), full_page=full, animations="disabled", timeout=10000)
            self.rep.screens.append(fn)
        except PWError as exc:
            print(f"  (screenshot {fn} failed: {exc})")

    # ---- 0. preflight
    async def preflight(self, client: httpx.AsyncClient) -> bool:
        rep, cfg = self.rep, self.cfg
        print(f"\nPreflight  app={cfg.app_url}  gateway={cfg.gateway_url}")
        try:
            r = await client.get(cfg.gateway_url + "/health", timeout=8.0)
            h = r.json()
            rep.context["health"] = h
            rep.add(
                "pre.gateway",
                "preflight",
                "Gateway reachable (GET /health)",
                "pass" if r.status_code == 200 else "fail",
                f"{r.status_code}, contracts {h.get('contracts_version')}, sim_connected={h.get('sim_connected')}, real_arms={h.get('real_arms')}",
            )
        except Exception as exc:  # noqa: BLE001
            rep.add(
                "pre.gateway",
                "preflight",
                "Gateway reachable (GET /health)",
                "fail",
                f"{type(exc).__name__}: {exc} (is the ssh tunnel up?)",
            )
            return False
        try:
            r = await client.get(cfg.app_url + "/", timeout=60.0)  # first Next dev compile can be slow
            rep.add(
                "pre.app",
                "preflight",
                "Web app reachable",
                "pass" if r.status_code == 200 else "fail",
                f"{r.status_code}",
            )
            if r.status_code != 200:
                return False
        except Exception as exc:  # noqa: BLE001
            rep.add("pre.app", "preflight", "Web app reachable", "fail", f"{type(exc).__name__}: {exc}")
            return False
        try:
            sim = (await client.get(cfg.gateway_url + "/api/sim/status", timeout=8.0)).json()
            rep.context["sim_status"] = sim
        except Exception as exc:  # noqa: BLE001
            sim = {}
            rep.context["sim_status_error"] = str(exc)
        self.isaac_expected = cfg.expect_isaac if cfg.expect_isaac is not None else bool(sim.get("isaac_robot_ids"))
        rep.add(
            "pre.sim",
            "preflight",
            "Sim backend",
            "info",
            f"/ws/sim workers {[w.get('worker_id') for w in sim.get('workers', [])] or 'none'}; Isaac drives {sim.get('isaac_robot_ids') or 'no robot'}, "
            f"2D fake drives {sim.get('fake_robot_ids')}; director/perceived streams {'REQUIRED' if self.isaac_expected else 'optional (2D fake)'}",
        )
        try:
            cams = (await client.get(cfg.gateway_url + "/api/cameras", timeout=8.0)).json()
            self.gw_cameras = {c["camera_id"]: c for c in cams}
            rep.context["cameras"] = sorted(self.gw_cameras)
        except Exception as exc:  # noqa: BLE001
            rep.context["cameras_error"] = str(exc)
        try:
            self.fleet = (await client.get(cfg.gateway_url + "/api/fleet/robots", timeout=8.0)).json()
            rep.context["robots"] = [
                {k: r.get(k) for k in ("robot_id", "name", "kind", "store_id", "status", "connected", "backend")}
                for r in self.fleet
            ]
        except Exception as exc:  # noqa: BLE001
            rep.context["robots_error"] = str(exc)
        return True

    # ---- 1-3. home -> pick -> order
    async def home_and_order(self, page: Page) -> bool:
        rep, cfg = self.rep, self.cfg
        print("\nUI: Home -> suggestion cards -> Order these")
        await page.goto(cfg.app_url + "/", wait_until="domcontentloaded", timeout=60000)
        rep.mark("home_loaded")
        await self.shot(page, "home")
        region = page.get_by_role("region", name=re.compile(r"running low", re.I))
        cards = region.locator("button[aria-pressed]")
        try:
            await cards.first.wait_for(state="visible", timeout=25000)
        except PWTimeout:
            cards = page.locator("button[aria-pressed]")
            try:
                await cards.first.wait_for(state="visible", timeout=5000)
            except PWTimeout:
                rep.add(
                    "ui.cards",
                    "ui",
                    "Suggestion cards render on Home",
                    "fail",
                    "no card (button[aria-pressed]) in 30 s",
                )
                await self.shot(page, "no_cards")
                return False
        rep.mark("cards_visible")
        await asyncio.sleep(1.5)  # card images
        n = await cards.count()
        demo = await page.get_by_text("Demo history", exact=True).count() > 0
        rep.add(
            "ui.cards",
            "ui",
            "Suggestion cards render on Home",
            "pass",
            f"{n} cards, source={'DEMO fallback' if demo else 'gateway'}",
            cards=n,
            demo=demo,
        )
        if demo:
            rep.add(
                "ui.cards_source",
                "ui",
                "Cards come from the gateway (not the demo fallback)",
                "fail",
                "the app shows 'Demo history': GET /api/users/{id}/suggestions failed or is missing",
            )
        else:
            rep.add("ui.cards_source", "ui", "Cards come from the gateway (not the demo fallback)", "pass")
        broken = await page.evaluate("""() => [...document.querySelectorAll('img')].filter(i => (i.currentSrc||i.src).includes('/image.jpg'))
            .map(i => ({src: i.currentSrc||i.src, ok: i.complete && i.naturalWidth > 0}))""")
        if broken:
            bad = [b["src"] for b in broken if not b["ok"]]
            rep.add(
                "ui.card_images",
                "ui",
                "Card product photos load",
                "pass" if not bad else "fail",
                f"{len(broken) - len(bad)}/{len(broken)} loaded",
                broken=bad[:10],
            )
        else:
            rep.add(
                "ui.card_images",
                "ui",
                "Card product photos load",
                "fail" if not demo else "warn",
                "no <img .../image.jpg> on the cards (tiles drawn instead: image 404 / no image_url)",
            )

        # in-stock cards first (an 'Online' badge = not in the store)
        order: list[int] = []
        online: list[int] = []
        for i in range(n):
            (online if await cards.nth(i).get_by_text("Online", exact=True).count() else order).append(i)
        chosen = (order + online)[: max(1, cfg.picks)]
        names = []
        for i in chosen:
            c = cards.nth(i)
            await c.scroll_into_view_if_needed()
            await c.click()
            names.append((await c.get_attribute("aria-label") or "?").split(",")[0])
            await asyncio.sleep(0.25)
        pressed = [await cards.nth(i).get_attribute("aria-pressed") for i in chosen]
        rep.add(
            "ui.pick",
            "ui",
            f"Tap {len(chosen)} cards",
            "pass" if all(p == "true" for p in pressed) else "fail",
            "; ".join(names),
            picked=names,
        )
        await self.shot(page, "picked")

        btn = page.get_by_role("button", name=re.compile(r"order these", re.I))
        try:
            await btn.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            rep.add("ui.order_button", "ui", "'Order these' button", "fail", "not found")
            return False
        label = (await btn.inner_text()).strip().replace("\n", " ")
        self.t_order = rep.mark("order_clicked")
        try:
            async with page.expect_response(
                lambda r: urlparse(r.url).path.rstrip("/") == "/api/requests" and r.request.method == "POST",
                timeout=45000,
            ) as info:
                await btn.click()
            resp = await info.value
            rep.mark("request_answered")
            try:
                plan = await resp.json()
                self.assigned_robot = next(
                    (g.get("robot_id") for g in plan.get("goals", []) if g.get("robot_id")), None
                )
            except Exception:  # noqa: BLE001 - checked again in api_checks
                pass
            rep.add(
                "ui.order",
                "ui",
                "'Order these' sends the order",
                "pass",
                f"clicked '{label}' -> POST /api/requests {resp.status}",
            )
        except PWTimeout:
            rep.add(
                "ui.order",
                "ui",
                "'Order these' sends the order",
                "fail",
                f"clicked '{label}' but no POST /api/requests in 45 s (app not in gateway mode? NEXT_PUBLIC_GATEWAY_URL / GET /health from the browser)",
            )
        try:
            await page.wait_for_url(re.compile(r"/(watch|live|trip)(\?|/|$)"), timeout=30000)
            self.t_live = rep.mark("live_page")
            q = parse_qs(urlparse(page.url).query)
            self.trip_id = (q.get("trip") or [None])[0]
            self.request_id = (q.get("request") or [None])[0]
            rep.add(
                "ui.live_page",
                "ui",
                "Lands on the live page",
                "pass",
                urlparse(page.url).path
                + (f" trip={self.trip_id}" if self.trip_id else " (no trip id in URL: demo replay?)"),
            )
        except PWTimeout:
            rep.add("ui.live_page", "ui", "Lands on the live page", "fail", f"still on {page.url}")
            await self.shot(page, "no_live_page")
            return False
        await asyncio.sleep(1.0)
        await self.shot(page, "live")
        return True

    # ---- 4. live page: progress, approvals, receipt (+ stream checks mid-trip)
    async def live(self, page: Page) -> None:
        rep, cfg, net = self.rep, self.cfg, self.net
        print("\nUI: live page -> approvals -> receipt")
        timeout_s = cfg.trip_timeout_s or (900.0 if self.isaac_expected else 300.0)
        deadline = time.monotonic() + timeout_s
        t_land = time.monotonic()
        last_periodic = time.monotonic()
        streams_done = streams_ok = streams_retried = False
        t_streams = 0.0
        seen_route = seen_pick = False
        last_answer = 0.0
        outcome = None
        while time.monotonic() < deadline:
            if await page.get_by_role("heading", name=re.compile(r"^\s*Paid\b")).count():
                outcome = "receipt"
                rep.mark("receipt")
                await asyncio.sleep(1.5)  # confetti settles
                await self.shot(page, "receipt")
                break
            if await page.get_by_text("The trip stopped.").count():
                outcome = "stopped"
                rep.mark("trip_stopped")
                await self.shot(page, "trip_stopped")
                break
            if not seen_route and net.ws_count("route.planned"):
                seen_route = True
                rep.mark("route_planned")
                await asyncio.sleep(0.8)
                await self.shot(page, "route_planned")
            if not seen_pick and net.ws_count("item.picked"):
                seen_pick = True
                rep.mark("first_item_picked")
                await self.shot(page, "first_pick")
            dialog = page.get_by_role("alertdialog")
            if time.monotonic() - last_answer > 1.5 and await dialog.count() and await dialog.first.is_visible():
                await self.answer(page, dialog.first)
                last_answer = time.monotonic()
                continue
            if time.monotonic() - last_answer > 1.2 and await self.answer_check(page):
                last_answer = time.monotonic()
                continue
            await self.watch_unload(page)
            moving = net.ws_count("robot.state") > 0 or seen_route
            if not streams_done and moving and time.monotonic() - t_land > 6:
                streams_done = True
                streams_ok = await self.page_streams(page)
                t_streams = time.monotonic()
            elif streams_done and not streams_ok and not streams_retried and time.monotonic() - t_streams > 25:
                streams_retried = True  # first sample looked frozen (robot idle?): sample again mid-trip
                streams_ok = await self.page_streams(page)
            if time.monotonic() - last_periodic > 30:
                last_periodic = time.monotonic()
                await self.shot(page, "progress")
            await asyncio.sleep(0.5)
        if outcome is None:
            await self.shot(page, "timeout")
        if not streams_done:
            await self.page_streams(page)
        if outcome == "receipt":
            rep.add(
                "ui.receipt",
                "ui",
                "Receipt shown after approving checkout",
                "pass",
                f"after {rep.timings['receipt'] - (self.t_order or 0):.1f} s from the order click",
            )
        elif outcome == "stopped":
            rep.add(
                "ui.receipt",
                "ui",
                "Receipt shown after approving checkout",
                "fail",
                "the app shows 'The trip stopped.'",
            )
        else:
            rep.add(
                "ui.receipt",
                "ui",
                "Receipt shown after approving checkout",
                "fail",
                f"no receipt within {timeout_s:.0f} s",
            )
        checkout = [a for a in rep.approvals if a["kind"] == "checkout"]
        rep.add(
            "ui.checkout_approval",
            "ui",
            "Checkout approval card shown and approved",
            "pass" if checkout else "fail",
            f"{len(rep.approvals)} card(s) answered: " + ", ".join(f"{a['kind']}->{a['answer']}" for a in rep.approvals)
            if rep.approvals
            else "no approval card appeared",
        )
        try:
            rep.panels = await page.evaluate(JS_PANELS)
        except PWError:
            pass

    async def answer_check(self, page: Page) -> bool:
        """The photo card of a pick ('Check each pick'): the first VERIFY_WRONG cards -> 'Wrong item', then 'Looks right'."""
        panel = page.get_by_role("region", name=re.compile(r"purchase checks", re.I))
        if not await panel.count():
            return False
        ok_btn = panel.first.get_by_role("button", name=re.compile(r"looks right", re.I))
        bad_btn = panel.first.get_by_role("button", name=re.compile(r"wrong item", re.I))
        if not await ok_btn.count() or not await ok_btn.first.is_visible():
            return False
        title = ""
        try:
            title = re.sub(r"\s+", " ", await panel.first.locator("h3").first.inner_text(timeout=1000)).strip()[:60]
        except PWError:
            pass
        wrong = sum(1 for a in self.verify_answers if a["answer"] == "wrong") < self.cfg.verify_wrong
        await self.shot(page, "photo_check")
        try:
            await (bad_btn if wrong else ok_btn).first.click(timeout=3000)
        except PWError:
            return False
        self.verify_answers.append({"t": self.net.now(), "item": title, "answer": "wrong" if wrong else "correct"})
        self.rep.approvals.append(
            {"t": self.net.now(), "kind": "verify", "answer": "wrong" if wrong else "correct", "card": title}
        )
        print(f"  answered photo check -> {'WRONG' if wrong else 'correct'}: {title}")
        return True

    async def watch_unload(self, page: Page) -> None:
        """Checkout unload: keep the panel's 'Unloading n/N: ...' lines and screenshot the first, a middle and the last."""
        loc = page.get_by_test_id("unload-text")
        try:
            if await loc.count():
                txt = re.sub(r"\s+", " ", await loc.first.inner_text(timeout=500)).strip()
                if txt and (not self.unload_texts or self.unload_texts[-1] != txt):
                    self.unload_texts.append(txt)
        except PWError:
            pass
        ev = self.net.jfind("checkout.unload")
        if not ev:
            return
        total = max(int(ev[-1].get("total") or 0), 1)
        for key, cond in (
            ("unload_first", any(e["status"] == "started" for e in ev)),
            (
                "unload_middle",
                any(e["status"] in ("placed", "verified") and int(e.get("n") or 0) >= (total + 1) // 2 for e in ev),
            ),
            ("unload_done", any(e["status"] == "done" for e in ev)),
        ):
            if cond and key not in self._unload_shots:
                self._unload_shots.add(key)
                await asyncio.sleep(0.6)
                await self.shot(page, key)

    async def answer(self, page: Page, d: Locator) -> None:
        rep, cfg = self.rep, self.cfg
        text = re.sub(r"\s+", " ", (await d.inner_text()))[:160]

        async def click(rx: str) -> str | None:
            b = d.get_by_role("button", name=re.compile(rx, re.I))
            if await b.count() and await b.first.is_visible() and await b.first.is_enabled():
                lbl = re.sub(r"\s+", " ", await b.first.inner_text()).strip()[:60]
                await b.first.click()
                return lbl
            return None

        kind = "question"
        await self.shot(page, "approval")
        if await d.get_by_role("button", name=re.compile(r"approve and pay", re.I)).count():
            kind = "checkout"
            rep.mark("checkout_approval_shown")
            ans = await click(r"approve and pay")
            rep.mark("checkout_approved")
        elif await d.get_by_role("button", name=re.compile(r"no thanks|yes, add it", re.I)).count():
            kind = "detour"
            ans = await click(r"^no thanks$" if cfg.detour_answer == "no" else r"yes, add it")
        elif await d.get_by_role("button", name=re.compile(r"^yes$", re.I)).count():
            ans = await click(r"^yes$")
        else:
            kind = "choice"
            ans = None
            if cfg.extras_answer == "none":  # extras offered at checkout: decline when a "none" option exists
                ans = await click(r"^(no thanks|none|none of these|nothing|skip|not now|no)\b")
            if ans is None:
                b = d.get_by_role("button")
                if await b.count():
                    ans = re.sub(r"\s+", " ", await b.first.inner_text()).strip()[:60] or "(first option)"
                    await b.first.click()
        rep.approvals.append({"t": self.net.now(), "kind": kind, "answer": ans, "card": text})
        print(f"  answered {kind} card -> {ans!r}: {text[:90]}")

    # ---- 5a. streams as the page shows them
    async def page_streams(self, page: Page) -> bool:
        """Checks every camera <img> on the page; True when all of them have frames and update."""
        rep = self.rep
        self.rep.streams_page = []
        print("\nStreams on the live page")
        imgs: list[dict[str, Any]] = []
        end = time.monotonic() + 15
        while time.monotonic() < end:
            imgs = await page.evaluate(JS_CAMERA_IMGS)
            if any(i["nw"] > 0 for i in imgs):
                break
            await asyncio.sleep(1)
        try:
            rep.panels = await page.evaluate(JS_PANELS)
        except PWError:
            pass
        if not imgs:
            rep.add(
                "page.streams",
                "streams",
                "Live page shows gateway camera images",
                "fail",
                "no <img src=.../api/cameras/...> on the page (replay mode or all panels fell back)",
                panels=rep.panels,
            )
            return False
        rep.add(
            "page.streams",
            "streams",
            "Live page shows gateway camera images",
            "pass",
            f"{len(imgs)} camera <img>; panels on air: {rep.panels}",
        )
        # the panel badges must not call a 2D-fake picture "Isaac Sim" (StageCamera promises a simulation is never
        # passed off as something else); the gateway's camera listing says who renders each camera
        shown = sorted({c for c in (camera_of(i["src"])[0] for i in imgs) if c})
        sources = {c: (self.gw_cameras.get(c) or {}).get("source") for c in shown}
        claims_isaac = [p for p in rep.panels if "isaac" in p.lower()]
        if claims_isaac and sources and not any(v == "isaac" for v in sources.values()):
            rep.add(
                "page.label_honest",
                "streams",
                "Camera badges match the real source",
                "fail",
                f"panels say {claims_isaac} but GET /api/cameras says {sources} (the app trusts /health sim_connected, which is also true "
                "when only the SO-101 bridge is on /ws/sim)",
            )
        else:
            rep.add("page.label_honest", "streams", "Camera badges match the real source", "pass", f"sources {sources}")
        all_ok = True
        loc_all = page.locator('img[src*="/api/cameras/"]')
        count = await loc_all.count()
        t_a = self.net.now()
        first: list[dict[str, Any] | None] = []
        for k in range(count):
            try:
                first.append(await sample_img(page, loc_all.nth(k)))
            except PWError as exc:
                first.append({"error": str(exc)[:160]})
        await asyncio.sleep(2.2)
        imgs2 = await page.evaluate(JS_CAMERA_IMGS)
        t_b = self.net.now()
        for k in range(min(count, len(imgs2))):
            info = imgs2[k]
            cam, kind = camera_of(info["src"])
            row: dict[str, Any] = {
                "camera": cam,
                "kind": kind,
                "alt": info["alt"][:60],
                "natural": f"{info['nw']}x{info['nh']}",
                "shown": f"{info['w']}x{info['h']}",
            }
            try:
                b = await sample_img(page, loc_all.nth(k))
            except PWError as exc:
                b = {"error": str(exc)[:160]}
            a = first[k] if k < len(first) else None
            if a and "gray" in a and "gray" in b:
                row["method"] = b["method"]
                row["changed"] = a["hash"] != b["hash"]
                row["diff_pct"] = round(100 * gray_diff(a["gray"], b["gray"]), 3)
            else:
                row["changed"] = None
                row["error"] = (a or {}).get("error") or b.get("error")
            rep.streams_page.append(row)
            cid = f"page.img.{cam}.{kind}"
            name = f"Page <img> {cam} ({kind})"
            if info["nw"] <= 0:
                rep.add(cid + ".frame", "streams", name + " has a frame", "fail", "naturalWidth 0")
                all_ok = False
                continue
            rep.add(
                cid + ".frame",
                "streams",
                name + " has a frame",
                "pass",
                f"natural {row['natural']}, shown {row['shown']}",
            )
            moved = self.net.robot_moved(t_a, t_b, cam.split(".")[0] if cam else None)
            if row.get("changed"):
                rep.add(
                    cid + ".updating",
                    "streams",
                    name + " updates (2 samples 2.2 s apart)",
                    "pass",
                    f"diff {row['diff_pct']}% via {row['method']}",
                )
            elif row.get("changed") is False:
                all_ok = False
                rep.add(
                    cid + ".updating",
                    "streams",
                    name + " updates (2 samples 2.2 s apart)",
                    "fail" if moved else "warn",
                    "identical frames"
                    + (
                        " while robot.state shows the robot moving"
                        if moved
                        else " (robot not moving: static scene or frozen stream)"
                    ),
                )
            else:
                all_ok = False
                rep.add(
                    cid + ".updating",
                    "streams",
                    name + " updates (2 samples 2.2 s apart)",
                    "warn",
                    f"could not sample: {row.get('error')}",
                )
        return all_ok

    # ---- 2. API assertions from the browser traffic
    async def probe(self, client: httpx.AsyncClient, c: dict[str, Any]) -> str:
        """The browser hides the status of a blocked cross-origin <img> response (net::ERR_BLOCKED_BY_ORB = the gateway
        answered with a non-image, e.g. a 404 JSON): fetch the same URL directly and say what the gateway returns."""
        key = c["method"] + " " + c["path"]
        if key in self._probes:
            return self._probes[key]
        try:
            async with client.stream("GET", c["url"], timeout=httpx.Timeout(8.0, read=4.0)) as r:
                body = b""
                if not r.headers.get("content-type", "").startswith(("multipart/", "image/")):
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > 300:
                            break
                out = f"{r.status_code} {r.headers.get('content-type', '')}" + (
                    f" {body[:90].decode(errors='replace')}" if body else ""
                )
        except (httpx.HTTPError, OSError) as exc:
            out = f"{type(exc).__name__}"
        self._probes[key] = out
        return out

    async def api_checks(self, client: httpx.AsyncClient) -> None:
        rep, cfg, net = self.rep, self.cfg, self.net
        for c in net.calls:  # fill in what the browser could not see
            if c["status"] is None and c["failure"]:
                c["direct"] = await self.probe(client, c)
                m = re.match(r"(\d{3}) ?([^ ]*)", c["direct"])
                if m:
                    c["status_direct"] = int(m.group(1))
        print("\nAPI calls made by the app")
        origins = sorted({c["origin"] for c in net.calls})
        if origins and _origin(cfg.gateway_url) not in origins:
            rep.add(
                "api.origin",
                "api",
                "App talks to GATEWAY_URL",
                "warn",
                f"app called {origins}, harness GATEWAY_URL={cfg.gateway_url} (NEXT_PUBLIC_GATEWAY_URL differs)",
            )
        elif origins:
            rep.add("api.origin", "api", "App talks to GATEWAY_URL", "pass", ", ".join(origins))

        def ok(c: dict[str, Any], codes: tuple[int, ...] = (200,)) -> bool:
            return c.get("status") in codes

        # GET /health
        hs = net.find("GET", "/health")
        rep.add(
            "api.health",
            "api",
            "GET /health",
            "pass" if any(ok(c) for c in hs) else "fail",
            f"{len(hs)} calls, statuses {sorted({c['status'] for c in hs}, key=str)}",
        )

        # suggestions
        path = f"/api/users/{cfg.user_id}/suggestions"
        ss = net.find("GET", path)
        good = [c for c in ss if ok(c) and isinstance(c.get("json"), list) and len(c["json"]) >= 1]
        if good:
            rep.add("api.suggestions", "api", f"GET {path} (200, >=1 card)", "pass", f"{len(good[0]['json'])} cards")
        else:
            rep.add(
                "api.suggestions",
                "api",
                f"GET {path} (200, >=1 card)",
                "fail",
                f"{len(ss)} calls, statuses {[c['status'] for c in ss]}"
                + (f", body {str(ss[0].get('json'))[:80]}" if ss else " (never called)"),
            )

        # product images
        ims = net.find("GET", template="/api/products/{sku}/image.jpg")
        good_i = [c for c in ims if ok(c) and (c.get("ctype") or "").startswith("image/jpeg")]
        rep.add(
            "api.product_image",
            "api",
            "GET /api/products/<sku>/image.jpg (200 image/jpeg)",
            "pass" if good_i else "fail",
            f"{len(good_i)}/{len(ims)} ok"
            + (
                f"; e.g. {ims[0]['path']} -> {_seen(ims[0])}"
                if ims and not good_i
                else " (never requested)"
                if not ims
                else ""
            ),
        )

        # POST /api/requests with items
        posts = net.find("POST", "/api/requests")
        best = None
        for c in posts:
            try:
                body = json.loads(c.get("post") or "{}")
            except ValueError:
                body = {}
            c["_items"] = body.get("items") or []
            j = c.get("json") or {}
            c["_trip"] = (j.get("trip_ids") or [None])[0] if isinstance(j, dict) else None
            if c["_items"] and ok(c, (200, 201)) and c["_trip"]:
                best = c
        if best:
            j = best["json"]
            robot = next((g.get("robot_id") for g in j.get("goals", []) if g.get("robot_id")), None)
            self.assigned_robot = self.assigned_robot or robot
            self.trip_id = self.trip_id or best["_trip"]
            if robot and robot != cfg.robot_id:
                rep.add(
                    "api.robot_assigned",
                    "api",
                    "Fleet assigned ROBOT_ID",
                    "info",
                    f"fleet picked {robot}, not {cfg.robot_id}; streams checked for both",
                )
            rep.add(
                "api.create_request",
                "api",
                "POST /api/requests with items (2xx + trip id)",
                "pass",
                f"{len(best['_items'])} items -> {best['status']}, request {j.get('request_id')}, trip {best['_trip']}, robot {robot}, status {j.get('status')}",
            )
        else:
            why = (
                "never called"
                if not posts
                else "; ".join(
                    f"{c['status']} items={len(c['_items'])} trip={c['_trip']} {str(c.get('json'))[:100]}"
                    for c in posts
                )
            )
            rep.add("api.create_request", "api", "POST /api/requests with items (2xx + trip id)", "fail", why)
        if len(posts) > 1:
            rep.add(
                "api.create_request_retry",
                "api",
                "Single POST /api/requests (no retry without items)",
                "warn",
                f"{len(posts)} POSTs (the app retries without items after a 422)",
            )

        # WS /ws?trip_id=
        socks = net.trip_sockets()
        if socks:
            w = socks[0]
            total = sum(s["frames"] for s in socks)
            rep.add(
                "api.ws_trip",
                "api",
                "WS /ws?trip_id=... opened and receiving events",
                "pass" if total else "fail",
                f"{len(socks)} socket(s), {total} frames, first {w['url'].split('?')[1][:90]}",
            )
        else:
            other = [w["url"] for w in net.ws]
            rep.add(
                "api.ws_trip",
                "api",
                "WS /ws?trip_id=... opened and receiving events",
                "fail",
                f"no trip socket; sockets seen: {other[:4]}",
            )
        for typ in REQUIRED_EVENTS:
            n = net.ws_count(typ)
            rep.add(f"api.ws_event.{typ}", "api", f"/ws event {typ}", "pass" if n else "fail", f"{n}")

        # approvals
        aps = net.find("POST", template="/api/approvals/{approval_id}")
        rep.add(
            "api.approval_post",
            "api",
            "POST /api/approvals/{id} (200)",
            "pass" if aps and all(ok(c) for c in aps) else "fail",
            f"{len(aps)} calls, statuses {[c['status'] for c in aps]}" + ("" if aps else " (never called)"),
        )

        # director + perceived MJPEG requested by the page
        robot = self.assigned_robot or cfg.robot_id
        for kind, cam in (("director", f"{robot}.director"), ("perceived", f"{robot}.front.perceived")):
            calls = net.find("GET", f"/api/cameras/{cam}/stream.mjpg")
            name = f"GET /api/cameras/{cam}/stream.mjpg (200 multipart/x-mixed-replace)"
            good = [c for c in calls if ok(c) and (c.get("ctype") or "").startswith("multipart/x-mixed-replace")]
            if good:
                rep.add(f"api.stream.{kind}", "api", name, "pass", f"{len(calls)} request(s), {good[0]['ctype']}")
            elif not calls:
                rep.add(f"api.stream.{kind}", "api", name, "fail", "the page never requested it")
            else:
                st = sorted({_seen(c) for c in calls})
                listed = cam in self.gw_cameras
                status = "fail" if (self.isaac_expected or listed) else "not_available"
                rep.add(
                    f"api.stream.{kind}",
                    "api",
                    name,
                    status,
                    f"statuses {st}; camera {'listed' if listed else 'not listed'} by GET /api/cameras"
                    + ("" if status == "fail" else " (2D fake has no such camera; the app fell back)"),
                )
        errs = [c for c in net.calls if isinstance(c.get("status"), int) and c["status"] >= 500]
        if errs:
            rep.add(
                "api.5xx",
                "api",
                "No 5xx from the gateway",
                "fail",
                "; ".join(f"{c['method']} {c['path']} {c['status']}" for c in errs[:6]),
            )
        else:
            rep.add("api.5xx", "api", "No 5xx from the gateway", "pass")

    # ---- 4b. judges' flow: photo checks, checkout unload by the arm, payment agent, purchase history
    async def judges_checks(self, client: httpx.AsyncClient) -> None:
        rep, cfg, net = self.rep, self.cfg, self.net
        G = "judges"
        print("\nJudges' flow (events + REST)")
        ev = net.judges
        # produce review (choose units from photos)
        pc = net.jfind("approval.requested", kind="pick_choice")
        rep.add(
            "j.produce_review",
            G,
            "Produce photo review card (pick_choice) shown and answered",
            "pass" if pc and any(a["kind"] == "choice" for a in rep.approvals) else ("info" if not pc else "fail"),
            f"{len(pc)} pick_choice card(s), {len(net.jfind('pick.choices'))} pick.choices"
            + ("" if pc else " (no produce in this order)"),
        )
        # per-item photo checks of the picks
        vers = [
            e
            for e in ev
            if e["type"] == "item.verification" and str(e.get("question") or "").startswith("Is this the right")
        ]
        created = {e["verification_id"] for e in vers if e["status"] == "pending"}
        final: dict[str, dict[str, Any]] = {}
        for e in vers:
            if e["status"] != "pending":
                final[e["verification_id"]] = e
        by_status = Counter(e["status"] for e in final.values())
        picks = len(net.jfind("item.picked"))
        user_ok = by_status.get("accepted", 0)
        user_bad = by_status.get("rejected", 0)
        need_bad = cfg.verify_wrong > 0
        rep.add(
            "j.pick_checks",
            G,
            "Photo check card per pick (item.verification), answered in the app",
            "pass" if created and user_ok >= 1 and (user_bad >= 1 or not need_bad) else "fail",
            f"{len(created)} cards for {picks} item.picked; resolved {dict(by_status)}; app answers {[a['answer'] for a in self.verify_answers]}",
        )
        photos = [e for e in vers if e.get("photo_url")]
        if photos:
            r = await client.get(cfg.gateway_url + photos[0]["photo_url"], timeout=10.0)
            sz = jpeg_size(r.content) if r.status_code == 200 else None
            rep.add(
                "j.check_photo",
                G,
                "Photo-check photos served (GET /api/evidence/<frame>.jpg)",
                "pass" if sz and sz[0] >= 160 else ("warn" if sz else "fail"),
                f"{r.status_code} {sz} from {photos[0].get('camera_id')}",
            )
        if need_bad:
            rej = [e for e in final.values() if e["status"] == "rejected"]
            if rej:
                sku = rej[0]["sku"]
                again = [e for e in vers if e["sku"] == sku and int(e.get("attempt") or 1) > 1]
                rep.add(
                    "j.wrong_repick",
                    G,
                    "'Wrong item' -> put back + re-pick (2nd photo card for that SKU)",
                    "pass" if again else "info",
                    f"{sku}: "
                    + (
                        f"re-picked, attempt {again[0].get('attempt')} card {again[0]['status'] if len(again) == 1 else [a['status'] for a in again]}"
                        if again
                        else "kept (backend without put-back: noted for staff)"
                    ),
                )
        # checkout unload by the arm
        un = net.jfind("checkout.unload")
        first_unload_t = min((e["t"] for e in un), default=None)
        baskets = [
            e
            for e in ev
            if e["type"] == "robot.state" and (self.assigned_robot is None or e.get("robot_id") == self.assigned_robot)
        ]
        before = [b for b in baskets if first_unload_t is not None and b["t"] <= first_unload_t]
        basket_at_counter = before[-1]["basket"] if before else None
        total = max((int(e.get("total") or 0) for e in un), default=0)
        seq_ok = True
        detail: list[str] = []
        order: list[int] = []
        for n in range(1, total + 1):
            st = [e["status"] for e in un if int(e.get("n") or 0) == n and e["status"] != "done"]
            want = ["started", "placed", "verified"]
            it = iter(st)
            if not all(any(x == w for x in it) for w in want):
                seq_ok = False
                detail.append(f"#{n}: {st}")
            t_start = next((e["t"] for e in un if int(e.get("n") or 0) == n and e["status"] == "started"), None)
            if t_start is not None:
                order.append(n)
        starts = [e for e in un if e["status"] == "started"]
        in_order = [int(e.get("n") or 0) for e in starts] == sorted(int(e.get("n") or 0) for e in starts)
        units_match = basket_at_counter is not None and len(basket_at_counter) == total
        rep.add(
            "j.unload_events",
            G,
            "Checkout unload: one started -> placed -> verified per basket unit, in order (checkout.unload)",
            "pass" if un and total and seq_ok and in_order and units_match else "fail",
            (
                f"{total} units; basket at the counter {basket_at_counter}; order {[int(e.get('n') or 0) for e in starts]}"
                if un
                else "no checkout.unload event (worker without basket_unload? PICKY_UNLOAD_BY_ARM off?)"
            )
            + (f"; bad {detail}" if detail else ""),
            texts=[e.get("text") for e in un if e["status"] == "verified"],
        )
        g_st = [
            e for e in ev if e["type"] == "step.started" and e.get("kind") == "grasp" and e.get("source") == "basket"
        ]
        p_st = [
            e
            for e in ev
            if e["type"] == "step.started" and e.get("kind") == "place" and e.get("destination") == "counter"
        ]
        done = {e["step_id"]: e["status"] for e in ev if e["type"] == "step.completed"}
        g_ok = sum(1 for e in g_st if done.get(e["step_id"]) in ("succeeded", "provisional"))
        p_ok = sum(1 for e in p_st if done.get(e["step_id"]) in ("succeeded", "provisional"))
        tiers = sorted({str(e.get("tier")) for e in g_st})
        rep.add(
            "j.unload_steps",
            G,
            "Unload loop steps: grasp(source=basket) + place(destination=counter) per unit, all succeeded",
            "pass" if total and g_ok >= total and p_ok >= total else "fail",
            f"grasp(basket) {g_ok}/{len(g_st)} ok, place(counter) {p_ok}/{len(p_st)} ok, units {total}, tiers {tiers}",
        )
        counter_cards = [
            e for e in ev if e["type"] == "item.verification" and "on the counter" in str(e.get("question") or "")
        ]
        rep.add(
            "j.counter_checks",
            G,
            "Photo card of every unit on the counter (item.verification)",
            "pass" if total and len({e["verification_id"] for e in counter_cards}) >= total else "fail",
            f"{len({e['verification_id'] for e in counter_cards})} counter cards for {total} units",
        )
        pay = net.jfind("payment.agent_step")
        t_pay = min(
            [e["t"] for e in pay] + [e["t"] for e in net.jfind("approval.requested", kind="checkout")], default=None
        )
        t_done = next((e["t"] for e in un if e["status"] == "done"), None)
        before_pay = [b for b in baskets if t_pay is not None and b["t"] <= t_pay]
        empty = bool(before_pay) and before_pay[-1]["basket"] == []
        rep.add(
            "j.basket_empty_before_pay",
            G,
            "Basket empty (and unload done) before the payment agent starts",
            "pass" if empty and t_done is not None and t_pay is not None and t_done <= t_pay else "fail",
            f"unload done at {t_done}s, payment starts at {t_pay}s, basket then {before_pay[-1]['basket'] if before_pay else '?'}",
        )
        if self.unload_texts:
            rep.add(
                "ui.unload_panel",
                "ui",
                "Live page shows the per-unit unload ('Unloading n/N: ...')",
                "pass",
                " | ".join(self.unload_texts[:4]) + (" ..." if len(self.unload_texts) > 4 else ""),
            )
        else:
            rep.add(
                "ui.unload_panel",
                "ui",
                "Live page shows the per-unit unload ('Unloading n/N: ...')",
                "fail" if un else "info",
                "no 'Unloading' line seen on the page",
            )
        # payment agent
        names = [e.get("name") for e in pay]
        rest = None
        if self.trip_id:
            try:
                r = await client.get(f"{cfg.gateway_url}/api/trips/{self.trip_id}/payment", timeout=8.0)
                rest = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                rest = None
        sim_all = bool(rest) and all(x.get("simulated") for x in rest)
        rep.add(
            "j.payment_agent",
            G,
            "Visa payment agent timeline (payment.agent_step + GET /api/trips/{id}/payment, all simulated)",
            "pass" if pay and sim_all else "fail",
            f"{len(pay)} events {list(dict.fromkeys(names))}; REST {len(rest or [])} steps, simulated={sim_all}"
            + (f", provider {rest[0].get('provider')}" if rest else ""),
        )
        # purchase history
        try:
            r = await client.get(f"{cfg.gateway_url}/api/users/{cfg.user_id}/purchases?limit=50", timeout=8.0)
            rows = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            rows = []
        mine = [x for x in rows if self.trip_id and x.get("trip_id") == self.trip_id]
        with_photo = [x for x in mine if x.get("photo_urls")]
        rep.add(
            "j.purchases",
            G,
            "Paid order lands in the purchase-history DB (GET /api/users/{id}/purchases)",
            "pass" if mine else "fail",
            f"{len(mine)} rows for {self.trip_id} ({len(with_photo)} with robot photos): "
            + ", ".join(f"{x.get('sku')} x{x.get('qty')}" for x in mine[:6]),
        )
        robot = self.assigned_robot or cfg.robot_id
        try:
            r = await client.get(f"{cfg.gateway_url}/api/fleet/robots/{robot}/vla", timeout=8.0)
            caps = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            caps = {}
        flat = json.dumps(caps)
        rep.add(
            "j.vla_caps",
            G,
            f"{robot} VLA tiers (basket_unload)",
            "info",
            ("basket_unload: true" if '"basket_unload": true' in flat else "basket_unload not advertised")
            + f"; {flat[:240]}",
        )

    # ---- 5b. streams read directly over HTTP
    async def gateway_alive(self, client: httpx.AsyncClient) -> bool:
        """GET /health again: a gateway that died mid-run must read as ONE clear failure, not 20 stream failures."""
        try:
            ok = (await client.get(self.cfg.gateway_url + "/health", timeout=5.0)).status_code == 200
        except (httpx.HTTPError, OSError):
            ok = False
        if not ok:
            self.rep.add(
                "pre.gateway_alive",
                "preflight",
                "Gateway still up after the UI run",
                "fail",
                "GET /health no longer answers: the gateway (or the ssh tunnel) went down during the run; direct stream checks skipped",
            )
        return ok

    async def http_streams(self, client: httpx.AsyncClient) -> None:
        cfg = self.cfg
        print("\nStreams read directly from the gateway")
        if not await self.gateway_alive(client):
            return
        robots = [r for r in dict.fromkeys([self.assigned_robot, cfg.robot_id]) if r]
        targets: list[tuple[str, bool]] = []  # (camera_id, required)
        for r in robots:
            targets += [
                (f"{r}.director", self.isaac_expected),
                (f"{r}.front.perceived", self.isaac_expected),
                (f"{r}.front", True),
            ]
        targets.append((f"{cfg.store_id}.ceiling-1", True))
        for cam, required in targets:
            await self._one_stream(client, cam, required)

    async def _one_stream(
        self, client: httpx.AsyncClient, cam: str, required: bool, group: str = "streams"
    ) -> dict[str, Any]:
        rep, cfg = self.rep, self.cfg
        listed = cam in self.gw_cameras
        base = f"{cfg.gateway_url}/api/cameras/{cam}"
        mj = await read_mjpeg(client, f"{base}/stream.mjpg?fps=10", cfg.mjpeg_seconds, cfg.mjpeg_min_parts)
        s1 = await read_snapshot(client, f"{base}/snapshot.jpg")
        await asyncio.sleep(1.2)
        s2 = await read_snapshot(client, f"{base}/snapshot.jpg")
        wsf = (
            await ws_frames(cfg.gateway_url, cam)
            if listed or mj["status"] == 200
            else {"frames": 0, "placeholder": 0, "skipped": True}
        )
        row = {
            "camera": cam,
            "listed": listed,
            "source": (self.gw_cameras.get(cam) or {}).get("source"),
            "mjpeg": mj,
            "snapshot": s1,
            "snapshot_changed": (s1["sha1"] != s2["sha1"]) if s1["sha1"] and s2["sha1"] else None,
            "ws": wsf,
        }
        rep.streams_http.append(row)
        absent = mj["status"] == 404 and not listed
        na = "fail" if required else "not_available"
        name = f"MJPEG {cam}"
        if absent:
            rep.add(f"http.mjpeg.{cam}", group, name, na, "404, not listed by GET /api/cameras")
        elif (
            mj["status"] == 200
            and (mj["ctype"] or "").startswith("multipart/x-mixed-replace")
            and mj["parts"] >= cfg.mjpeg_min_parts
        ):
            rep.add(
                f"http.mjpeg.{cam}",
                group,
                name,
                "pass",
                f"{mj['parts']} JPEG parts in {mj['read_s']} s, {mj['fps']} fps, {mj['resolution']}, first frame {mj['first_frame_s']} s, {mj['distinct']} distinct",
            )
        else:
            rep.add(
                f"http.mjpeg.{cam}",
                group,
                name,
                "fail",
                f"status {mj['status']} {mj['ctype']}, {mj['parts']} parts (need {cfg.mjpeg_min_parts}) in {cfg.mjpeg_seconds:.0f} s {mj.get('error') or ''}".strip(),
            )
        name = f"Snapshot {cam}"
        if s1["status"] == 404 and not listed:
            rep.add(f"http.snapshot.{cam}", group, name, na, "404, not listed")
        elif s1["status"] == 200 and (s1["ctype"] or "").startswith("image/jpeg") and s1["resolution"]:
            rep.add(
                f"http.snapshot.{cam}",
                group,
                name,
                "pass",
                f"{s1['resolution']}, {s1['kb']} KB, {s1['ms']} ms, changed after 1.2 s: {row['snapshot_changed']}",
            )
        else:
            rep.add(
                f"http.snapshot.{cam}",
                group,
                name,
                "fail",
                f"status {s1['status']} {s1['ctype']} {s1.get('error') or ''}".strip(),
            )
        if not wsf.get("skipped"):
            name = f"Frames from {cam} are real (not the gateway's offline card)"
            if wsf["frames"] and wsf["placeholder"] == wsf["frames"]:
                rep.add(
                    f"http.real.{cam}",
                    group,
                    name,
                    "fail" if required else "not_available",
                    f"every frame is the placeholder ({wsf['frame_ids'][0]}): the gateway lists the camera but cannot capture it"
                    " (robot unknown to the robot arm / worker not sending frames); see the gateway log",
                )
            elif wsf["frames"]:
                rep.add(
                    f"http.real.{cam}",
                    group,
                    name,
                    "pass",
                    f"{wsf['frames']} WS frames ({wsf['size']}), source={row['source']}, e.g. {wsf['frame_ids'][0]}",
                )
            else:
                rep.add(
                    f"http.real.{cam}",
                    group,
                    name,
                    "warn",
                    f"no camera.frame on WS /ws/cameras/{cam} in 4 s ({wsf.get('error')})",
                )
        return row

    # ---- 4. real robot
    async def real_robot(self, client: httpx.AsyncClient, page: Page | None) -> None:
        rep, cfg = self.rep, self.cfg
        rid = cfg.real_robot_id
        print(f"\nReal robot ({rid}) {'EXPECTED' if cfg.expect_real_robot else 'not expected (info only)'}")
        if not await self.gateway_alive(client):
            return
        try:
            self.fleet = (await client.get(cfg.gateway_url + "/api/fleet/robots", timeout=8.0)).json()
            self.gw_cameras = {
                c["camera_id"]: c for c in (await client.get(cfg.gateway_url + "/api/cameras", timeout=8.0)).json()
            }
        except Exception as exc:  # noqa: BLE001
            rep.context["real_robot_error"] = str(exc)
        bot = next((r for r in self.fleet if r.get("robot_id") == rid), None)
        miss = "fail" if cfg.expect_real_robot else "skipped"
        # the SO-101 bridge (arms/robot/so101/bridge.py) dials the gateway's /ws/sim like an Isaac worker
        try:
            sim = (await client.get(cfg.gateway_url + "/api/sim/status", timeout=8.0)).json()
        except Exception as exc:  # noqa: BLE001
            sim = {"workers": [], "error": str(exc)}
        workers = [
            w
            for w in sim.get("workers", [])
            if rid in [r.get("robot_id") for r in w.get("robots", [])] or "so101" in str(w.get("worker_id", ""))
        ]
        if workers:
            w = workers[0]
            rep.add(
                "real.bridge",
                "real_robot",
                f"SO-101 bridge connected on /ws/sim (hosts {rid})",
                "pass" if w.get("connected", True) else miss,
                f"worker {w.get('worker_id')} ({w.get('sim_version')}), robots {[r.get('robot_id') for r in w.get('robots', [])]}, "
                f"cameras {[c.get('camera_id') for c in w.get('cameras', [])]}, connected={w.get('connected')}",
            )
        else:
            rep.add(
                "real.bridge",
                "real_robot",
                f"SO-101 bridge connected on /ws/sim (hosts {rid})",
                miss,
                f"no /ws/sim worker hosts {rid} (workers: {[w.get('worker_id') for w in sim.get('workers', [])]})",
            )
        if bot is None:
            rep.add(
                "real.listed",
                "real_robot",
                f"GET /api/fleet/robots lists {rid}",
                miss,
                f"not listed (robots: {[r.get('robot_id') for r in self.fleet]})",
            )
        else:
            rep.add(
                "real.listed",
                "real_robot",
                f"GET /api/fleet/robots lists {rid}",
                "pass",
                f"kind={bot.get('kind')}, backend={bot.get('backend')}, status={bot.get('status')}",
            )
            rep.add(
                "real.connected",
                "real_robot",
                f"{rid} connected",
                "pass" if bot.get("connected") else miss,
                f"connected={bot.get('connected')}, error={bot.get('error')}",
            )
        cams = [c for c, info in self.gw_cameras.items() if info.get("robot_id") == rid or c.startswith(rid + ".")]
        if not cams:
            rep.add("real.camera", "real_robot", f"GET /api/cameras lists a {rid} camera", miss, "none listed")
        else:
            rep.add("real.camera", "real_robot", f"GET /api/cameras lists a {rid} camera", "pass", ", ".join(cams))
            for cam in cams[:2]:
                await self._one_stream(client, cam, cfg.expect_real_robot, group="real_robot")
        if page is None or (not cfg.expect_real_robot and bot is None):
            if page is not None:
                rep.add("real.app", "real_robot", f"App shows {rid}", "skipped", "not expected and not in the fleet")
            return
        names = [rid] + ([bot["name"]] if bot and bot.get("name") else [])
        found = []
        for p in cfg.real_robot_pages:
            try:
                await page.goto(cfg.app_url + p, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                hit = await page.evaluate(
                    """(names) => { const t = document.body.innerText; const imgs = [...document.querySelectorAll('img')].map(i => i.currentSrc || i.src);
                       return names.filter(n => t.includes(n) || imgs.some(s => s.includes(n))); }""",
                    names,
                )
                if hit:
                    found.append(f"{p}: {hit}")
                    await self.shot(page, "real_robot" + p.replace("/", "_"))
            except PWError as exc:
                rep.context.setdefault("real_robot_page_errors", []).append(f"{p}: {exc}"[:160])
        if found:
            rep.add("real.app", "real_robot", f"App shows {rid}", "pass", "; ".join(found))
        else:
            rep.add(
                "real.app",
                "real_robot",
                f"App shows {rid}",
                "gap" if cfg.expect_real_robot else "skipped",
                f"no page of {cfg.real_robot_pages} mentions {names} or shows its camera (no real-robot panel in the app yet)",
            )

    # ---- report
    def write(self) -> None:
        rep, cfg, net = self.rep, self.cfg, self.net
        rep.timings["total"] = round(time.monotonic() - rep.t0, 2)
        rel = {}
        if self.t_order is not None:
            for k, v in net.first_event.items():
                rel[k] = round(v - self.t_order, 2)
        summary = Counter(c.status for c in rep.checks)
        overall = "FAIL" if summary["fail"] else "PASS"
        calls = Counter()
        statuses: dict[tuple[str, str], Counter] = {}
        for c in net.calls:
            key = (c["method"], c["template"])
            calls[key] += 1
            statuses.setdefault(key, Counter())[
                _seen(c) if (c["status"] is not None or c["failure"]) else "pending"
            ] += 1
        ws_rows = [
            {
                "url": w["url"],
                "opened_t": w["opened_t"],
                "closed_t": w["closed_t"],
                "frames": w["frames"],
                "types": dict(w["types"].most_common()),
                "error": w["error"],
            }
            for w in net.ws
        ]
        data = {
            "overall": overall,
            "summary": dict(summary),
            "started_at": rep.started_at,
            "config": {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items()},
            "isaac_expected": self.isaac_expected,
            "trip_id": self.trip_id,
            "request_id": self.request_id,
            "assigned_robot": self.assigned_robot,
            "checks": [asdict(c) for c in rep.checks],
            "timings_s": rep.timings,
            "first_event_after_order_s": rel,
            "streams_http": rep.streams_http,
            "streams_page": rep.streams_page,
            "panels_on_air": rep.panels,
            "approvals": rep.approvals,
            "approval_events": net.events,
            "judges_events": net.judges,
            "unload_texts": self.unload_texts,
            "verify_answers": self.verify_answers,
            "websockets": ws_rows,
            "gateway_calls": [
                {"method": m, "path": p, "count": n, "statuses": dict(statuses[(m, p)])}
                for (m, p), n in sorted(calls.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            ],
            "console_errors": net.console_errors[:50],
            "page_errors": net.page_errors[:30],
            "context": rep.context,
            "screenshots": rep.screens,
            "artifacts": rep.artifacts,
        }
        (self.dir / "report.json").write_text(json.dumps(data, indent=2, default=str))

        L: list[str] = []
        L.append(f"# Picky E2E run {self.dir.name}: {overall}\n")
        L.append(
            f"{rep.started_at} · app `{cfg.app_url}` · gateway `{cfg.gateway_url}` · robot `{self.assigned_robot or cfg.robot_id}` · "
            f"Isaac {'expected' if self.isaac_expected else 'not expected'} · real robot {'expected' if cfg.expect_real_robot else 'not expected'}\n"
        )
        L.append(
            "Checks: "
            + ", ".join(f"{k} {summary[k]}" for k in STATUSES if summary[k])
            + f" · trip `{self.trip_id}` · total {rep.timings['total']} s\n"
        )
        L.append("| Question | Verdict | Checks |\n|---|---|---|")
        for group, q in (
            ("ui", "Judges' flow works in the app"),
            ("api", "The right APIs are hit"),
            ("streams", "Gateway (vhagar) -> website streams work"),
            ("real_robot", "Real robot connects and shows"),
        ):
            g = Counter(c.status for c in rep.checks if c.group == group)
            verdict = "FAIL" if g["fail"] else "GAP" if g["gap"] else "PASS" if g["pass"] else "N/A"
            if verdict == "PASS" and (g["warn"] or g["not_available"]):
                verdict = "PASS (with warnings / n/a)"
            L.append(f"| {q} | {verdict} | " + ", ".join(f"{k} {g[k]}" for k in STATUSES if g[k]) + " |")
        L.append("")
        if rep.failed:
            L.append("## Failures\n")
            L.append("| Check | Detail |\n|---|---|")
            for c in rep.failed:
                L.append(f"| {c.name} | {_md(c.detail)} |")
            L.append("")
        for group, title in (
            ("preflight", "Preflight"),
            ("ui", "Judges' flow (UI)"),
            ("judges", "Judges' flow: photo checks, checkout unload, payment agent, purchase DB"),
            ("api", "Right APIs hit (browser traffic)"),
            ("streams", "vhagar -> website streams"),
            ("real_robot", "Real robot"),
        ):
            rows = [c for c in rep.checks if c.group == group]
            if not rows:
                continue
            L.append(f"## {title}\n")
            L.append("| Status | Check | Detail |\n|---|---|---|")
            for c in rows:
                L.append(f"| {c.status.upper()} | {_md(c.name)} | {_md(c.detail)} |")
            L.append("")
        if rep.streams_http:
            L.append("### Direct stream reads\n")
            L.append(
                "| Camera | Listed | Source | MJPEG status | Parts | FPS | Resolution | First frame | KB/frame | Snapshot | Snapshot res | Snapshot changed | WS frames |"
                "\n|---|---|---|---|---|---|---|---|---|---|---|---|---|"
            )
            for r in rep.streams_http:
                m, s = r["mjpeg"], r["snapshot"]
                L.append(
                    f"| `{r['camera']}` | {r['listed']} | {r.get('source')} | {m['status']} | {m['parts']} | {m['fps']} | {m['resolution']} | {m['first_frame_s']} | "
                    f"{m['kb_per_frame']} | {s['status']} | {s['resolution']} | {r['snapshot_changed']} | {_ws_cell(r.get('ws') or {})} |"
                )
            L.append("")
        if rep.streams_page:
            L.append("### Camera images on the live page\n")
            L.append(f"Panels on air: {', '.join(f'`{p}`' for p in rep.panels) or '-'}\n")
            L.append("| Camera | Kind | Natural | Shown | Method | Changed | Diff % |\n|---|---|---|---|---|---|---|")
            for r in rep.streams_page:
                L.append(
                    f"| `{r['camera']}` | {r['kind']} | {r['natural']} | {r['shown']} | {r.get('method', '-')} | {r.get('changed')} | {r.get('diff_pct', '-')} |"
                )
            L.append("")
        L.append("## /ws events\n")
        L.append("| Socket | Frames | Types |\n|---|---|---|")
        for w in ws_rows:
            L.append(
                f"| `{_md(w['url'].replace(_origin(w['url']), ''))[:110]}` | {w['frames']} | {_md(', '.join(f'{k} {v}' for k, v in w['types'].items()))} |"
            )
        if rel:
            L.append(
                "\nFirst event after the order click (s): "
                + ", ".join(f"{k} {v}" for k, v in sorted(rel.items(), key=lambda kv: kv[1]))
            )
        L.append("\n## Gateway calls the app made\n")
        L.append("| Method | Path | Count | Status / content type |\n|---|---|---|---|")
        for row in data["gateway_calls"]:
            L.append(
                f"| {row['method']} | `{row['path']}` | {row['count']} | {_md(', '.join(f'{k} x{v}' for k, v in row['statuses'].items()))} |"
            )
        if rep.approvals:
            L.append("\n## Approval cards answered\n")
            L.append("| t (s) | Kind | Answer | Card |\n|---|---|---|---|")
            for a in rep.approvals:
                L.append(f"| {a['t']} | {a['kind']} | {_md(str(a['answer']))} | {_md(a['card'][:120])} |")
        L.append("\n## Timings (s since harness start)\n")
        L.append(", ".join(f"{k} {v}" for k, v in rep.timings.items()))
        if net.console_errors or net.page_errors:
            L.append("\n## Browser errors (first 15)\n")
            for e in (net.page_errors + net.console_errors)[:15]:
                L.append(f"- {_md(e[:200])}")
        L.append("\n## Artifacts\n")
        for k, v in rep.artifacts.items():
            L.append(f"- {k}: `{v}`")
        L.append("- screenshots: " + ", ".join(f"`{s}`" for s in rep.screens))
        (self.dir / "report.md").write_text("\n".join(L) + "\n")


def _seen(c: dict[str, Any]) -> str:
    """What happened to a browser request: status + content type, or the browser failure + the direct probe."""
    if c.get("status") is not None:
        return f"{c['status']} {(c.get('ctype') or '').split(';')[0]}".strip()
    return f"browser {c.get('failure') or 'no response'}" + (
        f", direct GET -> {c['direct']}" if c.get("direct") else ""
    )


def _ws_cell(w: dict[str, Any]) -> str:
    if w.get("skipped"):
        return "-"
    if not w.get("frames"):
        return f"none ({(w.get('error') or '')[:40]})"
    return f"{w['frames']} ({w['placeholder']} placeholder)"


def _md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


async def main() -> int:
    cfg = Config.from_env()
    run = Run(cfg)
    rep = run.rep
    print(f"Run dir: {run.dir}")
    page: Page | None = None
    async with httpx.AsyncClient(follow_redirects=True) as client:
        if not await run.preflight(client):
            run.write()
            print(f"\nUNREACHABLE. Report: {run.dir / 'report.md'}")
            return 2
        if cfg.skip_ui:
            await run.http_streams(client)
            await run.real_robot(client, None)
        else:
            async with async_playwright() as pw:
                try:
                    browser = await pw.chromium.launch(headless=not cfg.headed, slow_mo=cfg.slow_mo_ms)
                except PWError as exc:
                    print(f"Chromium missing? Run: ./run.sh install\n{exc}")
                    return 2
                vw, vh = cfg.viewport
                ctx_args: dict[str, Any] = {
                    "viewport": {"width": vw, "height": vh},
                    "record_video_dir": str(run.dir / "_video"),
                    "record_video_size": {"width": vw, "height": vh},
                }
                if cfg.har:
                    ctx_args.update(record_har_path=str(run.dir / "network.har"), record_har_content="omit")
                context = await browser.new_context(**ctx_args)
                page = await context.new_page()
                run.net.attach(page)
                try:
                    if await run.home_and_order(page):
                        await run.live(page)
                    await asyncio.gather(*run.net.tasks, return_exceptions=True)
                    await run.api_checks(client)
                    await run.judges_checks(client)
                    await run.http_streams(client)
                    await run.real_robot(client, page)
                except Exception as exc:  # noqa: BLE001 - keep the report
                    rep.add(
                        "ui.exception",
                        "ui",
                        "Harness ran without exceptions",
                        "fail",
                        f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                    try:
                        await run.shot(page, "exception")
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.gather(*run.net.tasks, return_exceptions=True)
                    if not any(c.group == "api" for c in rep.checks):
                        await run.api_checks(client)
                finally:
                    video = page.video
                    await context.close()
                    await browser.close()
                    if video is not None:
                        try:
                            src = Path(await video.path())
                            dst = run.dir / "run.webm"
                            await asyncio.to_thread(src.replace, dst)
                            rep.artifacts["video"] = str(dst)
                            (run.dir / "_video").rmdir()
                        except Exception as exc:  # noqa: BLE001
                            rep.artifacts["video_error"] = str(exc)
                    if cfg.har:
                        rep.artifacts["har"] = str(run.dir / "network.har")
    rep.artifacts["report_md"] = str(run.dir / "report.md")
    rep.artifacts["report_json"] = str(run.dir / "report.json")
    run.write()
    latest = cfg.runs_dir / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run.dir.name)
    except OSError:
        pass
    summary = Counter(c.status for c in rep.checks)
    print(
        f"\n{'FAIL' if summary['fail'] else 'PASS'}: " + ", ".join(f"{k} {summary[k]}" for k in STATUSES if summary[k])
    )
    for c in rep.failed:
        print(f"  FAIL  {c.name}: {c.detail}")
    print(f"Report: {run.dir / 'report.md'}")
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
