"""Helpers shared by the ``VisionProvider`` implementations (Perceptron, the sidecar fake) and the verifier."""

from __future__ import annotations

import re
import statistics
import time
from collections import deque
from typing import Any

from contracts.grounding import NORM_MAX, Evidence, NormBox, NormPoint, ProviderCall, VerifyMethod, VisionProviderName
from contracts.sim import CameraFrame
from contracts.verify import VisionAnswer

_YES_NO_RE = re.compile(r"^\W*(yes|no)\b", re.IGNORECASE)


def require_blurred(frame: CameraFrame) -> None:
    """CLAUDE.md section 8: no frame reaches a vision model unless faces are blurred (``VisionProvider`` contract)."""
    if not frame.blurred:
        raise ValueError(
            f"faces must be blurred before a frame reaches a vision model ({frame.frame_id}: blurred=False)"
        )


def parse_yes_no(text: str | None) -> bool | None:
    """'Yes, the gripper is empty.' -> True; 'No - a jar is held' -> False; anything else -> None."""
    m = _YES_NO_RE.match(text or "")
    if m is None:
        return None
    return m.group(1).lower() == "yes"


def yes_no_evidence(text: str | None) -> str:
    """The sentence after the yes/no word (the model's visual evidence), or the whole text."""
    t = (text or "").strip()
    m = _YES_NO_RE.match(t)
    rest = t[m.end() :].lstrip(" ,.:;-") if m else t
    return rest or t


def _clamp(v: Any) -> int:
    try:
        return max(0, min(NORM_MAX, round(float(v))))
    except (TypeError, ValueError):
        return 0


def norm_box(x1: Any, y1: Any, x2: Any, y2: Any) -> NormBox:
    """A NormBox from possibly out-of-range / swapped model coordinates (clamped to the 0-1000 grid, corners sorted)."""
    a, b, c, d = _clamp(x1), _clamp(y1), _clamp(x2), _clamp(y2)
    return NormBox(x1=min(a, c), y1=min(b, d), x2=max(a, c), y2=max(b, d))


def norm_point(x: Any, y: Any) -> NormPoint:
    return NormPoint(x=_clamp(x), y=_clamp(y))


def overlap_fraction(box: NormBox, roi: NormBox) -> float:
    """Share of ``box``'s own area inside ``roi`` (a small held item fully inside the jaws scores 1.0 even when its
    IoU with the big ROI is tiny)."""
    ix = max(0, min(box.x2, roi.x2) - max(box.x1, roi.x1))
    iy = max(0, min(box.y2, roi.y2) - max(box.y1, roi.y1))
    area = (box.x2 - box.x1) * (box.y2 - box.y1)
    return (ix * iy) / area if area > 0 else 0.0


def answer_ok(answer: VisionAnswer | None) -> bool:
    return answer is not None and answer.call.error_code is None


def failed_answer(
    provider: VisionProviderName, model: str | None, call: str, prompt: str | None, code: str, t0: float
) -> VisionAnswer:
    return VisionAnswer(
        call=ProviderCall(
            provider=provider,
            model=model,
            call=call,
            prompt=prompt,
            error_code=code,
            latency_ms=(time.monotonic() - t0) * 1000,
        ),
        confidence=0.0,
    )


def box_evidence(mention: str, box: NormBox, *, confidence: float = 0.85) -> Evidence:
    return Evidence(kind="box", mention=mention, box=box, confidence=confidence)


def provider_method(name: str) -> VerifyMethod:
    """VerifyMethod for a provider name: perceptron -> 'perceptron', gemini / claude -> 'vlm', anything else 'fake'."""
    if name == "perceptron":
        return "perceptron"
    if name in ("gemini", "claude"):
        return "vlm"
    return "fake"


class ProviderStats:
    """Calls / errors / latency / agreement with sim ground truth (GET /api/verifier/providers)."""

    def __init__(self, window: int = 200) -> None:
        self.calls = 0
        self.errors = 0
        self.error_codes: dict[str, int] = {}
        self._latency: deque[float] = deque(maxlen=window)
        self._agree: deque[bool] = deque(maxlen=window)

    def record(self, call: ProviderCall) -> None:
        self.calls += 1
        if call.error_code:
            self.errors += 1
            self.error_codes[call.error_code] = self.error_codes.get(call.error_code, 0) + 1
        else:
            self._latency.append(call.latency_ms)

    def record_agreement(self, agrees: bool) -> None:
        self._agree.append(agrees)

    @property
    def p50_latency_ms(self) -> float | None:
        return round(statistics.median(self._latency), 1) if self._latency else None

    @property
    def agreement(self) -> float | None:
        return round(sum(self._agree) / len(self._agree), 3) if self._agree else None


__all__ = [
    "ProviderStats",
    "answer_ok",
    "box_evidence",
    "failed_answer",
    "norm_box",
    "norm_point",
    "overlap_fraction",
    "parse_yes_no",
    "provider_method",
    "require_blurred",
    "yes_no_evidence",
]
