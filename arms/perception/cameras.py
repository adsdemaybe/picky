"""Cameras API (``contracts.services.CameraService``): registry, blurred snapshots / live streams, ``observe``.

``create_cameras_fake(services)`` -> :class:`FakeCameras` (base deps only: pillow);
``create_cameras(services)``      -> :class:`RealCameras` (same, plus ``observe`` through the ``utils.llm`` fast model's
vision (gemini-3.8-flash by default); raises without the provider's key (``GEMINI_API_KEY``) so the gateway boots the
fake).

Registry: ``utils.data.load_cameras()`` (data/fixtures/robots.yaml: each robot's front / wrist / basket camera + the
store ceiling cameras; built-in defaults with the fixed ids if the file is missing), overlaid at call time with what
``services.sim_link.status()`` reports (cameras announced by the Isaac worker's SimHello -> ``source='isaac'``) and the
measured live fps / last frame time.

Frames: RAW frames come from ``services.robot`` (``capture_camera`` for snapshots, ``frames(camera_id=...)`` for live
streams with a 1 s stall fallback to polling ``capture_camera``). EVERY frame leaving this service has
``blurred=True`` (CLAUDE.md §8), decided in :meth:`FakeCameras._blur`:

1. ``perception.blur_faces`` (services.perception when wired, else this arm's own: YuNet in the real service, the
   synthetic-frame rule in the fake).
2. Not blurred and the camera is rendered (``source`` 'isaac' / 'fake': every camera in this system is a sim camera,
   so there is no real person to hide) -> certified ``blurred=True``.
3. Otherwise the WHOLE frame is pixelated (nothing recognisable left) before ``blurred=True`` is set; an undecodable
   frame becomes an "unavailable" placeholder. A frame is never served with an unverified ``blurred`` flag.

When a camera can't deliver (no robot service, robot error, sim gone), snapshots and streams return a rendered
placeholder frame ("Pico · front - offline") instead of raising, so an ``<img>`` grid never breaks; unknown camera ids
raise ``KeyError`` (gateway -> 404).
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from contracts.cameras import (
    DEFAULT_ROBOT_CAMERA_MOUNTS,
    ROBOT_CAMERA_KINDS,
    CameraInfo,
    CameraMount,
    ObservedItem,
    VisionObservation,
    parse_camera_id,
    robot_camera_id,
)
from contracts.catalog import Product
from contracts.common import BBox, CameraId, GoalId, RobotId, Sku, StoreId, utcnow
from contracts.planogram import Planogram, Slot
from contracts.services import PerceptionService, Services
from contracts.sim import CameraFrame, ItemInstance
from utils.ids import IdFactory
from utils.log import get_logger
from utils.money import fmt_cents

from .fake import FakePerception, basket_instances, shelf_tag_price

log = get_logger(__name__)

STREAM_STALL_S = 1.0
"""No live frame for this long -> poll ``robot.capture_camera`` at the stream's fps (contract: 1 s)."""
STREAM_FPS_MIN = 0.2
STREAM_FPS_MAX = 15.0
NEAR_SLOT_RADIUS_M = 1.0
"""Planogram slots whose approach pose is this close to the robot count as 'in front of it' (fallback description)."""
NEAR_SLOT_MAX = 4
PIXELATE_CELLS = 12
"""Fail-closed obscuring: the frame is reduced to this many cells across before being scaled back up."""
PLACEHOLDER_PREFIX = "synthetic-offline"
"""frame_id prefix of rendered placeholder frames (also matched by fake.is_synthetic_frame: 'synthetic')."""
MJPEG_BOUNDARY = "frame"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"
JPEG_QUALITY = 82

RENDERED_SOURCES = ("isaac", "fake")

_STOPWORDS = frozenset(
    "the a an is are there any how many much what see you do does can could have has stock in on at of shelf this "
    "that which for and with price its look looking left fresh freshest here near me my your robot camera item items "
    "find show tell whats where it doing around front now right currently view anything something".split()
)


# --------------------------------------------------------------------------------------------------------------------
# Frame helpers (sync, Pillow; call through asyncio.to_thread). Exported for the gateway's MJPEG / JPEG endpoints.
# --------------------------------------------------------------------------------------------------------------------
def jpeg_bytes(frame: CameraFrame) -> bytes:
    """The frame's JPEG bytes (what GET /api/cameras/{id}/snapshot.jpg serves)."""
    return base64.b64decode(frame.jpeg_b64)


def mjpeg_part(frame: CameraFrame) -> bytes:
    """One part of a ``multipart/x-mixed-replace`` (MJPEG) body; serve with ``MJPEG_CONTENT_TYPE``."""
    data = jpeg_bytes(frame)
    head = f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n".encode("ascii")
    return head + data + b"\r\n"


