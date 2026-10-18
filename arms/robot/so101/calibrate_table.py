"""One-time pixel -> table (robot base frame) homography for the overhead camera.

Put 4+ tape markers on the table, measure each marker's (x, y) in meters from the SO-101 base
(x forward, y left), then click them in the same order in the camera window.

    python -m arms.robot.so101.calibrate_table --points "0.20,0.10 0.20,-0.10 0.32,-0.10 0.32,0.10"
"""

from __future__ import annotations

import argparse
import json

import cv2
import numpy as np

from .config import RigConfig


def pixel_to_table(H: np.ndarray, u: float, v: float) -> tuple[float, float]:
    p = H @ np.array([u, v, 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def load_homography(cfg: RigConfig) -> np.ndarray:
    return np.array(json.loads(cfg.homography_path.read_text())["H"], dtype=float)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", required=True, help='robot-frame marker xy, e.g. "0.2,0.1 0.2,-0.1 ..."')
    args = ap.parse_args()
    cfg = RigConfig()
    world = np.array([[float(c) for c in p.split(",")] for p in args.points.split()], dtype=float)

    cam_src = int(cfg.camera_index) if cfg.camera_index.isdigit() else cfg.camera_index
    cap = cv2.VideoCapture(cam_src)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"could not read camera {cfg.camera_index}")

    clicks: list[tuple[int, int]] = []

    def on_click(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < len(world):
            clicks.append((x, y))
            cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(frame, str(len(clicks)), (x + 6, y - 6), 0, 0.6, (0, 0, 255), 2)

    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_click)
    while len(clicks) < len(world):
        cv2.imshow("calibrate", frame)
        if cv2.waitKey(20) == 27:
            raise SystemExit("aborted")
    cv2.destroyAllWindows()

    H, _ = cv2.findHomography(np.array(clicks, dtype=float), world)
    cfg.homography_path.write_text(json.dumps({"H": H.tolist(), "pixels": clicks, "world": world.tolist()}))
    err = [np.hypot(*(np.array(pixel_to_table(H, *c)) - w)) for c, w in zip(clicks, world)]
    print(f"saved {cfg.homography_path}  max reprojection error {max(err) * 1000:.1f} mm")


if __name__ == "__main__":
    main()
