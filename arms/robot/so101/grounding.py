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


def detect(frame_bgr: np.ndarray, labels: list[str], model: str) -> dict[str, Detection]:
    """Return the highest-ranked box per label, in pixel coords of `frame_bgr`."""
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
        # Perceptron boxes are on a 0..1000 normalized grid.
        x0, y0 = b.top_left.x * w / 1000.0, b.top_left.y * h / 1000.0
        x1, y1 = b.bottom_right.x * w / 1000.0, b.bottom_right.y * h / 1000.0
        found[key] = Detection(key, (int(x0), int(y0), int(x1), int(y1)))
    return found