def _decode(jpeg_b64: str) -> Any:
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(jpeg_b64)))
    img.load()
    return img.convert("RGB")


def _encode(img: Any) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _scale_bbox(b: BBox | None, sx: float, sy: float) -> BBox | None:
    if b is None:
        return None
    return BBox(x=round(b.x * sx, 2), y=round(b.y * sy, 2), w=round(b.w * sx, 2), h=round(b.h * sy, 2))


def scale_frame(frame: CameraFrame, max_width: int | None) -> CameraFrame:
    """Downscale (aspect kept) when ``max_width`` is smaller than the frame; instance bboxes are scaled with it.
    Never upscales; an undecodable frame is returned unchanged."""
    if not max_width or max_width <= 0 or max_width >= frame.w:
        return frame
    try:
        from PIL import Image

        img = _decode(frame.jpeg_b64)
        w = max(1, int(max_width))
        h = max(1, round(img.height * w / img.width))
        small = img.resize((w, h), Image.Resampling.BILINEAR)
    except Exception as exc:
        log.debug("scale_frame: cannot decode %s (%s); serving full size", frame.frame_id, exc)
        return frame
    sx, sy = w / frame.w, h / frame.h
    instances = [i.model_copy(update={"bbox": _scale_bbox(i.bbox, sx, sy)}) for i in frame.instances]
    return frame.model_copy(update={"jpeg_b64": _encode(small), "w": w, "h": h, "instances": instances})


def obscure_frame(frame: CameraFrame) -> CameraFrame:
    """Fail-closed privacy: pixelate the WHOLE frame (no face can survive), then mark it blurred.
    Raises if the JPEG can't be decoded (the caller substitutes a placeholder)."""
    from PIL import Image, ImageDraw

    img = _decode(frame.jpeg_b64)
    cells_w = max(1, PIXELATE_CELLS)
    cells_h = max(1, round(img.height * cells_w / max(1, img.width)))
    img = img.resize((cells_w, cells_h), Image.Resampling.BILINEAR).resize(img.size, Image.Resampling.NEAREST)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, img.width, 18), fill=(20, 20, 20))
    draw.text((6, 3), "privacy: face check unavailable, frame obscured", fill=(240, 240, 240))
    return frame.model_copy(update={"jpeg_b64": _encode(img), "w": img.width, "h": img.height, "blurred": True})


