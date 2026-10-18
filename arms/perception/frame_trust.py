"""Which evidence frames a vision model may judge at all (pure code, before any provider call).

A camera atom judged on the wrong pixels is worse than no camera check: measured on vhagar (goal_bu7gy5gm,
rover-ave-2, Isaac), the post-grasp wrist frame ``rover-ave-2.wrist:000266`` was 97 % black, Perceptron's OCR "read"
``Brand: Nike / Product Name: Air Max 270 React / Size: 10`` off it, the label check FAILED and the runner tried to put
a correctly grasped jar back (no place-back primitive on that backend -> the shopper got "I'm having trouble").

So the verifier drops a frame from VISION (its atoms fall back to sim ground truth / robot state, camera-only atoms go
'uncertain') when the frame:

- is from another camera, robot or store than the one the atom needs (``frame_mismatch``): treated like a stale frame,
  its ground truth is not used either;
- was already judged for another step / attempt, or before the action in this one (``FrameReuse``): a re-served
  frame shows the past, like a stale one;
- is dark (``dark_fraction`` >= ``DARK_FRAME_FRACTION``): lens blocked or pointing into the shelf; the sim ground
  truth of that moment still counts (it does not come from the pixels).

Stale frames (captured before ``VerifyRequest.not_before``) were already handled by ``VerifierBase._frames``.
"""

from __future__ import annotations

import base64
import io
from collections import OrderedDict

from contracts.evidence import FrameRef
from contracts.plan import ExecContext
from contracts.sim import CameraFrame

DARK_LUMA = 24
"""Pixel luma (0-255) below which a pixel counts as black."""
DARK_FRAME_FRACTION = 0.95
"""A frame with at least this share of black pixels carries nothing readable. Measured: the vhagar wrist frame that
produced the Nike read = 0.971; legitimate dark Isaac frames stay below (wrist close-up on a black-label bean can =
0.895, wrist under the shelf edge = 0.888); normal store frames 0.00-0.32."""
THUMB = (160, 120)
REUSE_MEMORY = 2000


def dark_fraction(frame: CameraFrame) -> float | None:
    """Share of black pixels in the frame (thumbnail, luma < ``DARK_LUMA``); None when it cannot be decoded."""
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64)))
        img.draft("L", THUMB)  # JPEG: decode at reduced scale (fast)
        gray = img.convert("L")
        gray.thumbnail(THUMB)
        px = np.asarray(gray)
        if px.size == 0:
            return None
        return float((px < DARK_LUMA).mean())
    except Exception:  # noqa: BLE001 - undecodable: no opinion (the provider answers or fails on its own)
        return None


def dark_reason(frame: CameraFrame) -> str | None:
    """'is dark (97% black pixels)' when the frame is too dark to judge, else None."""
    share = dark_fraction(frame)
    if share is None or share < DARK_FRAME_FRACTION:
        return None
    return f"is dark ({share:.0%} black pixels: lens blocked or facing into the shelf)"


def frame_mismatch(ref: FrameRef, frame: CameraFrame | None, robot_id: str, store_id: str | None) -> str | None:
    """Why the retained frame is not a picture from the camera the atom needs (None = it is)."""
    if frame is None:
        return None  # missing: the caller skips it anyway
    if frame.camera_id and ref.camera_id and frame.camera_id != ref.camera_id:
        return f"is from camera {frame.camera_id}, not {ref.camera_id}"
    if frame.robot_id and frame.robot_id != robot_id:
        return f"is from robot {frame.robot_id}, not {robot_id}"
    if frame.store_id and store_id and frame.store_id != store_id:
        return f"is from store {frame.store_id}, not {store_id}"
    return None


class FrameReuse:
    """Remembers which step (plan step + attempt) and phase first used each retained frame; the SAME frame (same id and
    capture time) presented again for another step, or for the post-check after its own pre-check, is reused."""

    def __init__(self, size: int = REUSE_MEMORY) -> None:
        self.size = size
        self._seen: OrderedDict[tuple[str, str], tuple[str, str]] = OrderedDict()

    @staticmethod
    def _key(ref: FrameRef) -> tuple[str, str]:
        return ref.frame_id, ref.ts.isoformat()

    @staticmethod
    def _step(context: ExecContext | None) -> str | None:
        if context is None or not context.step_id:
            return None
        return f"{context.step_key}#a{context.attempt}"

    def check(self, ref: FrameRef, context: ExecContext | None, phase: str) -> str | None:
        """'was already used for ...' when this exact frame belongs to another step / an earlier phase, else None."""
        step = self._step(context)
        first = self._seen.get(self._key(ref))
        if step is None or first is None:
            return None
        first_step, first_phase = first
        if first_step != step:
            return f"was already used for step {first_step.split(':', 1)[-1]} (reused frame)"
        if first_phase == "pre" and phase != "pre":
            return "was captured for the pre-check, before the action (reused frame)"
        return None

    def note(self, refs: list[FrameRef], context: ExecContext | None, phase: str) -> None:
        step = self._step(context)
        if step is None:
            return
        for ref in refs:
            key = self._key(ref)
            if key not in self._seen:
                self._seen[key] = (step, phase)
            self._seen.move_to_end(key)
        while len(self._seen) > self.size:
            self._seen.popitem(last=False)


__all__ = [
    "DARK_FRAME_FRACTION",
    "DARK_LUMA",
    "FrameReuse",
    "dark_fraction",
    "dark_reason",
    "frame_mismatch",
]
