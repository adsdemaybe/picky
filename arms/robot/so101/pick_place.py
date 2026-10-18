"""Natural-language pick-and-place on the real SO-101.

    python -m arms.robot.so101.pick_place "pick up the oat milk and put it in the basket"
    python -m arms.robot.so101.pick_place "grab the red soda can" --dry-run   # no motors, prints the plan

Pipeline: camera frame -> Perceptron detect(target, basket) -> homography to table xy -> IK -> scripted
hover / descend / grip / lift / move over basket / release / home.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field

import cv2

from .calibrate_table import load_homography, pixel_to_table
from .camera import grab_frame
from .config import RigConfig
from .grounding import detect, parse_command
from .kinematics import SO101Kinematics


def connect_robot(cfg: RigConfig):
    """Connect with every Goal_Position preset to the present position *before* torque comes on.

    LeRobot's connect() -> configure() re-enables torque on all motors without syncing goals; a servo whose goal
    register still holds a stale value (seen: wrist_flex goal=0 after a power cycle) would swing to it at full speed.
    """
    try:
        from lerobot.robots.so101_follower import SO101Follower, SO101FollowerConfig
    except ImportError:  # LeRobot >= 0.6 merged so100/so101 into so_follower
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig
    robot = SO101Follower(SO101FollowerConfig(port=cfg.port, id=cfg.robot_id, use_degrees=True))
    bus = robot.bus

    def assert_motors_reply() -> None:
        # LeRobot treats a servo status flag (e.g. a latched overload after the gripper squeezed something) as a
        # missing motor and refuses to connect until a power cycle; only a motor that does not answer is fatal.
        for name, m in bus.motors.items():
            _, comm, err = bus.packet_handler.ping(bus.port_handler, m.id)
            if comm != 0:
                raise ConnectionError(f"SO-101 motor {name} (id {m.id}) does not respond")
            if err:
                print(f"warning: {name} reports {bus.packet_handler.getRxPacketError(err)} (continuing)")

    bus._assert_motors_exist = assert_motors_reply
    bus._assert_same_firmware = lambda: None  # its strict read fails on the same latched status flags
    bus.connect()
    bus.disable_torque()
    present = bus.sync_read("Present_Position", normalize=False, num_retry=3)
    bus.sync_write("Goal_Position", present, normalize=False)
    robot.configure()  # writes PID/limits with torque off, then enables torque: holds the current pose
    # LeRobot limits the gripper to 50% torque and drops it to 25% once it stalls on an object, which let a held
    # gum pack slip out mid-carry. Holds here last seconds, so allow 65% / 45%.
    with bus.torque_disabled("gripper"):
        bus.write("Max_Torque_Limit", "gripper", 650)
        bus.write("Overload_Torque", "gripper", 45)
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


ARM_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")


@dataclass
class PickPlan:
    target: str
    dest: str
    frame_path: str
    missing: list[str] = field(default_factory=list)
    target_xy: tuple[float, float] | None = None
    dest_xy: tuple[float, float] | None = None
    steps: list[tuple[str, dict[str, float] | None, float]] = field(default_factory=list)
    error: str | None = None


def plan_pick(cfg: RigConfig, kin: SO101Kinematics, command: str, save: str = "last_detection.jpg") -> PickPlan:
    """Sentence -> camera frame -> Perceptron boxes -> table xy -> IK waypoints. Never touches the motors."""
    target, dest = parse_command(command)
    plan = PickPlan(target, dest, save)
    frame = grab_frame(cfg)
    dets = detect(frame, [target, dest], cfg.perceptron_model)
    for d in dets.values():
        x0, y0, x1, y1 = d.box_px
        cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 255, 0), 2)
        cv2.putText(frame, d.label, (x0, y0 - 6), 0, 0.6, (0, 255, 0), 2)
    cv2.imwrite(save, frame)
    plan.missing = [lab for lab in (target, dest) if lab not in dets]
    if plan.missing:
        plan.error = f"not found in frame: {plan.missing}"
        return plan

    H = load_homography(cfg)
    tx, ty = pixel_to_table(H, *dets[target].center_px)
    bx, by = pixel_to_table(H, *dets[dest].center_px)
    plan.target_xy, plan.dest_xy = (tx, ty), (bx, by)
    try:
        plan.steps = [
            ("hover", kin.ik((tx, ty, cfg.hover_z), cfg.home_deg), cfg.gripper_open),
            ("descend", kin.ik((tx, ty, cfg.grasp_z), cfg.home_deg), cfg.gripper_open),
            ("grip", None, cfg.gripper_closed),
            ("lift", kin.ik((tx, ty, cfg.hover_z), cfg.home_deg), cfg.gripper_closed),
            ("over_basket", kin.ik((bx, by, cfg.place_z), cfg.home_deg), cfg.gripper_closed),
            ("release", None, cfg.gripper_open),
        ]
    except ValueError as e:  # IK target out of reach
        plan.error = str(e)
    return plan


def execute(robot, plan: PickPlan, rest: dict[str, float], rest_gripper: float) -> None:
    """Run the waypoints, then return to `rest` (the pose captured at connect, known to be safe)."""
    last = rest
    for _name, joints, grip in plan.steps:
        last = joints or last
        move(robot, last, grip, seconds=0.8 if joints is None else 2.0)
    move(robot, rest, rest_gripper, seconds=2.5)


def read_pose(robot) -> tuple[dict[str, float], float]:
    obs = robot.get_observation()
    return {k: float(obs[f"{k}.pos"]) for k in ARM_JOINTS}, float(obs["gripper.pos"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help='e.g. "pick up the granola bar and put it in the basket"')
    ap.add_argument("--dry-run", action="store_true", help="detect + plan only, no motors")
    ap.add_argument("--save", default="last_detection.jpg", help="annotated frame output path")
    args = ap.parse_args()

    cfg = RigConfig()
    plan = plan_pick(cfg, SO101Kinematics(cfg), args.command, args.save)
    print(f"target={plan.target!r} destination={plan.dest!r}  (annotated frame: {plan.frame_path})")
    if plan.error:
        raise SystemExit(plan.error)
    print(f"{plan.target}: {plan.target_xy}  {plan.dest}: {plan.dest_xy}")
    for name, joints, grip in plan.steps:
        print(f"  {name:12s} {joints or '(hold)'} gripper={grip}")
    if args.dry_run:
        return

    robot = connect_robot(cfg)
    try:
        rest, rest_grip = read_pose(robot)
        execute(robot, plan, rest, rest_grip)
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()