def render_placeholder(camera: CameraInfo, message: str, frame_id: str) -> CameraFrame:
    """A rendered (face-free) 'camera offline' card in the camera's resolution."""
    from PIL import Image, ImageDraw

    w, h = camera.mount.width, camera.mount.height
    img = Image.new("RGB", (w, h), (28, 31, 38))
    draw = ImageDraw.Draw(img)
    step = max(16, w // 16)
    for x in range(0, w, step):
        draw.line((x, 0, x, h), fill=(36, 40, 48))
    for y in range(0, h, step):
        draw.line((0, y, w, y), fill=(36, 40, 48))
    cx, cy = w // 2, h // 2
    draw.ellipse((cx - 22, cy - 60, cx + 22, cy - 16), outline=(120, 128, 140), width=3)
    draw.line((cx - 26, cy - 64, cx + 26, cy - 12), fill=(200, 90, 80), width=3)
    for i, line in enumerate((camera.name, message)):
        tw = draw.textlength(line)
        draw.text((cx - tw / 2, cy + 4 + 18 * i), line, fill=(225, 228, 235) if i == 0 else (160, 166, 178))
    return CameraFrame(
        frame_id=frame_id,
        camera=camera.kind,
        camera_id=camera.camera_id,
        robot_id=camera.robot_id,
        store_id=camera.store_id,
        jpeg_b64=_encode(img),
        w=w,
        h=h,
        blurred=True,  # rendered by us: no faces exist
    )


def is_placeholder(frame: CameraFrame) -> bool:
    return frame.frame_id.startswith(PLACEHOLDER_PREFIX)


# --------------------------------------------------------------------------------------------------------------------
# Registry defaults (used only when data/fixtures/robots.yaml is missing / invalid)
# --------------------------------------------------------------------------------------------------------------------
_DEFAULT_ROBOTS: tuple[tuple[RobotId, str, StoreId], ...] = (
    ("rover-ave-1", "Pico", "store_ave"),
    ("rover-ave-2", "Rhea", "store_ave"),
    ("rover-hill-1", "Hazel", "store_hill"),
)
_DEFAULT_CEILING: tuple[tuple[CameraId, str, StoreId, float, float], ...] = (
    ("store_ave.ceiling-1", "Ave ceiling 1", "store_ave", 2.0, 2.0),
    ("store_ave.ceiling-2", "Ave ceiling 2", "store_ave", 10.0, 8.0),
    ("store_hill.ceiling-1", "Hill ceiling 1", "store_hill", 2.0, 2.0),
)


def default_cameras() -> list[CameraInfo]:
    """The fixed camera ids (contracts/OVERVIEW.md) with nominal mounts."""
    out: list[CameraInfo] = []
    for rid, name, sid in _DEFAULT_ROBOTS:
        for kind in ROBOT_CAMERA_KINDS:
            out.append(
                CameraInfo(
                    camera_id=robot_camera_id(rid, kind),
                    kind=kind,
                    name=f"{name} · {kind}",
                    store_id=sid,
                    robot_id=rid,
                    mount=DEFAULT_ROBOT_CAMERA_MOUNTS[kind].model_copy(),
                )
            )
    for cid, name, sid, x, y in _DEFAULT_CEILING:
        mount = CameraMount(frame="world", x=x, y=y, z=3.2, yaw=0.785, pitch=-0.9, hfov_deg=100.0)
        out.append(CameraInfo(camera_id=cid, kind="ceiling", name=name, store_id=sid, mount=mount))
    return out


def _load_registry(data_root: Any = None) -> list[CameraInfo]:
    try:
        from utils.data import load_cameras

        cams = load_cameras(data_root=data_root)
        if cams:
            return cams
        log.warning("cameras: robots.yaml lists no cameras; using the built-in defaults")
    except Exception as exc:  # missing/invalid robots.yaml must not take the cameras API down
        log.warning("cameras: cannot load data/fixtures/robots.yaml (%s); using the built-in defaults", exc)
    return default_cameras()


def _canonical_id(camera_id: CameraId) -> CameraId:
    """'<robot>.head' (v0.1 alias) -> '<robot>.front'; anything else unchanged."""
    owner, sep, name = camera_id.rpartition(".")
    return f"{owner}.front" if sep and name == "head" else camera_id


# --------------------------------------------------------------------------------------------------------------------
# Ground-truth description (fake observe; hints for the real VLM)
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class _Seen:
    """What the ground truth says is in view."""

    items: list[ObservedItem]
    source: str  # 'camera' (frame instances / aimed-at slot) | 'map' (planogram near the robot) | 'none' | 'offline'
    notes: list[str]


def _label(product: Product | None, sku: Sku) -> str:
    if product is None:
        return sku.replace("_", " ")
    label = product.name
    if product.brand and product.brand.lower() not in label.lower():
        label = f"{product.brand} {label}"
    if product.size and product.size.lower() not in label.lower():
        label = f"{label} {product.size}"
    return label


def _union_bbox(boxes: Iterable[BBox | None]) -> BBox | None:
    bs = [b for b in boxes if b is not None]
    if not bs:
        return None
    x0, y0 = min(b.x for b in bs), min(b.y for b in bs)
    x1, y1 = max(b.x + b.w for b in bs), max(b.y + b.h for b in bs)
    return BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def _price_note(slot: Slot, product: Product | None, label: str) -> str | None:
    """'tag $3.99 vs price $3.49' when the physical shelf tag disagrees with the store price."""
    if slot.shelf_tag_cents is None:
        return None
    price = slot.price_cents if slot.price_cents is not None else (product.price_cents if product else None)
    if price is None or price == slot.shelf_tag_cents:
        return None
    return (
        f"the shelf tag for {label} says {fmt_cents(slot.shelf_tag_cents)} but the store price is "
        f"{fmt_cents(price)} (possible mispricing)"
    )


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        out.add(w)
        if w.endswith("es") and len(w) > 4:
            out.add(w[:-2])
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
    return out


def match_items(question: str, items: list[ObservedItem]) -> list[ObservedItem]:
    """Items whose label / sku shares a meaningful word with the question ('mild salsa' -> the mild salsa jar)."""
    q = _tokens(question)
    if not q:
        return []
    return [it for it in items if q & _tokens(f"{it.label} {(it.sku or '').replace('_', ' ')}")]


def _phrase(it: ObservedItem) -> str:
    s = f"{it.count} × {it.label}"
    extra = []
    if it.shelf_tag_cents is not None:
        extra.append(f"tag {fmt_cents(it.shelf_tag_cents)}")
    if it.freshness is not None:
        extra.append(f"freshness {it.freshness:.2f}")
    if it.slot_id:
        extra.append(f"slot {it.slot_id}")
    return s + (f" ({', '.join(extra)})" if extra else "")


_WHERE = {
    "front": "in front of me",
    "wrist": "in my wrist camera",
    "basket": "in my basket",
    "ceiling": "from the ceiling camera",
}


def compose_answer(camera: CameraInfo, seen: _Seen, question: str | None) -> str:
    """Deterministic natural-language answer from ground truth (fake observe; also the real one's fallback)."""
    where = _WHERE.get(camera.kind, "in view")
    if seen.source == "offline":
        return f"{camera.name} is offline right now, so I can't see anything."
    if seen.source == "map":
        summary = (
            "I can't make out individual products in this frame; per the store map, nearby: "
            + "; ".join(_phrase(it) for it in seen.items)
            + "."
        )
    elif seen.items:
        present = [it for it in seen.items if it.count > 0]
        empty = [it for it in seen.items if it.count == 0]
        parts = []
        if present:
            parts.append(f"I see {'; '.join(_phrase(it) for it in present)} {where}.")
        for it in empty:
            parts.append(f"The {it.label} spot{f' ({it.slot_id})' if it.slot_id else ''} is empty.")
        summary = " ".join(parts)
    else:
        summary = f"I don't see any products {where}."
    notes = "".join(f" Note: {n}." for n in seen.notes)

    if not question or not question.strip() or not _tokens(question):  # "what do you see?" = describe the view
        return summary + notes
    matched = match_items(question, seen.items)
    if not matched:
        return f"I don't see anything matching “{question.strip()}”. {summary}{notes}"
    in_stock = [it for it in matched if it.count > 0]
    if in_stock:
        lead = "Yes: " + "; ".join(_phrase(it) for it in in_stock) + "."
        if seen.source == "map":
            lead += " (from the store map, not confirmed by the camera)"
    else:
        lead = "No: " + "; ".join(f"the {it.label} spot is empty" for it in matched) + "."
    rel = [n for n in seen.notes if any(it.label in n for it in matched)]
    return lead + "".join(f" Note: {n}." for n in rel)


# --------------------------------------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class _Stats:
    last_frame_at: datetime | None = None
    last_mono: float | None = None
    fps: float | None = None


class FakeCameras:
    """CameraService with deterministic ground-truth ``observe``. Frames come from ``services.robot``."""

    def __init__(self, services: Services | None = None, *, data_root: Any = None, seed: int | str = "cameras") -> None:
        self._services = services or Services()
        self._data_root = data_root
        self._ids = IdFactory(seed)
        self._gt = FakePerception(self._services, seed=f"{seed}-gt")
        self._registry: dict[CameraId, CameraInfo] | None = None
        self._stats: dict[CameraId, _Stats] = {}
        self._placeholders = 0

    # ---- registry --------------------------------------------------------------------------------------------------
    async def _base(self) -> dict[CameraId, CameraInfo]:
        if self._registry is None:
            cams = await asyncio.to_thread(_load_registry, self._data_root)
            self._registry = {c.camera_id: c for c in cams}
        return self._registry

    def _sim_overlay(self) -> tuple[dict[CameraId, CameraInfo], set[RobotId], set[StoreId], set[CameraId]]:
        sim = self._services.sim_link
        cams: dict[CameraId, CameraInfo] = {}
        robots: set[RobotId] = set()
        stores: set[StoreId] = set()
        streaming: set[CameraId] = set()
        if sim is None:
            return cams, robots, stores, streaming
        try:
            status = sim.status()
        except Exception as exc:
            log.debug("cameras: sim_link.status() failed (%s)", exc)
            return cams, robots, stores, streaming
        for w in getattr(status, "workers", []) or []:
            if not getattr(w, "connected", False):
                continue
            for c in w.cameras:
                cams[c.camera_id] = c
            streaming |= set(w.streaming_camera_ids)
        robots |= set(getattr(status, "isaac_robot_ids", []) or [])
        stores |= set(getattr(status, "isaac_store_ids", []) or [])
        return cams, robots, stores, streaming

    async def _cameras(self) -> dict[CameraId, CameraInfo]:
        base = await self._base()
        isaac_cams, isaac_robots, isaac_stores, _streaming = self._sim_overlay()
        robot_wired = self._services.robot is not None
        out: dict[CameraId, CameraInfo] = {}
        for cid, info in [*base.items(), *((c, i) for c, i in isaac_cams.items() if c not in base)]:
            on_isaac = (
                cid in isaac_cams
                or (info.robot_id is not None and info.robot_id in isaac_robots)
                or (info.robot_id is None and info.store_id in isaac_stores)
            )
            if on_isaac:
                upd: dict[str, Any] = {"source": "isaac", "online": True}
            elif robot_wired:
                upd = {"source": "fake", "online": True}
            else:
                upd = {"source": "none", "online": False}
            st = self._stats.get(cid)
            if st is not None:
                upd["last_frame_at"] = st.last_frame_at
                upd["fps"] = round(st.fps, 2) if st.fps is not None else None
            out[cid] = info.model_copy(update=upd)
        return out

    async def list_cameras(self, store_id: StoreId | None = None, robot_id: RobotId | None = None) -> list[CameraInfo]:
        cams = await self._cameras()
        return [
            c
            for c in cams.values()
            if (store_id is None or c.store_id == store_id) and (robot_id is None or c.robot_id == robot_id)
        ]

    async def get_camera(self, camera_id: CameraId) -> CameraInfo:
        cams = await self._cameras()
        cam = cams.get(camera_id) or cams.get(_canonical_id(camera_id))
        if cam is None:
            raise KeyError(f"unknown camera {camera_id!r}")
        return cam

    # ---- frames ----------------------------------------------------------------------------------------------------
    def _placeholder(self, cam: CameraInfo, message: str) -> CameraFrame:
        self._placeholders += 1
        return render_placeholder(cam, message, f"{PLACEHOLDER_PREFIX}-{cam.camera_id}-{self._placeholders:05d}")

    async def _capture_raw(self, cam: CameraInfo) -> CameraFrame:
        robot = self._services.robot
        if robot is None:
            return await asyncio.to_thread(self._placeholder, cam, "offline (no robot service)")
        try:
            return await robot.capture_camera(cam.camera_id)
        except Exception as exc:  # RobotError (unknown camera, disconnected, ...), timeouts: degrade to a card
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("cameras: capture %s failed (%s: %s)", cam.camera_id, code, exc)
            return await asyncio.to_thread(self._placeholder, cam, f"offline ({code})")

    def _fallback_blurrer(self) -> PerceptionService:
        return self._gt

    async def _blur(self, frame: CameraFrame, cam: CameraInfo) -> CameraFrame:
        """Return a frame that is safe to serve (``blurred=True``, see module docstring for the rule)."""
        if frame.blurred:
            return frame
        blurrer = self._services.perception or self._fallback_blurrer()
        out = frame
        try:
            out = await blurrer.blur_faces(frame)
        except Exception as exc:
            log.warning("cameras: blur_faces failed on %s (%s)", frame.frame_id, exc)
        if out.blurred:
            return out
        if cam.source in RENDERED_SOURCES:
            return out.model_copy(update={"blurred": True})  # rendered by Isaac / the 2D fake: no real faces exist
        try:
            log.warning("cameras: %s (%s) not face-checked; pixelating the whole frame", frame.frame_id, cam.camera_id)
            return await asyncio.to_thread(obscure_frame, out)
        except Exception as exc:
            log.warning("cameras: cannot obscure %s (%s); serving a placeholder", frame.frame_id, exc)
            return await asyncio.to_thread(self._placeholder, cam, "frame withheld (privacy)")

    def _record(self, camera_id: CameraId, frame: CameraFrame) -> None:
        st = self._stats.setdefault(camera_id, _Stats())
        now = time.monotonic()
        if st.last_mono is not None:
            dt = now - st.last_mono
            if dt > 0:
                inst = 1.0 / dt
                st.fps = inst if st.fps is None else 0.7 * st.fps + 0.3 * inst
        st.last_mono = now
        st.last_frame_at = utcnow()

    async def _serve(self, raw: CameraFrame, cam: CameraInfo, max_width: int | None) -> CameraFrame:
        upd: dict[str, Any] = {}
        if raw.camera_id is None:
            upd["camera_id"] = cam.camera_id
        if raw.store_id is None and cam.store_id is not None:
            upd["store_id"] = cam.store_id
        if raw.robot_id is None and cam.robot_id is not None:
            upd["robot_id"] = cam.robot_id
        frame = raw.model_copy(update=upd) if upd else raw
        safe = await self._blur(frame, cam)
        if max_width:
            safe = await asyncio.to_thread(scale_frame, safe, max_width)
        self._record(cam.camera_id, safe)
        return safe

    async def snapshot(self, camera_id: CameraId, max_width: int | None = None) -> CameraFrame:
        cam = await self.get_camera(camera_id)
        return await self._serve(await self._capture_raw(cam), cam, max_width)

    def _open_live(self, camera_id: CameraId) -> tuple[AsyncIterator[CameraFrame] | None, bool]:
        """robot.frames(camera_id=...) -> (iterator, needs_filter). (None, False) when there is no live source."""
        robot = self._services.robot
        if robot is None or not hasattr(robot, "frames"):
            return None, False
        try:
            return robot.frames(camera_id=camera_id), False
        except TypeError:  # pre-0.2 robot: frames() without filters
            try:
                return robot.frames(), True
            except Exception as exc:
                log.debug("cameras: robot.frames() failed (%s)", exc)
        except Exception as exc:
            log.debug("cameras: robot.frames(%s) failed (%s)", camera_id, exc)
        return None, False

    @staticmethod
    async def _pump(
        source: AsyncIterator[CameraFrame], queue: asyncio.Queue[CameraFrame], camera_id: CameraId, filt: bool
    ) -> None:
        """Copy live frames into a 1-slot queue (latest frame wins)."""
        try:
            async for f in source:
                if filt and f.camera_id not in (None, camera_id):
                    continue
                if queue.full():
                    with suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(f)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("cameras: live source for %s ended (%s); polling", camera_id, exc)

    async def stream(
        self, camera_id: CameraId, fps: float = 5.0, max_width: int | None = None
    ) -> AsyncIterator[CameraFrame]:
        cam = await self.get_camera(camera_id)
        fps = min(max(float(fps or 0.0), STREAM_FPS_MIN), STREAM_FPS_MAX)
        interval = 1.0 / fps
        source, filt = self._open_live(cam.camera_id)
        queue: asyncio.Queue[CameraFrame] = asyncio.Queue(maxsize=1)
        pump = asyncio.create_task(self._pump(source, queue, cam.camera_id, filt)) if source is not None else None
        polling = pump is None
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        try:
            while True:
                frame: CameraFrame | None = None
                if not queue.empty():
                    frame = queue.get_nowait()
                elif not polling and pump is not None and not pump.done():
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=STREAM_STALL_S)
                    except TimeoutError:
                        log.info("cameras: no live frame from %s in %.1fs; polling", cam.camera_id, STREAM_STALL_S)
                polling = frame is None
                if frame is None:
                    frame = await self._capture_raw(cam)
                yield await self._serve(frame, cam, max_width)
                next_t += interval
                delay = next_t - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_t = loop.time()
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(BaseException):
                    await pump
            aclose = getattr(source, "aclose", None)
            if aclose is not None:
                with suppress(BaseException):
                    await aclose()

    # ---- observe ---------------------------------------------------------------------------------------------------
    async def _ground_truth(self, cam: CameraInfo, frame: CameraFrame) -> _Seen:
        if is_placeholder(frame):
            return _Seen([], "offline", [])
        store_id = frame.store_id or cam.store_id
        plan: Planogram | None = await self._gt._planogram(store_id) if store_id else None
        if cam.kind == "basket":
            insts: list[ItemInstance] = basket_instances(frame)
        else:
            insts = [i for i in frame.instances if not i.in_basket]

        groups: dict[tuple[Sku, str | None], list[ItemInstance]] = {}
        for i in sorted(insts, key=lambda i: (i.slot_id or "", i.sku, i.instance_id)):
            groups.setdefault((i.sku, None if cam.kind == "basket" else i.slot_id), []).append(i)

        aimed = plan.slot(frame.slot_id) if (plan is not None and frame.slot_id) else None
        near: list[Slot] = []
        if not groups and aimed is None and plan is not None and frame.pose is not None and cam.kind != "basket":
            pose = frame.pose
            near = sorted(
                (s for s in plan.slots if s.approach_pose.distance_to(pose) <= NEAR_SLOT_RADIUS_M),
                key=lambda s: (s.approach_pose.distance_to(pose), s.slot_id),
            )[:NEAR_SLOT_MAX]

        skus = {k[0] for k in groups} | ({aimed.sku} if aimed else set()) | {s.sku for s in near}
        products = await self._gt._products(skus)
        items: list[ObservedItem] = []
        notes: list[str] = []
        for (sku, slot_id), group in groups.items():
            slot = plan.slot(slot_id) if (plan is not None and slot_id) else None
            product = products.get(sku)
            label = _label(product, sku)
            fresh = [i.freshness for i in group if i.freshness is not None]
            items.append(
                ObservedItem(
                    label=label,
                    sku=sku,
                    count=len(group),
                    confidence=1.0,
                    bbox=_union_bbox(i.bbox for i in group),
                    slot_id=slot_id,
                    shelf_tag_cents=shelf_tag_price(slot, product) if slot is not None else None,
                    freshness=round(sum(fresh) / len(fresh), 3) if fresh else None,
                )
            )
            if slot is not None and (note := _price_note(slot, product, label)):
                notes.append(note)
        if groups:
            return _Seen(items, "camera", notes)
        if aimed is not None:  # aimed at a slot and the sim reported nothing there: truly empty
            product = products.get(aimed.sku)
            label = _label(product, aimed.sku)
            items.append(
                ObservedItem(
                    label=label,
                    sku=aimed.sku,
                    count=0,
                    slot_id=aimed.slot_id,
                    shelf_tag_cents=shelf_tag_price(aimed, product),
                )
            )
            if note := _price_note(aimed, product, label):
                notes.append(note)
            return _Seen(items, "camera", notes)
        for s in near:
            product = products.get(s.sku)
            label = _label(product, s.sku)
            items.append(
                ObservedItem(
                    label=label,
                    sku=s.sku,
                    count=s.qty,
                    confidence=0.6,
                    slot_id=s.slot_id,
                    shelf_tag_cents=shelf_tag_price(s, product),
                )
            )
        return _Seen(items, "map" if items else "none", notes)

    async def _describe(self, cam: CameraInfo, frame: CameraFrame, question: str | None) -> VisionObservation:
        seen = await self._ground_truth(cam, frame)
        return self._observation(cam, frame, question, seen)

    def _observation(
        self, cam: CameraInfo, frame: CameraFrame, question: str | None, seen: _Seen, **upd: Any
    ) -> VisionObservation:
        method = "ground_truth" if seen.source in ("camera", "map") else "fake"
        obs = VisionObservation(
            observation_id=self._ids("obs"),
            camera_id=cam.camera_id,
            robot_id=frame.robot_id or cam.robot_id,
            store_id=frame.store_id or cam.store_id,
            frame_id=frame.frame_id,
            question=question,
            answer=compose_answer(cam, seen, question),
            items=seen.items,
            people=0,  # rendered frames contain no people
            blurred=True,
            method=method,
        )
        return obs.model_copy(update=upd) if upd else obs

    async def observe(
        self, camera_id: CameraId, question: str | None = None, *, goal_id: GoalId | None = None
    ) -> VisionObservation:
        t0 = time.perf_counter()
        cam = await self.get_camera(camera_id)
        frame = await self.snapshot(cam.camera_id)
        question = question.strip() if question and question.strip() else None
        try:
            obs = await self._describe(cam, frame, question)
        except Exception as exc:  # never fail the brain's look tool on a description bug
            log.warning("cameras: observe %s failed (%s)", camera_id, exc)
            obs = self._observation(cam, frame, question, _Seen([], "none", []))
        return obs.model_copy(update={"goal_id": goal_id, "latency_ms": round((time.perf_counter() - t0) * 1000, 3)})


