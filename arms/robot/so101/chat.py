"""Terminal chat for the SO-101: type a request, see what the robot found and plans, confirm, it moves.

    python -m arms.robot.so101.chat            # connects to the arm, asks before every motion
    python -m arms.robot.so101.chat --dry      # never moves: detect + plan only (no arm needed)

On macOS run it from Terminal itself (or a `screen` session started there): processes launched over SSH are not
allowed to use the camera.
"""

from __future__ import annotations

import argparse
import os
import readline  # noqa: F401  (arrow-key history in input())
import time
import traceback

from .config import RigConfig
from .kinematics import SO101Kinematics
from .pick_place import connect_robot, execute, move, plan_pick, read_pose

HELP = """\
Type a request, e.g.  pick up the oat milk and put it in the basket
  /dry        toggle dry-run (plan only, never move)      /status   joint angles
  /home       return to the pose captured at connect      /open /close   gripper
  /snap       save a camera frame to snap.jpg             /cam N    switch camera index
  /help /quit"""

C = {
    "dim": "\033[2m",
    "b": "\033[1m",
    "g": "\033[32m",
    "y": "\033[33m",
    "r": "\033[31m",
    "c": "\033[36m",
    "0": "\033[0m",
}


def say(msg: str, color: str = "0") -> None:
    print(f"{C[color]}{msg}{C['0']}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="start in dry-run mode and do not connect to the arm")
    args = ap.parse_args()

    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    dry = args.dry
    robot, rest, rest_grip = None, None, None

    say("Picky SO-101 — natural-language pick & place", "b")
    key, url = os.environ.get("PERCEPTRON_API_KEY"), os.environ.get("PICKY_VLM_URL")
    mode = os.environ.get("PICKY_GROUNDING", "auto")
    if mode == "local" or not key:
        say(f"grounding: self-hosted Isaac-0.2-1B at {url}" if url else "warning: no grounding backend set", "dim")
    else:
        say(f"grounding: Perceptron API ({cfg.perceptron_model})" + (f", fallback {url}" if url else ""), "dim")
    if not cfg.homography_path.exists():
        say(f"warning: no table calibration at {cfg.homography_path}; run calibrate_table first", "y")
    if not dry:
        say(f"connecting to the arm on {cfg.port} ...", "dim")
        robot = connect_robot(cfg)
        _, rest_grip = read_pose(robot)
        rest = dict(cfg.home_deg)  # raised pose, wrist camera overlooking the table
        move(robot, rest, rest_grip, seconds=3.0)
        say("connected; moved to the raised home pose (/home returns here)", "g")
    say(HELP, "dim")

    try:
        while True:
            try:
                line = input(f"{C['c']}{'[dry] ' if dry else ''}you> {C['0']}").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            cmd = line.lower()
            try:
                if cmd in ("/quit", "/exit", "/q"):
                    break
                if cmd == "/help":
                    say(HELP, "dim")
                elif cmd == "/dry":
                    if robot is None and dry:
                        say("started with --dry: no arm connection, staying in dry-run", "y")
                    else:
                        dry = not dry
                        say(f"dry-run {'on' if dry else 'off'}", "y")
                elif cmd.startswith("/cam"):
                    parts = cmd.split()
                    if len(parts) == 2:
                        cfg.camera_index = parts[1]
                    say(f"camera index: {cfg.camera_index}  (then /snap to check the view)", "g")
                elif cmd == "/snap":
                    import cv2

                    from .camera import grab_frame

                    cv2.imwrite("snap.jpg", grab_frame(cfg))
                    say("saved snap.jpg", "g")
                elif cmd in ("/status", "/home", "/open", "/close"):
                    if robot is None:
                        say("no arm connected (dry mode)", "y")
                        continue
                    pose, grip = read_pose(robot)
                    if cmd == "/status":
                        say("  ".join(f"{k}={v:.1f}" for k, v in pose.items()) + f"  gripper={grip:.1f}")
                    elif cmd == "/home":
                        move(robot, rest, rest_grip, seconds=2.5)
                    else:
                        move(robot, pose, cfg.gripper_open if cmd == "/open" else cfg.gripper_closed, seconds=1.0)
                elif cmd.startswith("/"):
                    say(f"unknown command {line}; /help lists them", "y")
                else:
                    t0 = time.perf_counter()
                    say("looking ...", "dim")
                    plan = plan_pick(cfg, kin, line)
                    say(f"robot> target: {plan.target!r}   put in: {plan.dest!r}   ({time.perf_counter() - t0:.1f}s)")
                    say(f"       annotated camera frame: {plan.frame_path}", "dim")
                    if plan.error:
                        say(f"robot> can't do that: {plan.error}", "r")
                        continue
                    tx, ty = plan.target_xy
                    bx, by = plan.dest_xy
                    say(f"robot> {plan.target} at ({tx:.2f}, {ty:.2f}) m, {plan.dest} at ({bx:.2f}, {by:.2f}) m")
                    for name, joints, grip in plan.steps:
                        js = "  ".join(f"{v:6.1f}" for v in joints.values()) if joints else "  (hold)"
                        say(f"       {name:12s}{js}   gripper {grip:.0f}", "dim")
                    if dry or robot is None:
                        say("robot> dry-run: not moving", "y")
                        continue
                    if input(f"{C['y']}execute? [y/N] {C['0']}").strip().lower() != "y":
                        say("robot> ok, skipped", "y")
                        continue
                    execute(robot, plan, rest, rest_grip)
                    say(f"robot> done — {plan.target} should be in the {plan.dest}", "g")
            except Exception as e:  # keep the session alive on any single-request failure
                say(f"error: {e}", "r")
                if os.environ.get("PICKY_DEBUG"):
                    traceback.print_exc()
    finally:
        if robot is not None:
            say("returning to the start pose and releasing torque ...", "dim")
            try:
                move(robot, rest, rest_grip, seconds=2.5)
            finally:
                robot.disconnect()


if __name__ == "__main__":
    main()
