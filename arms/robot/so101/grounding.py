"""Natural-language grounding with Perceptron (Mk1.5 / Isaac): sentence + frame -> pixel targets.

Perceptron models are perception VLMs (points/boxes), not action models; the arm motion is done by `kinematics.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

_PICK_RE = re.compile(
    r"(?:pick(?:\s+up)?|grab|get|take)\s+(?:the\s+|a\s+|an\s+)?(?P<target>.+?)"
    r"(?:\s+(?:and\s+)?(?:put|place|drop)\s+(?:it\s+)?(?:in|into|on)\s+(?:the\s+)?(?P<dest>.+?))?\s*[.!]?$",
    re.IGNORECASE,
)


@dataclass
class Detection:
    label: str
    box_px: tuple[int, int, int, int]  # x0, y0, x1, y1

    @property
    def center_px(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.box_px
        return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def parse_command(text: str) -> tuple[str, str]:
    """'pick up the red soda can and put it in the basket' -> ('red soda can', 'basket')."""
    m = _PICK_RE.search(text.strip())
    if not m:
        return text.strip(), "basket"
    return m.group("target").strip(), (m.group("dest") or "basket").strip()


def _to_pixels(label: str, box1000, w: int, h: int) -> Detection:
    x0, y0, x1, y1 = box1000  # Perceptron / Isaac boxes are on a 0..1000 normalized grid
    return Detection(label, (int(x0 * w / 1000), int(y0 * h / 1000), int(x1 * w / 1000), int(y1 * h / 1000)))


def detect_local(frame_bgr: np.ndarray, labels: list[str], url: str, timeout: float = 30.0) -> dict[str, Detection]:
    """Self-hosted Isaac-0.2-1B (see vlm_server.py): no API key or quota, ~0.6 s per label on an RTX 5090."""
    import base64
    import json
    import urllib.request

    import cv2

    ok, jpg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("could not JPEG-encode the frame")
    body = json.dumps({"image_jpeg_b64": base64.b64encode(jpg.tobytes()).decode(), "classes": labels}).encode()
    req = urllib.request.Request(f"{url.rstrip('/')}/detect", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        boxes = json.loads(resp.read())["boxes"]
    h, w = frame_bgr.shape[:2]
    return {lab: _to_pixels(lab, box, w, h) for lab, box in boxes.items() if box}


def detect(frame_bgr: np.ndarray, labels: list[str], model: str) -> dict[str, Detection]:
    """Return the highest-ranked box per label, in pixel coords of `frame_bgr`.

    PICKY_GROUNDING=auto (default): Perceptron API when PERCEPTRON_API_KEY is set, falling back to the self-hosted
    server at PICKY_VLM_URL if the API call fails (quota, network). `api` / `local` force one backend.
    """
    import os

    mode = os.environ.get("PICKY_GROUNDING", "auto").lower()
    url = os.environ.get("PICKY_VLM_URL")
    has_key = bool(os.environ.get("PERCEPTRON_API_KEY"))
    if mode == "local" or (mode == "auto" and not has_key):
        if not url:
            raise RuntimeError("no grounding backend: set PERCEPTRON_API_KEY or PICKY_VLM_URL")
        return detect_local(frame_bgr, labels, url)
    try:
        return detect_api(frame_bgr, labels, model)
    except Exception as e:
        if mode == "auto" and url:
            print(f"Perceptron API failed ({type(e).__name__}: {e}); using self-hosted model at {url}")
            return detect_local(frame_bgr, labels, url)
        raise


def detect_api(frame_bgr: np.ndarray, labels: list[str], model: str) -> dict[str, Detection]:
    """Perceptron API (Mk1.5 by default): ~0.6 s for several labels in one call."""
    import cv2
    from perceptron import configure, image
    from perceptron import detect as p_detect
    from PIL import Image

    configure(model=model)
    h, w = frame_bgr.shape[:2]
    pil = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    result = p_detect(image(pil), classes=labels)

    found: dict[str, Detection] = {}
    for b in result.boxes or []:
        label = (b.mention or "").strip().lower()
        key = next((lab for lab in labels if lab.lower() in label or label in lab.lower()), None)
        if key is None or key in found:
            continue
        found[key] = _to_pixels(
            key, (b.top_left.x, b.top_left.y, b.bottom_right.x, b.bottom_right.y), *frame_bgr.shape[1::-1]
        )
    return found