# --------------------------------------------------------------------------------------------------------------------
# Real: claude-haiku-5-5 vision on the blurred frame, ground truth as hints (sim) and as the fallback
# --------------------------------------------------------------------------------------------------------------------
class _VisionItem(BaseModel):
    label: str = Field(description="Product in words, e.g. 'Casa Verde Mild Salsa 16 oz'.")
    sku: str | None = Field(None, description="SKU from the hints when it matches; otherwise null.")
    count: int = Field(1, description="Visible units (approximate).")
    confidence: float = 0.8
    shelf_tag_cents: int | None = Field(None, description="Price on the shelf tag in cents, if readable.")
    freshness: float | None = Field(None, description="Produce only, 0..1 (1 = perfectly fresh).")


class _VisionAnswer(BaseModel):
    answer: str = Field(description="Short spoken-style answer to the question (or a description if no question).")
    items: list[_VisionItem] = Field(default_factory=list)
    people: int = Field(0, description="Number of people visible (faces are already blurred).")


VISION_SYSTEM = (
    "You are the eyes of Picky, a small grocery-store robot (a rover with a lift and an SO-101 arm). You get one "
    "camera frame (faces already blurred) plus, in simulation, ground-truth hints about what is in view. Answer the "
    "question briefly and concretely for a shopper or store staff. Trust the hints for SKUs and unit counts (you "
    "cannot reliably count identical items); use the image for anything the hints don't cover (shelf tags, "
    "freshness, gaps, misplaced items). Never identify or describe people beyond a count. Reply with JSON only."
)


