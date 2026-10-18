"""Overhead camera capture for the SO-101 rig."""

from __future__ import annotations

import cv2
import numpy as np

from .config import RigConfig


def grab_frame(cfg: RigConfig) -> np.ndarray:
    src = int(cfg.camera_index) if cfg.camera_index.isdigit() else cfg.camera_index
    cap = cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_h)
    ok, frame = False, None
    for _ in range(10):  # let auto-exposure settle
        ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(
            f"camera {cfg.camera_index} returned no frame (on macOS over SSH: run inside a screen session started "
            "from Terminal so it inherits Terminal's camera permission)"
        )
    return frame
