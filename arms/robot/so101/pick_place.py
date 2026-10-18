"""Natural-language pick-and-place on the real SO-101.

    python -m arms.robot.so101.pick_place "pick up the oat milk and put it in the basket"
    python -m arms.robot.so101.pick_place "grab the red soda can" --dry-run   # no motors, prints the plan

Pipeline: camera frame -> Perceptron detect(target, basket) -> homography to table xy -> IK -> scripted
hover / descend / grip / lift / move over basket / release / home.
"""

from __future__ import annotations

import argparse
import time

import cv2

from .calibrate_table import load_homography, pixel_to_table
from .camera import grab_frame
from .config import RigConfig
from .grounding import detect, parse_command
from .kinematics import SO101Kinematics


def connect_robot(cfg: RigConfig):
    try:
        from lerobot.robots.so101_follower import SO101Follower, SO101FollowerConfig
    except ImportError:  # newer LeRobot merged so100/so101 into so_follower
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    robot = SO101Follower(SO101FollowerConfig(port=cfg.port, id=cfg.robot_id, use_degrees=True))
    robot.connect()
    return robot


def move(robot, joints: dict[str, float], gripper: float, seconds: float = 1.5, steps: int = 30) -> None:
    """Linear joint-space interpolation from the current pose (the bus has no trajectory controller)."""
    obs = robot.get_observation()
    start = {k: float(obs[f"{k}.pos"]) for k in [*joints, "gripper"]}
    goal = {**joints, "gripper": gripper}
    for s in range(1, steps + 1):
        a = s / steps
        robot.send_action({f"{k}.pos": start[k] + a * (goal[k] - start[k]) for k in goal})
        time.sleep(seconds / steps)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help='e.g. "pick up the granola bar and put it in the basket"')
    ap.add_argument("--dry-run", action="store_true", help="detect + plan only, no motors")
    ap.add_argument("--save", default="last_detection.jpg", help="annotated frame output path")
    args = ap.parse_args()

    cfg = RigConfig()
    target, dest = parse_command(args.command)
    print(f"target={target!r} destination={dest!r}")

    frame = grab_frame(cfg)
    dets = detect(frame, [target, dest], cfg.perceptron_model)
    for d in dets.values():
        x0, y0, x1, y1 = d.box_px
        cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 255, 0), 2)
        cv2.putText(frame, d.label, (x0, y0 - 6), 0, 0.6, (0, 255, 0), 2)
    cv2.imwrite(args.save, frame)
    missing = [lab for lab in (target, dest) if lab not in dets]
    if missing:
        raise SystemExit(f"not found in frame: {missing} (see {args.save})")

    H = load_homography(cfg)
    tx, ty = pixel_to_table(H, *dets[target].center_px)
    bx, by = pixel_to_table(H, *dets[dest].center_px)
    print(f"{target}: ({tx:.3f}, {ty:.3f}) m   {dest}: ({bx:.3f}, {by:.3f}) m")

    kin = SO101Kinematics(cfg)
    plan = [
        ("hover", kin.ik((tx, ty, cfg.hover_z), cfg.home_deg), cfg.gripper_open),
        ("descend", kin.ik((tx, ty, cfg.grasp_z), cfg.home_deg), cfg.gripper_open),
        ("grip", None, cfg.gripper_closed),
        ("lift", kin.ik((tx, ty, cfg.hover_z), cfg.home_deg), cfg.gripper_closed),
        ("over_basket", kin.ik((bx, by, cfg.place_z), cfg.home_deg), cfg.gripper_closed),
        ("release", None, cfg.gripper_open),
        ("home", cfg.home_deg, cfg.gripper_open),
    ]
    for name, joints, grip in plan:
        print(f"  {name:12s} {joints or '(hold)'} gripper={grip}")
    if args.dry_run:
        return

    robot = connect_robot(cfg)
    try:
        last = cfg.home_deg
        for _name, joints, grip in plan:
            last = joints or last
            move(robot, last, grip, seconds=0.8 if joints is None else 1.5)
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()
