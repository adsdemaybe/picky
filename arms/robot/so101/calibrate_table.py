"""One-time pixel -> table (robot base frame) homography for the overhead camera.

Put 4+ tape markers on the table, measure each marker's (x, y) in meters from the SO-101 base
(x forward, y left), then click them in the same order in the camera window.

    python -m arms.robot.so101.calibrate_table --points "0.20,0.10 0.20,-0.10 0.32,-0.10 0.32,0.10"

Headless (over SSH, no window): save a frame, read the marker pixels off it, then pass them in.

    python -m arms.robot.so101.calibrate_table --snapshot /tmp/table.jpg
    python -m arms.robot.so101.calibrate_table --points "..." --pixels "212,301 430,298 441,152 220,149"
"""

from __future__ import annotations

import argparse
import json

import cv2
import numpy as np

from .camera import grab_frame
from .config import RigConfig


def pixel_to_table(H: np.ndarray, u: float, v: float) -> tuple[float, float]:
    p = H @ np.array([u, v, 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def load_homography(cfg: RigConfig) -> np.ndarray:
    return np.array(json.loads(cfg.homography_path.read_text())["H"], dtype=float)


def parse_pairs(text: str) -> np.ndarray:
    return np.array([[float(c) for c in p.split(",")] for p in text.split()], dtype=float)


def click_markers(frame: np.ndarray, n: int) -> list[tuple[int, int]]:
    clicks: list[tuple[int, int]] = []

    def on_click(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < n:
            clicks.append((x, y))
            cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(frame, str(len(clicks)), (x + 6, y - 6), 0, 0.6, (0, 0, 255), 2)

    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_click)
    while len(clicks) < n:
        cv2.imshow("calibrate", frame)
        if cv2.waitKey(20) == 27:
            raise SystemExit("aborted")
    cv2.destroyAllWindows()
    return clicks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", help='robot-frame marker xy in meters, e.g. "0.2,0.1 0.2,-0.1 ..."')
    ap.add_argument("--pixels", help='marker pixels in the same order, e.g. "212,301 430,298 ..." (skips the window)')
    ap.add_argument("--snapshot", help="only save a camera frame to this path (with a pixel grid) and exit")
    args = ap.parse_args()
    cfg = RigConfig()

    if args.snapshot:
        frame = grab_frame(cfg)
        grid = frame.copy()
        for x in range(0, grid.shape[1], 50):
            cv2.line(grid, (x, 0), (x, grid.shape[0]), (0, 255, 255), 1)
            cv2.putText(grid, str(x), (x + 2, 12), 0, 0.35, (0, 255, 255), 1)
        for y in range(0, grid.shape[0], 50):
            cv2.line(grid, (0, y), (grid.shape[1], y), (0, 255, 255), 1)
            cv2.putText(grid, str(y), (2, y - 2), 0, 0.35, (0, 255, 255), 1)
        cv2.imwrite(args.snapshot, frame)
        cv2.imwrite(args.snapshot.rsplit(".", 1)[0] + "_grid.jpg", grid)
        print(f"saved {args.snapshot} ({frame.shape[1]}x{frame.shape[0]}) and a _grid copy")
        return

    if not args.points:
        ap.error("--points is required unless --snapshot is given")
    world = parse_pairs(args.points)
    if args.pixels:
        pixels = parse_pairs(args.pixels)
    else:
        pixels = np.array(click_markers(grab_frame(cfg), len(world)), dtype=float)
    if len(pixels) != len(world) or len(world) < 4:
        raise SystemExit(f"need >= 4 markers with matching counts (got {len(pixels)} pixels, {len(world)} points)")

    H, _ = cv2.findHomography(pixels, world)
    cfg.homography_path.write_text(json.dumps({"H": H.tolist(), "pixels": pixels.tolist(), "world": world.tolist()}))
    err = [np.hypot(*(np.array(pixel_to_table(H, *c)) - w)) for c, w in zip(pixels, world, strict=True)]
    print(f"saved {cfg.homography_path}  max reprojection error {max(err) * 1000:.1f} mm")


if __name__ == "__main__":
    main()
