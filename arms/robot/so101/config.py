"""SO-101 bench rig config: ports, camera, table geometry, joint mapping.

Everything here is a per-rig constant you measure once. Units: meters, degrees (LeRobot joint space).
Override any value with an env var of the same name prefixed `SO101_` (e.g. SO101_PORT=/dev/ttyACM0).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SO101_{name}", default)


def _default_port() -> str:
    """First USB serial device that looks like the SO-101 controller (macOS cu.usbmodem*, Linux ttyACM*)."""
    import glob

    found = sorted(glob.glob("/dev/cu.usbmodem*")) + sorted(glob.glob("/dev/ttyACM*"))
    return found[0] if found else "/dev/ttyACM0"


@dataclass
class RigConfig:
    port: str = field(default_factory=lambda: _env("PORT", _default_port()))  # Windows: set SO101_PORT=COM3
    robot_id: str = _env("ROBOT_ID", "follower")  # must match the id used in lerobot-calibrate
    camera_index: str = _env("CAMERA", "0")  # int index or /dev/videoN
    frame_w: int = 640
    frame_h: int = 480

    urdf_path: Path = Path(_env("URDF", str(ASSETS / "so101_new_calib.urdf")))
    homography_path: Path = Path(_env("HOMOGRAPHY", str(HERE / "homography.json")))

    # Grasp heights above the table plane, in the robot base frame (base sits on the table, z=0).
    hover_z: float = 0.08
    grasp_z: float = 0.025
    place_z: float = 0.10

    gripper_open: float = 60.0  # LeRobot gripper range is 0 (closed) .. 100 (open)
    gripper_closed: float = 20.0  # stop short of jaws-touching: squeezing nothing latches an overload flag

    # URDF joint radians -> LeRobot degrees: deg = sign * rad2deg(q) + offset.
    # so101_new_calib.urdf is built to match LeRobot calibration, so start with identity and tune on the bench.
    joint_names: tuple[str, ...] = (
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
    )
    joint_sign: dict[str, float] = field(default_factory=lambda: {})
    joint_offset_deg: dict[str, float] = field(default_factory=lambda: {})

    # Safe pose (LeRobot degrees) the arm returns to between picks.
    home_deg: dict[str, float] = field(
        default_factory=lambda: {
            "shoulder_pan": 0.0,
            "shoulder_lift": -90.0,
            "elbow_flex": 90.0,
            "wrist_flex": 60.0,
            "wrist_roll": 0.0,
        }
    )

    perceptron_model: str = os.environ.get("PERCEPTRON_MODEL", "perceptron-mk1.5")
