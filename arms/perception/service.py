"""Real perception: CV cascade (zxing-cpp barcode -> SigLIP 2 embedding -> claude-haiku-5-5 vision) + YuNet face blur.

Heavy deps live in the ``perception`` extra (``uv sync --extra perception``) and are imported lazily, so this module
imports with base deps only. Whatever isn't available (or isn't built yet) degrades to the ground-truth bypass in
``fake.py``: the pipeline never stalls on CV.

TODO (numbered; maps to the CLAUDE.md §3 perception spec / README scope checklist):
  1. [P0] Face blur on every frame: YuNet ``cv2.FaceDetectorYN`` + Gaussian blur, 20% padded boxes.        (DONE, below)
  2. [P0] Barcode read (zxing-cpp) on each instance crop / the whole frame -> sku via the product barcode.  (DONE)
  3. [P1] SigLIP 2 (``hf-hub:timm/ViT-B-16-SigLIP2``) gallery of our label textures; cosine match per crop.
          Build the gallery once (lazy), device 'mps' on Mac else cpu; fallback ViT-B-32 laion2b_s34b_b79k.
  4. [P1] Claude vision (``utils.llm.model('fast')`` = claude-haiku-5-5): pick-from-candidates JSON on BLURRED
          crops only (>= 200 px; images before text). Never use it to count identical items.
  5. [P1] Produce freshness thumbnails: face-free JPEG crops of the top-3 from the blurred frame.         (DONE)
  6. [P2] Freshness from pixels: one multi-image Claude call ranking 'Candidate N' crops, blended 0.6/0.4 with an
          HSV hue/brown-pixel heuristic; ground-truth freshness stays the P1 path.
  7. [P2] Basket verify from pixels: crop the basket cam per item, run the cascade, multiset-diff vs the cart.
  8. [P2] Shelf-tag OCR for mispricing (Claude vision on the tag crop), feeding ScanReport.shelf_tag_cents.
"""

import asyncio
import base64
import io
import time
from pathlib import Path
from typing import Any

from contracts.common import BBox, Sku, SlotId, StoreId
from contracts.perception import BasketCheck, Detection, PickChoice, ScanReport
from contracts.services import Services
from contracts.sim import CameraFrame
from utils.config import settings
from utils.log import get_logger

from .fake import FakePerception, is_synthetic_frame

log = get_logger(__name__)

YUNET_MODEL = "models/face_detection_yunet_2023mar.onnx"
"""Repo-relative path; download with the curl line in arms/perception/README.md."""
FACE_SCORE_THRESHOLD = 0.7
FACE_PAD = 0.2
THUMB_MAX_SIDE = 160


class CascadeStageUnavailable(Exception):
    """A cascade stage can't run here (dep missing / not built yet); the cascade moves on."""


# --------------------------------------------------------------------------------------------------------------------
# Image helpers (sync; call via asyncio.to_thread)
# --------------------------------------------------------------------------------------------------------------------
def _decode_pil(jpeg_b64: str) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise CascadeStageUnavailable("pillow not installed (uv sync --extra perception)") from exc
    img = Image.open(io.BytesIO(base64.b64decode(jpeg_b64)))
    return img.convert("RGB")


def _crop(img: Any, bbox: BBox | None) -> Any:
    if bbox is None:
        return img
    x0, y0 = max(0, int(bbox.x)), max(0, int(bbox.y))
    x1, y1 = min(img.width, int(bbox.x + bbox.w)), min(img.height, int(bbox.y + bbox.h))
    if x1 <= x0 or y1 <= y0:
        return img
    return img.crop((x0, y0, x1, y1))


def _thumbnail_b64(jpeg_b64: str, bbox: BBox | None, max_side: int = THUMB_MAX_SIDE) -> str | None:
    """JPEG crop of ``bbox`` scaled to fit ``max_side``. Caller guarantees the source frame is already blurred."""
    try:
        crop = _crop(_decode_pil(jpeg_b64), bbox)
        crop.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        log.debug("thumbnail failed: %s", exc)
        return None


