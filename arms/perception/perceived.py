"""Perceived onboard streams (contracts 0.2.3): ``<robot>.front.perceived`` / ``<robot>.wrist.perceived``.

The live onboard frame (already face-blurred by ``CameraService``) with detection boxes, labels and confidence drawn
on it for the robot's CURRENT target product (green) and the other open shopping-list items (amber):

- Detection runs ASYNCHRONOUSLY on the latest frame at ``PICKY_PERCEIVED_DETECT_HZ`` (~1 Hz) per stream; the newest
  boxes are reused on every frame in between, so the stream never waits on a model call.
- Provider: Perceptron Mk1.5 ``detect`` with one class string per product ('<brand> <name> <size>'; mentions mapped
  back to SKUs by ``verify.match_mention_to_sku``), header "Perceptron Mk1.5 · live". Each call is charged to the
  robot's trip (``PICKY_PERCEIVED_CALL_BUDGET`` per trip, shared by the robot's two perceived streams).
- Fallback (no key / SDK, quota, auth, rate limit, timeout, budget spent): ground-truth boxes from the sim (a capture of
  the source camera carries every visible unit with its pixel box), labelled "ground truth" so nobody mistakes them
  for the model. Quota / auth errors pause Perceptron for minutes, a rate limit for seconds.
- No active goal (robot idle): no detection, no Perceptron call; the header says so.

Privacy (CLAUDE.md section 8): the source frame is a served (blurred) frame; the overlay is drawn on it and the result
keeps ``blurred=True``. Ground-truth captures are used for their instance boxes only; their pixels never leave.
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from contracts.cameras import CameraInfo, perceived_source
from contracts.catalog import Product
from contracts.common import RobotId, Sku
from contracts.grounding import NORM_MAX
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance
from contracts.verify import match_mention_to_sku
from utils.log import get_logger

log = get_logger(__name__)

HEADER_LIVE = "Perceptron Mk1.5 · live"
HEADER_GT = "Ground truth"
DEFAULT_DETECT_HZ = 1.0
DEFAULT_TRIP_BUDGET = 240
DEFAULT_TIMEOUT_S = 6.0
MAX_CLASSES = 6
"""Current target + up to 5 other open list items per detect call."""
FIRST_DETECT_WAIT_S = 3.0
"""A snapshot with no detection yet waits this long for the first one (streams never wait)."""
STALE_S = 8.0
"""Boxes older than this are not drawn (the view has moved on)."""
TARGETS_TTL_S = 1.0
GOAL_ITEM_OPEN = frozenset({"pending", "located", "picked"})
COOLDOWN_S: dict[str, float] = {
    "insufficient_quota": 600.0,
    "auth": 600.0,
    "unavailable": 600.0,
    "sdk_missing": 3600.0,
    "rate_limit": 20.0,
}
DEFAULT_COOLDOWN_S = 3.0
TARGET_COLOR = (34, 197, 94)
OTHER_COLOR = (245, 158, 11)
JPEG_QUALITY = 84

Method = Literal["perceptron", "ground_truth", "none"]


@dataclass
class OverlayBox:
    """One box in normalized image coordinates (0..1)."""

    x0: float
    y0: float
    x1: float
    y1: float
    label: str
    sku: Sku | None
    confidence: float
    target: bool


@dataclass
class Detection:
    boxes: list[OverlayBox]
    method: Method
    note: str
    target_label: str | None
    at: float
    latency_ms: float = 0.0
    error_code: str | None = None


@dataclass
class Targets:
    trip_key: str | None
    target: Sku | None
    skus: list[Sku]
    labels: dict[Sku, str]
    short: dict[Sku, str]


@dataclass
class _Stream:
    det: Detection | None = None
    task: asyncio.Task[Any] | None = None
    last_start: float = -1e9
    last_frame: CameraFrame | None = None


@dataclass
class _Budget:
    used: dict[str, int] = field(default_factory=dict)

    def left(self, key: str | None, total: int) -> int:
        return total - self.used.get(key or "-", 0)

    def spend(self, key: str | None) -> int:
        k = key or "-"
        self.used[k] = self.used.get(k, 0) + 1
        return self.used[k]


def class_string(product: Product | None, sku: Sku) -> str:
    """'<brand> <name> <size>' (what Perceptron is asked to find)."""
    if product is None:
        return sku.replace("-", " ").lower()
    parts = [product.brand or "", product.name]
    if product.size and product.size.lower() not in product.name.lower():
        parts.append(product.size)
    return " ".join(p for p in parts if p).strip()


def _short(product: Product | None, sku: Sku) -> str:
    return product.name if product is not None else sku


def _font(px: int):  # noqa: ANN202 - PIL font
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def draw_overlay(
    frame: CameraFrame, det: Detection | None, *, camera_id: str, header: str | None = None, now: float | None = None
) -> CameraFrame:
    """The frame with ``det``'s boxes + a header bar, re-encoded; keeps ``blurred`` (must already be True)."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64)))
    img.load()
    img = img.convert("RGB")
    w, h = img.size
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    scale = max(0.6, w / 640)
    fpx = max(10, round(13 * scale))
    f = _font(fpx)
    fb = _font(max(11, round(14 * scale)))
    now = time.monotonic() if now is None else now
    fresh = det is not None and now - det.at <= STALE_S
    if fresh and det is not None:
        for b in sorted(det.boxes, key=lambda b: b.target):  # target drawn last (on top)
            color = TARGET_COLOR if b.target else OTHER_COLOR
            x0, y0, x1, y1 = b.x0 * w, b.y0 * h, b.x1 * w, b.y1 * h
            width = max(2, round((3 if b.target else 2) * scale))
            d.rectangle((x0, y0, x1, y1), outline=(*color, 255), width=width)
            if not b.label:  # another unit of an already-labelled product
                continue
            if det.method == "ground_truth":
                tag = f"{b.label} · ground truth"
            else:
                tag = f"{b.label} {round(b.confidence * 100)}%"
            tw = d.textlength(tag, font=f) + 8
            ty = y0 - fpx - 6 if y0 - fpx - 6 >= 30 * scale else y0 + 2
            tx = min(max(0.0, x0), max(0.0, w - tw))
            d.rectangle((tx, ty, tx + tw, ty + fpx + 5), fill=(*color, 230))
            d.text((tx + 4, ty + 2), tag, fill=(15, 23, 42, 255), font=f)
    bar = round(26 * scale)
    d.rectangle((0, 0, w, bar), fill=(15, 23, 42, 255))
    if det is None:
        title, color, right = header or HEADER_LIVE, (148, 163, 184), "starting…"
    else:
        title, color = {
            "perceptron": (HEADER_LIVE, (74, 222, 128)),
            "ground_truth": (HEADER_GT, (251, 191, 36)),
        }.get(det.method, ("Idle", (148, 163, 184)))
        age = max(0.0, now - det.at)
        bits = []
        if det.target_label:
            bits.append(f"target: {det.target_label}")
        if det.method != "none":
            bits.append(f"{len(det.boxes)} box{'es' if len(det.boxes) != 1 else ''} · {age:.1f}s ago")
        right = " · ".join(bits) if bits else det.note
    d.ellipse((8, bar / 2 - 5 * scale, 8 + 10 * scale, bar / 2 + 5 * scale), fill=(*color, 255))
    d.text((14 + 10 * scale, bar / 2), title, fill=(241, 245, 249, 255), font=fb, anchor="lm")
    tw = d.textlength(right, font=f)
    d.text((w - tw - 8, bar / 2), right, fill=(203, 213, 225, 255), font=f, anchor="lm")
    if det is not None and det.method != "perceptron" and det.note:
        nb = round(20 * scale)
        d.rectangle((0, h - nb, w, h), fill=(15, 23, 42, 235))
        d.text((8, h - nb / 2), det.note, fill=(226, 232, 240, 255), font=f, anchor="lm")
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return frame.model_copy(
        update={
            "frame_id": f"{frame.frame_id}~perceived",
            "camera_id": camera_id,
            "jpeg_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "w": w,
            "h": h,
            "instances": [],
            "blurred": True,
        }
    )