def _clamp01(v: float | None) -> float | None:
    return None if v is None else max(0.0, min(1.0, float(v)))


class RealCameras(FakeCameras):
    """FakeCameras + ``observe`` through Claude vision (``utils.llm.model('fast')`` = claude-haiku-5-5)."""

    def __init__(self, services: Services | None = None, *, client: Any = None, **kw: Any) -> None:
        super().__init__(services, **kw)
        self._client = client
        self._real_blurrer: PerceptionService | None = None

    def _fallback_blurrer(self) -> PerceptionService:
        if self._real_blurrer is None:
            from .service import RealPerception

            self._real_blurrer = RealPerception(self._services)
        return self._real_blurrer

    def _messages(self, cam: CameraInfo, frame: CameraFrame, question: str | None, seen: _Seen) -> list[dict]:
        hints = (
            "\n".join(
                f"- sku={it.sku} label={it.label!r} count={it.count}"
                + (f" slot={it.slot_id}" if it.slot_id else "")
                + (f" shelf_tag_cents={it.shelf_tag_cents}" if it.shelf_tag_cents is not None else "")
                + (f" freshness={it.freshness}" if it.freshness is not None else "")
                for it in seen.items
            )
            or "- (none)"
        )
        hint_kind = {"camera": "sim ground truth", "map": "store map near the robot (not confirmed)"}.get(
            seen.source, "none"
        )
        text = (
            f"Camera: {cam.name} ({cam.kind}). Store: {frame.store_id or cam.store_id}.\n"
            f"Hints ({hint_kind}):\n{hints}\n"
            + (f"Known issues: {'; '.join(seen.notes)}\n" if seen.notes else "")
            + f"Question: {question or 'Describe what you see that matters for shopping or restocking.'}"
        )
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame.jpeg_b64}},
                    {"type": "text", "text": text},
                ],
            }
        ]

    def _merge(self, seen: _Seen, vis: _VisionAnswer) -> list[ObservedItem]:
        truth = {it.sku: it for it in seen.items if it.sku} if seen.source == "camera" else {}
        hinted = {it.sku for it in seen.items if it.sku}
        out: list[ObservedItem] = []
        used: set[str] = set()
        for v in vis.items:
            sku = v.sku if v.sku in hinted else None
            gt = truth.get(sku) if sku else None
            if sku and sku in used:
                continue
            if sku:
                used.add(sku)
            out.append(
                ObservedItem(
                    label=gt.label if gt else v.label,
                    sku=sku,
                    count=gt.count if gt else max(0, int(v.count)),
                    confidence=_clamp01(v.confidence) or 0.0,
                    bbox=gt.bbox if gt else None,
                    slot_id=gt.slot_id if gt else None,
                    shelf_tag_cents=v.shelf_tag_cents if v.shelf_tag_cents is not None else (gt and gt.shelf_tag_cents),
                    freshness=_clamp01(v.freshness) if v.freshness is not None else (gt.freshness if gt else None),
                )
            )
        out.extend(it for it in truth.values() if it.sku not in used)  # sim truth the model didn't mention
        return out

    async def _describe(self, cam: CameraInfo, frame: CameraFrame, question: str | None) -> VisionObservation:
        seen = await self._ground_truth(cam, frame)
        if seen.source == "offline" or not frame.blurred:  # never send an unblurred frame to an LLM
            return self._observation(cam, frame, question, seen)
        from utils import llm

        try:
            vis = await llm.complete_json(
                self._messages(cam, frame, question, seen),
                _VisionAnswer,
                system=VISION_SYSTEM,
                kind="fast",
                max_tokens=1024,
                client=self._client,
            )
        except Exception as exc:  # no key, overload, malformed output: the ground-truth answer stands
            log.warning("cameras: vision call failed for %s (%s); using ground truth", cam.camera_id, exc)
            return self._observation(cam, frame, question, seen)
        return self._observation(
            cam,
            frame,
            question,
            seen,
            answer=vis.answer.strip() or compose_answer(cam, seen, question),
            items=self._merge(seen, vis),
            people=max(0, int(vis.people)),
            method="vlm",
            model=llm.model("fast"),
        )


def create_cameras_fake(services: Services) -> FakeCameras:
    return FakeCameras(services)


def create_cameras(services: Services) -> RealCameras:
    from utils import llm

    if not llm.is_configured():
        reason = llm.not_configured_reason() or "no LLM configured"
        raise llm.LLMNotConfigured(f"cameras: {reason}; the fake (ground-truth observe) runs instead")
    return RealCameras(services)


__all__ = [
    "MJPEG_BOUNDARY",
    "MJPEG_CONTENT_TYPE",
    "FakeCameras",
    "RealCameras",
    "compose_answer",
    "create_cameras",
    "create_cameras_fake",
    "default_cameras",
    "is_placeholder",
    "jpeg_bytes",
    "match_items",
    "mjpeg_part",
    "obscure_frame",
    "parse_camera_id",
    "render_placeholder",
    "scale_frame",
]
