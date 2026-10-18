"""Automatic camera-to-robot calibration: no markers, the arm's own fingertip is the target.

The arm visits a spread of known poses; at each one the overhead camera grabs a frame and Perceptron (point mode,
high reasoning) marks the fingertip; PnP over those 2D-3D pairs gives the camera pose (focal length searched too).
Saves ~/.config/picky/camera_pose.json, which scene.CameraModel uses to turn pixels into table positions.

    python -m arms.robot.so101.calibrate_camera            # ~2 min; arm moves above the table, keep hands clear
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from .camera import open_camera
from .config import RigConfig
from .kinematics import SO101Kinematics
from .pick_place import ARM_JOINTS, connect_robot, move, read_pose
from .scene import POSE_FILE

POINT_RE = re.compile(r"<point[^>]*>\s*\((\d+),\s*(\d+)\)\s*</point>")
TIP_PROMPT = (
    "Place one point on the very tip (lowest end) of the long white robot gripper finger that points down toward "
    "the table. If the gripper is not visible, do not invent a point."
)


def settle(robot, target: dict[str, float], gripper: float, tol: float = 0.8, rounds: int = 4) -> float:
    """Re-command each joint past its target by the remaining error (LeRobot's low P gain leaves a few degrees)."""
    err = 0.0
    for _ in range(rounds):
        pose, _ = read_pose(robot)
        errs = {j: target[j] - pose[j] for j in ARM_JOINTS}
        err = max(abs(e) for e in errs.values())
        if err < tol:
            break
        cmd = {f"{j}.pos": target[j] + max(-6.0, min(6.0, errs[j])) for j in ARM_JOINTS}
        cmd["gripper.pos"] = gripper
        robot.send_action(cmd)
        time.sleep(0.5)
    return err


def set_limits_from_calibration(kin: SO101Kinematics, robot) -> None:
    for name, cal in robot.calibration.items():  # degrees mode: 0 at mid-range, +-half the calibrated span
        half = (cal.range_max - cal.range_min) / 2 * 360 / 4096
        kin.limits_deg[name] = (-half + 3.0, half - 3.0)


def fk_tip(kin: SO101Kinematics, pose: dict[str, float]) -> np.ndarray:
    q = np.zeros(len(kin.chain.links))
    for i, n in zip(kin.active, kin.active_names, strict=True):
        q[i] = kin._deg_to_rad(n, pose[n])
    return kin.chain.forward_kinematics(q)[:3, 3]


def find_tip(path: str, model: str = "perceptron-mk1.5") -> tuple[float, float] | None:
    """Fingertip location on Perceptron's 0..1000 grid, or None if the model sees no gripper."""
    from perceptron import Client, image

    client = Client(provider="perceptron", api_key=os.environ["PERCEPTRON_API_KEY"])
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [image(path), {"type": "text", "text": TIP_PROMPT}]}],
        vision_config={"annotation_format": "point"},
        reasoning_effort="high",
        max_completion_tokens=2048,
    )
    m = POINT_RE.search(r.text or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def solve_pnp(obj: np.ndarray, img: np.ndarray, size: tuple[int, int]) -> dict:
    W, H = size
    best = None
    for f in range(300, 1500, 10):
        K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]], dtype=np.float64)
        ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            continue
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
        err = np.linalg.norm(proj.reshape(-1, 2) - img, axis=1)
        if best is None or err.mean() < best["mean_err"]:
            best = {
                "mean_err": float(err.mean()),
                "errs": err.tolist(),
                "K": K.tolist(),
                "rvec": rvec.ravel().tolist(),
                "tvec": tvec.ravel().tolist(),
                "size": [W, H],
            }
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-err", type=float, default=25.0, help="drop correspondences with larger reprojection px")
    args = ap.parse_args()
    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    robot = connect_robot(cfg)
    set_limits_from_calibration(kin, robot)
    rest, rest_grip = read_pose(robot)
    cap = open_camera(cfg.camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.frame_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.frame_h)
    tmp = Path(tempfile.mkdtemp(prefix="picky_camcal_"))
    targets = [
        (r * math.cos(math.radians(a)), r * math.sin(math.radians(a)), z)
        for z in (0.05, 0.09)
        for r in (0.20, 0.26)
        for a in (-30, 0, 30)
    ] + [(0.30, 0.0, 0.06)]
    pairs = []
    try:
        for i, xyz in enumerate(targets):
            pose, _ = read_pose(robot)
            try:
                q = kin.ik(xyz, pose)
            except ValueError as e:
                print(f"skip {xyz}: {e}")
                continue
            q["wrist_roll"] = pose["wrist_roll"]
            jump = max(abs(q[j] - pose[j]) for j in ARM_JOINTS)
            move(robot, q, rest_grip, seconds=max(1.5, jump / 15.0), steps=60)
            settle(robot, q, rest_grip)
            time.sleep(0.4)
            reached, _ = read_pose(robot)
            frame = None
            for _ in range(8):
                ok, f = cap.read()
                frame = f if ok else frame
            if frame is None:
                print("camera returned no frame")
                continue
            path = str(tmp / f"pose{i}.jpg")
            cv2.imwrite(path, frame)
            tip = find_tip(path, cfg.perceptron_model)
            p = fk_tip(kin, reached)
            print(f"pose {i}: fk tip {np.round(p, 3)}  image point {tip}")
            if tip:
                pairs.append((p, (tip[0] * frame.shape[1] / 1000, tip[1] * frame.shape[0] / 1000)))
    finally:
        move(robot, rest, rest_grip, seconds=3.0)
        robot.disconnect()
        cap.release()
    if len(pairs) < 6:
        raise SystemExit(
            f"only {len(pairs)} usable views; make sure the gripper is visible to camera {cfg.camera_index}"
        )
    obj = np.array([p for p, _ in pairs], dtype=np.float64)
    img = np.array([u for _, u in pairs], dtype=np.float64)
    size = (cfg.frame_w, cfg.frame_h)
    sol = solve_pnp(obj, img, size)
    keep = np.array(sol["errs"]) < args.max_err  # one refit without outliers (a misplaced point)
    if keep.sum() >= 6 and not keep.all():
        print(f"dropping {int((~keep).sum())} outlier view(s)")
        sol = solve_pnp(obj[keep], img[keep], size)
    R, _ = cv2.Rodrigues(np.array(sol["rvec"]))
    cam_pos = -R.T @ np.array(sol["tvec"])
    POSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSE_FILE.write_text(json.dumps(sol, indent=1))
    print(
        f"camera at {np.round(cam_pos, 3)} m (robot frame), focal {sol['K'][0][0]:.0f}px, "
        f"mean reprojection error {sol['mean_err']:.1f}px -> {POSE_FILE}"
    )


if __name__ == "__main__":
    main()
