"""Overhead camera capture for the SO-101 rig.

On macOS only apps the user approved may open a camera (Terminal.app usually is; IDE terminals, iTerm or an SSH
session usually are not). If direct capture is refused there, the frame is grabbed by a one-shot helper launched
inside Terminal.app via AppleScript, so the chat works from any terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from .config import RigConfig

_HELPER = """
import sys, cv2
cap = cv2.VideoCapture(int(sys.argv[1]) if sys.argv[1].isdigit() else sys.argv[1])
cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(sys.argv[3])); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(sys.argv[4]))
ok, frame = False, None
for _ in range(10):
    ok, f = cap.read()
    frame = f if ok else frame
cap.release()
if frame is not None:
    cv2.imwrite(sys.argv[2] + ".tmp.jpg", frame)
    import os; os.replace(sys.argv[2] + ".tmp.jpg", sys.argv[2])
"""


def _grab_direct(cfg: RigConfig) -> np.ndarray | None:
    src = int(cfg.camera_index) if cfg.camera_index.isdigit() else cfg.camera_index
    cap = cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_h)
    ok, frame = False, None
    for _ in range(10):  # let auto-exposure settle
        ok, f = cap.read()
        frame = f if ok else frame
    cap.release()
    return frame if ok else None


def _grab_via_terminal(cfg: RigConfig, timeout: float = 25.0) -> np.ndarray | None:
    tmp = Path(tempfile.mkdtemp(prefix="picky_cam_"))
    script, out = tmp / "grab.py", tmp / "frame.jpg"
    script.write_text(_HELPER)
    cmd = f"{sys.executable} {script} {cfg.camera_index} {out} {cfg.frame_w} {cfg.frame_h}; exit"
    apple = f'tell application "Terminal" to do script "{cmd}"'
    subprocess.run(["osascript", "-e", apple], check=False, capture_output=True, timeout=15)
    deadline = time.time() + timeout
    while time.time() < deadline and not out.exists():
        time.sleep(0.2)
    frame = cv2.imread(str(out)) if out.exists() else None
    for f in tmp.glob("*"):
        f.unlink()
    os.rmdir(tmp)
    return frame


def grab_frame(cfg: RigConfig) -> np.ndarray:
    frame = _grab_direct(cfg)
    if frame is None and sys.platform == "darwin":
        frame = _grab_via_terminal(cfg)
    if frame is None:
        raise RuntimeError(
            f"camera {cfg.camera_index} returned no frame. On macOS allow camera access for this terminal app "
            "(System Settings > Privacy & Security > Camera) or run from Terminal.app; check SO101_CAMERA."
        )
    return frame
