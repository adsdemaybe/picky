"""Camera-guided pick & place: "pick up the gum and put it on the plate".

One overhead frame -> Perceptron boxes for the object and the destination -> table positions through the calibrated
camera (calibrate_camera.py) -> wrist roll so the jaws close across the object's thin side -> IK waypoints, each
settled to <1 deg. Recipe from the first successful pick: hover, straddle the top, close, lift, carry, lower, open.

    python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate" --dry
    python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate" --step   # Enter per waypoint
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .calibrate_camera import set_limits_from_calibration, settle
from .camera import grab_frame
from .config import RigConfig
from .grounding import detect, parse_command
from .kinematics import SO101Kinematics
from .pick_place import ARM_JOINTS, connect_robot, move, read_pose
from .scene import CameraModel, best_roll, box_long_axis_yaw

GRIP_CLOSED = 5.0  # stalls on the object; LeRobot drops the gripper to reduced torque when it does
GRIP_OPEN = 98.0


@dataclass
class Waypoint:
    name: str
    xyz: tuple[float, float, float] | None  # None = gripper-only step
    gripper: float


@dataclass
class ScenePlan:
    target: str
    dest: str
    target_xy: tuple[float, float] | None = None
    dest_xy: tuple[float, float] | None = None
    jaw_yaw_deg: float | None = None
    roll: float | None = None
    waypoints: list[Waypoint] = field(default_factory=list)
    frame_path: str = "last_scene.jpg"
    error: str | None = None


def plan(
    cfg: RigConfig,
    kin: SO101Kinematics,
    cam: CameraModel,
    command: str,
    seed: dict[str, float],
    object_height: float = 0.05,
    frame: np.ndarray | None = None,
) -> ScenePlan:
    target, dest = parse_command(command)
    p = ScenePlan(target, dest)
    frame = grab_frame(cfg) if frame is None else frame
    size = (frame.shape[1], frame.shape[0])
    dets = detect(frame, [target, dest], cfg.perceptron_model)
    for d in dets.values():
        x0, y0, x1, y1 = d.box_px
        cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 255, 0), 2)
        cv2.putText(frame, d.label, (x0, max(12, y0 - 6)), 0, 0.6, (0, 255, 0), 2)
    cv2.imwrite(p.frame_path, frame)
    missing = [lab for lab in (target, dest) if lab not in dets]
    if missing:
        p.error = f"not found in the camera view: {missing}"
        return p

    h = object_height
    t = cam.pixel_to_world(*dets[target].center_px, h / 2, size)
    dd = cam.pixel_to_world(*dets[dest].center_px, 0.01, size)
    p.target_xy, p.dest_xy = (float(t[0]), float(t[1])), (float(dd[0]), float(dd[1]))
    jaw = box_long_axis_yaw(cam, dets[target].box_px, h / 2, size) + math.pi / 2  # close across the thin side
    p.jaw_yaw_deg = math.degrees(jaw)

    hover = (t[0], t[1], h + 0.012)
    try:
        q_hover = kin.ik(hover, seed)
    except ValueError as e:
        p.error = f"{target} at ({t[0]:.2f}, {t[1]:.2f}) is out of reach: {e}"
        return p
    p.roll = best_roll(kin, q_hover, jaw)
    carry_z = h + 0.03
    hang = h - 0.02  # the object hangs this far below the fingertip (gripped 2 cm below its top)
    p.waypoints = [
        Waypoint("hover over target", hover, GRIP_OPEN),
        Waypoint("straddle", (t[0], t[1], h - 0.02), GRIP_OPEN),
        Waypoint("close", None, GRIP_CLOSED),
        Waypoint("lift", (t[0], t[1], carry_z), GRIP_CLOSED),
        Waypoint("carry over destination", (dd[0], dd[1], carry_z), GRIP_CLOSED),
        Waypoint("lower", (dd[0], dd[1], 0.01 + hang + 0.01), GRIP_CLOSED),
        Waypoint("release", None, GRIP_OPEN),
        Waypoint("retreat", (dd[0], dd[1], carry_z + 0.01), GRIP_OPEN),
    ]
    for w in p.waypoints:  # check every waypoint is reachable before moving anything
        if w.xyz:
            try:
                kin.ik(w.xyz, q_hover)
            except ValueError as e:
                p.error = f"waypoint '{w.name}' {tuple(round(c, 3) for c in w.xyz)} unreachable: {e}"
                return p
    return p


def execute(robot, kin: SO101Kinematics, p: ScenePlan, step: bool = False, log=print) -> None:
    for w in p.waypoints:
        if step:
            input(f"  next: {w.name} - Enter to run, Ctrl-C to stop ")
        pose, grip = read_pose(robot)
        if w.xyz is None:
            move(robot, pose, w.gripper, seconds=1.5)
            log(f"  {w.name}: gripper -> {read_pose(robot)[1]:.0f}")
            continue
        q = kin.ik(w.xyz, pose)
        q["wrist_roll"] = p.roll
        jump = max(abs(q[j] - pose[j]) for j in ARM_JOINTS)
        move(robot, q, w.gripper, seconds=max(1.5, jump / 15.0), steps=60)
        err = settle(robot, q, w.gripper)
        log(f"  {w.name}: settled to {err:.1f} deg")


def describe(p: ScenePlan) -> str:
    if p.error:
        return f"can't do that: {p.error}"
    lines = [
        f"{p.target} at ({p.target_xy[0]:.3f}, {p.target_xy[1]:.3f}) m, {p.dest} at "
        f"({p.dest_xy[0]:.3f}, {p.dest_xy[1]:.3f}) m; jaws across yaw {p.jaw_yaw_deg:.0f} deg -> roll {p.roll:.0f}"
    ]
    for w in p.waypoints:
        where = f"({w.xyz[0]:.3f}, {w.xyz[1]:.3f}, {w.xyz[2]:.3f})" if w.xyz else "(gripper)"
        lines.append(f"  {w.name:24s} {where}  gripper {w.gripper:.0f}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help='e.g. "pick up the gum and put it on the plate"')
    ap.add_argument("--dry", action="store_true", help="plan only (connects to read the current pose)")
    ap.add_argument("--step", action="store_true", help="wait for Enter before each waypoint")
    ap.add_argument("--height", type=float, default=0.05, help="object height in metres (standing gum pack: 0.05)")
    args = ap.parse_args()
    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    cam = CameraModel.load()
    robot = connect_robot(cfg)
    try:
        set_limits_from_calibration(kin, robot)
        rest, rest_grip = read_pose(robot)
        p = plan(cfg, kin, cam, args.command, rest, args.height)
        print(describe(p))
        print(f"annotated camera frame: {p.frame_path}")
        if p.error or args.dry:
            return
        execute(robot, kin, p, step=args.step)
        print("done")
    finally:
        move(robot, rest, rest_grip, seconds=3.0)
        robot.disconnect()


if __name__ == "__main__":
    main()