class _YuNet:
    """Lazy YuNet face detector + blur. ``available`` is False when cv2 or the model file is missing."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self._detector: Any = None
        self._size: tuple[int, int] | None = None
        self.available = True
        self.reason = ""

    def _ensure(self, w: int, h: int) -> Any:
        try:
            import cv2
        except ImportError as exc:
            self.available, self.reason = False, "opencv not installed (uv sync --extra perception)"
            raise CascadeStageUnavailable(self.reason) from exc
        if not self.model_path.exists():
            self.available, self.reason = False, f"missing {self.model_path}"
            raise CascadeStageUnavailable(self.reason)
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(
                str(self.model_path), "", (w, h), score_threshold=FACE_SCORE_THRESHOLD, nms_threshold=0.3, top_k=5000
            )
        if self._size != (w, h):
            self._detector.setInputSize((w, h))
            self._size = (w, h)
        return self._detector

    def blur_jpeg(self, jpeg_b64: str) -> tuple[str, int]:
        """Return (blurred jpeg_b64, faces found). Raises CascadeStageUnavailable / ValueError."""
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg_b64), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("frame is not a decodable JPEG")
        h, w = img.shape[:2]
        _, faces = self._ensure(w, h).detect(img)
        n = 0 if faces is None else len(faces)
        for face in faces if faces is not None else []:
            x, y, bw, bh = (float(v) for v in face[:4])
            px, py = bw * FACE_PAD, bh * FACE_PAD
            x0, y0 = max(0, int(x - px)), max(0, int(y - py))
            x1, y1 = min(w, int(x + bw + px)), min(h, int(y + bh + py))
            if x1 > x0 and y1 > y0:
                roi = img[y0:y1, x0:x1]
                sigma = max(5.0, (x1 - x0) / 6)
                img[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (0, 0), sigma)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise ValueError("jpeg encode failed")
        return base64.b64encode(buf.tobytes()).decode("ascii"), n


# --------------------------------------------------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------------------------------------------------
class RealPerception:
    """PerceptionService with real CV where available and the ground-truth bypass everywhere else."""

    def __init__(self, services: Services | None = None, yunet_model: str | Path | None = None) -> None:
        self._services = services or Services()
        self._gt = FakePerception(self._services, seed="perception-real")
        path = Path(yunet_model or YUNET_MODEL)
        self._yunet = _YuNet(path if path.is_absolute() else settings.repo_root / path)
        self._barcode_index: dict[str, Sku] | None = None

    # ---- 1. face blur ----------------------------------------------------------------------------------------------
    async def blur_faces(self, frame: CameraFrame) -> CameraFrame:
        if frame.blurred:
            return frame.model_copy()
        if self._yunet.available:
            try:
                jpeg, n = await asyncio.to_thread(self._yunet.blur_jpeg, frame.jpeg_b64)
                if n:
                    log.info("blurred %d face(s) in %s", n, frame.frame_id)
                return frame.model_copy(update={"jpeg_b64": jpeg, "blurred": True})
            except CascadeStageUnavailable as exc:
                log.warning("YuNet unavailable (%s); falling back to the synthetic-frame rule", exc)
            except Exception as exc:
                log.warning("YuNet failed on %s (%s); falling back to the synthetic-frame rule", frame.frame_id, exc)
        # Fail closed: only rendered frames may be certified without a detector (see fake.is_synthetic_frame).
        return await self._gt.blur_faces(frame)

    # ---- 2-4. identify cascade -------------------------------------------------------------------------------------
    async def identify(
        self,
        frame: CameraFrame,
        store_id: StoreId,
        slot_id: SlotId | None = None,
        expected_sku: Sku | None = None,
    ) -> ScanReport:
        t0 = time.perf_counter()
        report = await self._gt.identify(frame, store_id, slot_id=slot_id, expected_sku=expected_sku)
        try:
            index = await self._get_barcode_index()
            cv = await asyncio.to_thread(self._run_cascade, frame, report.detections, index)
        except CascadeStageUnavailable as exc:
            log.debug("perception cascade unavailable (%s); using ground truth", exc)
            return report
        except Exception as exc:  # any CV failure -> the ground-truth report stands
            log.warning("perception cascade failed on %s (%s); using ground truth", frame.frame_id, exc)
            return report
        if not cv:
            return report
        merged = []
        for det in report.detections:
            hit = cv.get(det.instance_id or "")
            if hit is not None and hit.sku == det.sku:
                merged.append(det.model_copy(update={"method": hit.method, "text": hit.text or det.text}))
            else:
                merged.append(det)  # CV miss or disagreement -> keep ground truth (logged below)
                if hit is not None:
                    log.warning("cv/gt disagree on %s: cv=%s gt=%s", det.instance_id, hit.sku, det.sku)
        if not report.detections:  # no ground truth: CV is all we have (visible facings only -> non-exact)
            merged = list(cv.values())
            observed = sum(1 for d in merged if d.sku == report.expected_sku)
            report = report.model_copy(update={"observed_count": observed, "count_is_exact": False})
        n_cv = sum(1 for d in merged if d.method != "ground_truth")
        return report.model_copy(
            update={
                "detections": merged,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3),
                "notes": "; ".join(x for x in (report.notes, f"cv {n_cv}/{len(merged)}") if x),
            }
        )

    async def _get_barcode_index(self) -> dict[str, Sku]:
        if self._barcode_index is None:
            from utils.data import load_products

            products = await asyncio.to_thread(load_products)
            self._barcode_index = {p.barcode: sku for sku, p in products.items()}
        return self._barcode_index

    def _run_cascade(self, frame: CameraFrame, gt: list[Detection], index: dict[str, Sku]) -> dict[str, Detection]:
        """Per crop: barcode -> embedding -> VLM; first answer wins. Keyed by instance_id ('' = whole frame)."""
        img = _decode_pil(frame.jpeg_b64)
        crops = [(d.instance_id or "", d.bbox) for d in gt] or [("", None)]
        out: dict[str, Detection] = {}
        for key, bbox in crops:
            crop = _crop(img, bbox)
            for stage in (self._stage_barcode, self._stage_embedding, self._stage_vlm):
                try:
                    det = stage(crop, index)
                except (CascadeStageUnavailable, NotImplementedError):
                    continue
                if det is not None:
                    out[key] = det.model_copy(update={"bbox": bbox, "instance_id": key or None})
                    break
        return out

    def _stage_barcode(self, crop: Any, index: dict[str, Sku]) -> Detection | None:
        try:
            import zxingcpp
        except ImportError as exc:
            raise CascadeStageUnavailable("zxing-cpp not installed") from exc
        for result in zxingcpp.read_barcodes(crop):
            sku = index.get(result.text)
            if sku is not None:
                return Detection(sku=sku, confidence=1.0, method="barcode", text=result.text)
        return None

    def _stage_embedding(self, crop: Any, index: dict[str, Sku]) -> Detection | None:
        raise NotImplementedError("TODO 3: SigLIP 2 label-texture gallery match")

    def _stage_vlm(self, crop: Any, index: dict[str, Sku]) -> Detection | None:
        raise NotImplementedError("TODO 4: claude-haiku-5-5 pick-from-candidates on blurred crops")

    # ---- 5-6. freshness --------------------------------------------------------------------------------------------
    async def rank_freshness(self, frame: CameraFrame, slot_id: SlotId, sku: Sku, k: int = 3) -> PickChoice:
        choice = await self._gt.rank_freshness(frame, slot_id, sku, k=k)  # P1: ground-truth freshness (TODO 6 = P2)
        safe = await self.blur_faces(frame)
        if not safe.blurred:
            return choice  # never ship a thumbnail from an unblurred frame
        cands = []
        for c in choice.candidates:
            thumb = await asyncio.to_thread(_thumbnail_b64, safe.jpeg_b64, c.bbox)
            cands.append(c.model_copy(update={"thumbnail_b64": thumb}))
        return choice.model_copy(update={"candidates": cands})

    # ---- 7. basket -------------------------------------------------------------------------------------------------
    async def verify_basket(self, frame: CameraFrame, expected: dict[Sku, int]) -> BasketCheck:
        # TODO 7: crop the basket cam per item and run the cascade; ground truth until then.
        return await self._gt.verify_basket(frame, expected)

    @property
    def face_blur_status(self) -> str:
        return "yunet" if self._yunet.available else f"synthetic-only ({self._yunet.reason or 'not checked yet'})"


def create(services: Services) -> RealPerception:
    return RealPerception(services)


__all__ = ["RealPerception", "create", "is_synthetic_frame"]