class PerceivedStreams:
    """Annotates perceived-camera frames. ``provider`` = a ``VisionProvider`` (Perceptron) or None (ground truth only);
    ``capture_gt(camera_id)`` = a raw capture of the source camera (instances with pixel boxes) for the fallback."""

    def __init__(
        self,
        services: Services,
        *,
        provider: Any = None,
        capture_gt: Callable[[str], Awaitable[CameraFrame]] | None = None,
        detect_hz: float = DEFAULT_DETECT_HZ,
        trip_budget: int = DEFAULT_TRIP_BUDGET,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        provider_note: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.s = services
        self.provider = provider
        self.capture_gt = capture_gt
        self.period = 1.0 / max(0.05, float(detect_hz or DEFAULT_DETECT_HZ))
        self.trip_budget = max(0, int(trip_budget))
        self.timeout_s = timeout_s
        self.provider_note = provider_note
        self.clock = clock
        self._streams: dict[str, _Stream] = {}
        self._budget = _Budget()
        self._cooldown_until = 0.0
        self._cooldown_code: str | None = None
        self._products: dict[Sku, Product | None] = {}
        self._targets: dict[RobotId, tuple[float, Targets]] = {}
        self.calls = 0

    # ---- public ------------------------------------------------------------------------------------------------
    async def annotate(self, cam: CameraInfo, frame: CameraFrame, *, wait_s: float = 0.0) -> CameraFrame:
        """``frame`` (the source camera's served, blurred frame) with the latest boxes drawn; starts a detection on it
        when one is due. ``wait_s`` > 0 (snapshots) waits that long for the FIRST detection."""
        if not frame.blurred:
            raise ValueError(f"perceived overlay needs a blurred frame ({frame.frame_id})")
        st = self._streams.setdefault(cam.camera_id, _Stream())
        st.last_frame = frame
        now = self.clock()
        if (st.task is None or st.task.done()) and now - st.last_start >= self.period:
            st.last_start = now
            st.task = asyncio.ensure_future(self._detect(cam, frame, st))
        if st.det is None and wait_s > 0 and st.task is not None and not st.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(st.task), timeout=wait_s)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:  # noqa: BLE001 - drawn without boxes
                log.debug("perceived %s: first detection failed (%s)", cam.camera_id, exc)
        return await asyncio.to_thread(draw_overlay, frame, st.det, camera_id=cam.camera_id)

    def detection(self, camera_id: str) -> Detection | None:
        st = self._streams.get(camera_id)
        return st.det if st else None

    def budget_used(self, trip_key: str | None) -> int:
        return self._budget.used.get(trip_key or "-", 0)

    async def close(self) -> None:
        for st in self._streams.values():
            if st.task is not None and not st.task.done():
                st.task.cancel()

    # ---- targets -----------------------------------------------------------------------------------------------
    async def _product(self, sku: Sku) -> Product | None:
        if sku not in self._products:
            catalog = self.s.catalog
            p = None
            if catalog is not None:
                try:
                    p = await catalog.get_product(sku)
                except Exception:  # noqa: BLE001 - label falls back to the SKU
                    p = None
            self._products[sku] = p
        return self._products[sku]

    async def targets(self, robot_id: RobotId) -> Targets:
        """The robot's active goal: current target (first open item) + the other open items (<= MAX_CLASSES)."""
        cached = self._targets.get(robot_id)
        now = self.clock()
        if cached is not None and now - cached[0] < TARGETS_TTL_S:
            return cached[1]
        empty = Targets(None, None, [], {}, {})
        fleet = self.s.fleet
        out = empty
        if fleet is not None:
            try:
                goals = await fleet.list_goals(robot_id=robot_id, active=True, limit=5)
            except Exception as exc:  # noqa: BLE001
                log.debug("perceived: list_goals(%s) failed (%s)", robot_id, exc)
                goals = []
            goal = next((g for g in goals if g.items), None)
            if goal is not None:
                open_items = [i for i in goal.items if i.status in GOAL_ITEM_OPEN and i.sku]
                skus: list[Sku] = []
                for n, it in enumerate(open_items):
                    # the current target (first open item) + the other open LIST items (not unasked rec detours)
                    if it.sku not in skus and (n == 0 or not it.optional):
                        skus.append(it.sku)  # type: ignore[arg-type]
                skus = skus[:MAX_CLASSES]
                labels: dict[Sku, str] = {}
                short: dict[Sku, str] = {}
                for sku in skus:
                    p = await self._product(sku)
                    labels[sku] = class_string(p, sku)
                    short[sku] = _short(p, sku)
                out = Targets(goal.trip_id or goal.goal_id, skus[0] if skus else None, skus, labels, short)
        self._targets[robot_id] = (now, out)
        return out

    # ---- detection ---------------------------------------------------------------------------------------------
    def _provider_ready(self, t: Targets) -> tuple[bool, str]:
        if self.provider is None:
            return False, self.provider_note or "Perceptron off"
        if self._cooldown_until > self.clock():
            return False, f"Perceptron {self._cooldown_code or 'paused'}"
        if self._budget.left(t.trip_key, self.trip_budget) <= 0:
            return False, f"Perceptron budget spent ({self.trip_budget} calls this trip)"
        return True, ""

    async def _detect(self, cam: CameraInfo, frame: CameraFrame, st: _Stream) -> None:
        try:
            st.det = await self._run_detection(cam, frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the last boxes
            log.warning("perceived %s: detection failed (%s)", cam.camera_id, exc)

    async def _run_detection(self, cam: CameraInfo, frame: CameraFrame) -> Detection:
        t0 = self.clock()
        robot_id = cam.robot_id or (frame.robot_id or "")
        t = await self.targets(robot_id)
        target_label = t.short.get(t.target) if t.target else None
        if not t.skus:
            return Detection([], "none", "no active goal: nothing to look for", None, self.clock())
        ok, why = self._provider_ready(t)
        if ok:
            self.calls += 1
            self._budget.spend(t.trip_key)
            ans = await self.provider.detect(frame, [t.labels[s] for s in t.skus], timeout_s=self.timeout_s)
            code = ans.call.error_code
            if code is None:
                self._cooldown_code = None
                boxes = self._perceptron_boxes(ans.boxes, t)
                note = f"{len(boxes)} of your list items in view" if boxes else "no list items in view"
                return Detection(boxes, "perceptron", note, target_label, self.clock(), ans.call.latency_ms)
            self._cooldown_until = self.clock() + COOLDOWN_S.get(code, DEFAULT_COOLDOWN_S)
            self._cooldown_code = code
            why = f"Perceptron {code}"
            log.info("perceived %s: Perceptron %s; ground truth for now", cam.camera_id, code)
        boxes = await self._gt_boxes(cam, frame, t)
        return Detection(
            boxes,
            "ground_truth",
            f"ground truth (sim) · {why}",
            target_label,
            self.clock(),
            (self.clock() - t0) * 1000,
            error_code=self._cooldown_code,
        )

    @staticmethod
    def _perceptron_boxes(evidence: list[Any], t: Targets) -> list[OverlayBox]:
        out: list[OverlayBox] = []
        for e in evidence:
            if e.box is None:
                continue
            sku = e.sku or match_mention_to_sku(e.mention or "", t.labels)
            if sku is None and len(t.skus) == 1:
                sku = t.skus[0]
            if sku is None:
                continue
            b = e.box
            out.append(
                OverlayBox(
                    b.x1 / NORM_MAX,
                    b.y1 / NORM_MAX,
                    b.x2 / NORM_MAX,
                    b.y2 / NORM_MAX,
                    t.short.get(sku, sku),
                    sku,
                    float(e.confidence),
                    sku == t.target,
                )
            )
        return out

    async def _gt_boxes(self, cam: CameraInfo, frame: CameraFrame, t: Targets) -> list[OverlayBox]:
        insts: list[ItemInstance] = [i for i in frame.instances if i.bbox is not None]
        fw, fh = frame.w, frame.h
        source = cam.derived_from or perceived_source(cam.camera_id)
        if not insts and self.capture_gt is not None and source:
            try:
                raw = await self.capture_gt(source)
                insts = [i for i in raw.instances if i.bbox is not None]
                fw, fh = raw.w, raw.h
            except Exception as exc:  # noqa: BLE001 - no boxes this round
                log.debug("perceived %s: ground-truth capture failed (%s)", cam.camera_id, exc)
        wanted = set(t.skus)
        out: list[OverlayBox] = []
        labelled: set[Sku] = set()
        for i in sorted(insts, key=lambda i: (i.sku != t.target, i.instance_id)):
            if i.sku not in wanted or i.in_basket or i.bbox is None:
                continue
            b = i.bbox
            x0, y0 = max(0.0, b.x / fw), max(0.0, b.y / fh)
            x1, y1 = min(1.0, (b.x + b.w) / fw), min(1.0, (b.y + b.h) / fh)
            if x1 <= x0 or y1 <= y0:
                continue
            label = t.short.get(i.sku, i.sku) if i.sku not in labelled else ""
            labelled.add(i.sku)
            out.append(OverlayBox(x0, y0, x1, y1, label, i.sku, 1.0, i.sku == t.target))
        return [b for b in out if b.label] + [b for b in out if not b.label]


__all__ = [
    "HEADER_GT",
    "HEADER_LIVE",
    "Detection",
    "OverlayBox",
    "PerceivedStreams",
    "Targets",
    "class_string",
    "draw_overlay",
]
