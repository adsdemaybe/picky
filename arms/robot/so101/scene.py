"""Camera-to-robot geometry for the SO-101 rig: pixels -> table positions, and jaw orientation -> wrist roll.

The camera pose comes from `calibrate_camera` (the arm's fingertip at known FK positions, located with Perceptron
point mode, solved with PnP) and lives in ~/.config/picky/camera_pose.json.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

POSE_FILE = Path.home() / ".config" / "picky" / "camera_pose.json"

# Measured on the first successful pick (gum pack standing, long side across the jaws): with the arm at this pose
# (LeRobot degrees, current calibration) the jaws closed along the robot's x axis.
JAW_REFERENCE_POSE = {
    "shoulder_pan": 25.3,
    "shoulder_lift": -8.1,
    "elbow_flex": 29.2,
    "wrist_flex": 54.6,
    "wrist_roll": -10.0,
}
JAW_REFERENCE_YAW = 0.0  # radians, world yaw of the closing direction at JAW_REFERENCE_POSE


@dataclass
class CameraModel:
    K: np.ndarray
    R: np.ndarray  # world -> camera rotation
    t: np.ndarray  # world -> camera translation
    size: tuple[int, int]

    @classmethod
    def load(cls, path: Path = POSE_FILE) -> CameraModel:
        import cv2

        d = json.loads(path.read_text())
        R, _ = cv2.Rodrigues(np.array(d["rvec"], dtype=float))
        return cls(np.array(d["K"], dtype=float), R, np.array(d["tvec"], dtype=float), tuple(d["size"]))

    def pixel_to_world(self, u: float, v: float, z: float, frame_size: tuple[int, int] | None = None) -> np.ndarray:
        """Intersect the camera ray through pixel (u, v) with the horizontal plane at height z (robot frame)."""
        if frame_size and tuple(frame_size) != tuple(self.size):
            u, v = u * self.size[0] / frame_size[0], v * self.size[1] / frame_size[1]
        ray_cam = np.linalg.inv(self.K) @ np.array([u, v, 1.0])
        origin = -self.R.T @ self.t
        ray = self.R.T @ ray_cam
        s = (z - origin[2]) / ray[2]
        return origin + s * ray

    def world_to_pixel(self, p: np.ndarray) -> tuple[float, float]:
        q = self.K @ (self.R @ np.asarray(p, dtype=float) + self.t)
        return float(q[0] / q[2]), float(q[1] / q[2])


def gripper_x_yaw(kin, pose: dict[str, float]) -> float:
    """World yaw of the gripper frame's x axis (projected on the table) at a joint pose."""
    q = np.zeros(len(kin.chain.links))
    for i, n in zip(kin.active, kin.active_names, strict=True):
        q[i] = kin._deg_to_rad(n, pose[n])
    x_axis = kin.chain.forward_kinematics(q)[:3, 0]
    return math.atan2(x_axis[1], x_axis[0])


def jaw_yaw(kin, pose: dict[str, float]) -> float:
    """World yaw of the jaws' closing direction: rotates rigidly with the gripper frame."""
    return JAW_REFERENCE_YAW + gripper_x_yaw(kin, pose) - gripper_x_yaw(kin, JAW_REFERENCE_POSE)


def best_roll(kin, pose: dict[str, float], wanted_yaw: float, limits: tuple[float, float] = (-175, 175)) -> float:
    """Wrist roll (deg) whose jaw closing direction is closest to `wanted_yaw` (mod 180 deg: jaws are symmetric)."""
    best, best_err = pose["wrist_roll"], 1e9
    for roll in np.arange(limits[0], limits[1] + 0.1, 2.5):
        yaw = jaw_yaw(kin, {**pose, "wrist_roll": float(roll)})
        err = abs(math.remainder(yaw - wanted_yaw, math.pi))
        if err < best_err:
            best, best_err = float(roll), err
    return best


def box_long_axis_yaw(cam: CameraModel, box_px: tuple[int, int, int, int], z: float, frame_size) -> float:
    """World yaw of an object's long side, from its (axis-aligned) image box: the longer image side wins."""
    x0, y0, x1, y1 = box_px
    if (x1 - x0) >= (y1 - y0):
        a, b = ((x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2))
    else:
        a, b = (((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1))
    pa, pb = cam.pixel_to_world(*a, z, frame_size), cam.pixel_to_world(*b, z, frame_size)
    return math.atan2(pb[1] - pa[1], pb[0] - pa[0])
