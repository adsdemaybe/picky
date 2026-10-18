"""Camera-guided pick & place: "pick up the gum and put it on the plate".

One overhead frame -> Perceptron boxes for the object and the destination -> table positions through the calibrated
camera (calibrate_camera.py) -> wrist roll so the jaws close across the object's thin side -> IK waypoints, each
settled to <1 deg. Recipe from the first successful pick: hover, straddle the top, close, lift, carry, lower, open.

    python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate" --dry
    python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate" --step   # Enter per waypoint
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .calibrate_camera import set_limits_from_calibration, settle
from .camera import grab_frame
from .config import RUNS_DIR, RigConfig
from .grounding import detect, parse_command
from .kinematics import SO101Kinematics
from .pick_place import ARM_JOINTS, connect_robot, move, read_pose
from .scene import CameraModel, best_roll, box_long_axis_yaw

GRIP_CLOSED = 5.0  # stalls on the object; LeRobot drops the gripper to reduced torque when it does
GRIP_OPEN = 98.0
MAX_REACH = 0.36  # m, horizontal; nothing beyond this solves at grasp/carry heights


# What the objects on this table look like: a bare "gum" has been matched to the Oreo bag, so ask for these first.
APPEARANCE = {
    "gum": "green Extra chewing gum pack",
    "oreo": "blue Oreo cookie bag",
    "oreos": "blue Oreo cookie bag",
    "plate": "round paper plate",
}


def detect_named(frame: np.ndarray, name: str, model: str):
    """Perceptron box for `name`: its known appearance first, then fuller phrasings (short names often miss)."""
    described = [APPEARANCE[name.lower()]] if name.lower() in APPEARANCE else []
    for query in (
        *described,
        name,
        f"{name} packet",
        f"{name} package",
        f"pack of {name}",
        f"box of {name}",
        f"{name} container",
    ):
        d = detect(frame, [query], model).get(query)
        if d:
            d.label = name
            return d
    return None


def choose_drop_point(kin, cam, box_px, size, heights, seed, shift=(0.0, 0.0), margin=0.75):
    """Reachable drop spot inside a destination: samples a grid on the ellipse inscribed in its image box (shrunk by
    `margin`, so points stay off the rim and inside a round plate even when the box is cut by the frame edge), keeps
    the points the arm reaches at every height in `heights`, and returns the one deepest inside the destination."""
    x0, y0, x1, y1 = box_px
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    grid = [(gu, gv) for gu in np.linspace(-1, 1, 9) for gv in np.linspace(-1, 1, 9) if math.hypot(gu, gv) <= 1.0]
    grid.sort(key=lambda g: math.hypot(*g))  # deepest inside first: the first reachable point is the best one
    for gu, gv in grid:
        pt = cam.pixel_to_world(cx + gu * rx * margin, cy + gv * ry * margin, 0.01, size)
        pt = pt + np.array([shift[0], shift[1], 0.0])
        if math.hypot(pt[0], pt[1]) > MAX_REACH:  # cheap reject: proving unreachability with IK is the slow case
            continue
        try:
            for z in heights:
                kin.ik((pt[0], pt[1], z), seed, quick=True)
        except ValueError:
            continue
        return pt
    return None


def highest_reachable(kin, x: float, y: float, seed, z_max: float = 0.16, z_min: float = 0.09, step: float = 0.01):
    """Highest fingertip height (z_max down to z_min) the arm reaches above (x, y), or None."""
    z = z_max
    while z >= z_min - 1e-9:
        try:
            kin.ik((x, y, z), seed, quick=True)
            return round(z, 3)
        except ValueError:
            z -= step
    return None


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
    steer: dict[str, float] | None = None,
) -> ScenePlan:
    """`steer` (metres / degrees) nudges the base plan; train_loop learns it from success/failure labels:
    grasp_dx, grasp_dy, grasp_dz, roll, drop_dx, drop_dy."""
    st = {"grasp_dx": 0.0, "grasp_dy": 0.0, "grasp_dz": 0.0, "roll": 0.0, "drop_dx": 0.0, "drop_dy": 0.0}
    st.update(steer or {})
    target, dest = parse_command(command)
    p = ScenePlan(target, dest)
    frame = grab_frame(cfg) if frame is None else frame
    size = (frame.shape[1], frame.shape[0])
    dets = {}
    for name in (target, dest):
        d = detect_named(frame, name, cfg.perceptron_model)
        if d:
            dets[name] = d
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
    t = t + np.array([st["grasp_dx"], st["grasp_dy"], 0.0])
    jaw = box_long_axis_yaw(cam, dets[target].box_px, h / 2, size) + math.pi / 2  # close across the thin side
    p.jaw_yaw_deg = math.degrees(jaw)

    hover = (t[0], t[1], h + 0.012)
    try:
        q_hover = kin.ik(hover, seed)
    except ValueError as e:
        p.error = f"{target} at ({t[0]:.2f}, {t[1]:.2f}) is out of reach: {e}"
        return p
    p.roll = max(-175.0, min(175.0, best_roll(kin, q_hover, jaw) + st["roll"]))
    hang = h - 0.02  # the object hangs this far below the fingertip (gripped 2 cm below its top)
    lower_z = 0.01 + hang + 0.01  # object bottom ~1 cm above the plate when released
    # Transit must clear a plate/bowl rim with margin for arm error and the object slipping in the jaws:
    # object bottom >= ~6 cm above the table -> fingertip >= hang + 0.06.
    clear_z = max(h + 0.04, hang + 0.06)

    dd = choose_drop_point(
        kin,
        cam,
        dets[dest].box_px,
        size,
        (clear_z, lower_z),
        seed,
        shift=(st["drop_dx"], st["drop_dy"]),
    )
    if dd is None:
        p.error = (
            f"no part of the {dest} is within reach at a safe carry height - move it within ~28 cm of the arm's base"
        )
        return p
    p.target_xy, p.dest_xy = (float(t[0]), float(t[1])), (float(dd[0]), float(dd[1]))
    lift_z = highest_reachable(kin, t[0], t[1], seed, z_min=clear_z)
    drop_high_z = highest_reachable(kin, dd[0], dd[1], seed, z_min=clear_z)
    mid = ((t[0] + dd[0]) / 2 * 0.85, (t[1] + dd[1]) / 2)  # a bit closer to the base, where the arm reaches higher
    mid_z = highest_reachable(kin, mid[0], mid[1], seed, z_min=clear_z)
    if None in (lift_z, drop_high_z, mid_z):
        p.error = f"can't carry high enough (>= {clear_z * 100:.0f} cm) between {target} and {dest}; move them closer"
        return p
    p.waypoints = [
        Waypoint("hover over target", hover, GRIP_OPEN),
        Waypoint("straddle", (t[0], t[1], h - 0.02 + st["grasp_dz"]), GRIP_OPEN),
        Waypoint("close", None, GRIP_CLOSED),
        Waypoint("lift straight up", (t[0], t[1], h + 0.03), GRIP_CLOSED),
        Waypoint("lift high", (t[0], t[1], lift_z), GRIP_CLOSED),
        Waypoint("transit", (mid[0], mid[1], mid_z), GRIP_CLOSED),
        Waypoint("over destination", (dd[0], dd[1], drop_high_z), GRIP_CLOSED),
        Waypoint("lower", (dd[0], dd[1], lower_z), GRIP_CLOSED),
        Waypoint("release", None, GRIP_OPEN),
        Waypoint("retreat", (dd[0], dd[1], drop_high_z), GRIP_OPEN),
    ]
    for w in p.waypoints:  # check every waypoint is reachable before moving anything
        if w.xyz:
            try:
                kin.ik(w.xyz, seed)
            except ValueError as e:
                p.error = f"waypoint '{w.name}' {tuple(round(c, 3) for c in w.xyz)} unreachable: {e}"
                return p
    return p


def execute(
    robot,
    kin: SO101Kinematics,
    p: ScenePlan,
    step: bool = False,
    log=print,
    cfg: RigConfig | None = None,
    snap_after: tuple[str, ...] = ("lift high", "release"),
    on_step=None,
) -> list[dict]:
    """Runs the plan; returns the joint trajectory actually commanded (for recipes / exact replay)."""
    traj: list[dict] = []
    for w in p.waypoints:
        if on_step:
            on_step(w.name)
        if step:
            input(f"  next: {w.name} - Enter to run, Ctrl-C to stop ")
        pose, grip = read_pose(robot)
        if w.xyz is None:
            move(robot, pose, w.gripper, seconds=1.5)
            log(f"  {w.name}: gripper -> {read_pose(robot)[1]:.0f}")
            traj.append({"name": w.name, "joints": None, "gripper": w.gripper})
            _snap(cfg, w.name, snap_after, log)
            continue
        q = kin.ik(w.xyz, pose)
        q["wrist_roll"] = p.roll
        jump = max(abs(q[j] - pose[j]) for j in ARM_JOINTS)
        move(robot, q, w.gripper, seconds=max(1.5, jump / 15.0), steps=60)
        err = settle(robot, q, w.gripper)
        log(f"  {w.name}: settled to {err:.1f} deg")
        traj.append({"name": w.name, "joints": {j: round(float(q[j]), 2) for j in ARM_JOINTS}, "gripper": w.gripper})
        _snap(cfg, w.name, snap_after, log)
    return traj


RECIPES = RUNS_DIR / "recipes"


def save_recipe(name: str, p: ScenePlan, traj: list[dict], home: dict[str, float]) -> Path:
    RECIPES.mkdir(parents=True, exist_ok=True)
    path = RECIPES / f"{name}.json"
    rec = {
        "name": name,
        "target": p.target,
        "dest": p.dest,
        "target_xy": p.target_xy,
        "dest_xy": p.dest_xy,
        "roll": p.roll,
        "home": home,
        "trajectory": traj,
        "waypoints": [{"name": w.name, "xyz": w.xyz, "gripper": w.gripper} for w in p.waypoints],
    }
    path.write_text(json.dumps(rec, indent=1, default=float))
    return path


def replay(robot, name: str, log=print) -> None:
    """Re-run a saved recipe joint-for-joint: no camera, no planning - identical every time if the objects are
    placed where they were when it was recorded."""
    rec = json.loads((RECIPES / f"{name}.json").read_text())
    log(f"replaying {name}: {rec['target']} at {rec['target_xy']} -> {rec['dest']} at {rec['dest_xy']}")
    for step in rec["trajectory"]:
        pose, _ = read_pose(robot)
        q = step["joints"] or pose
        jump = max(abs(q[j] - pose[j]) for j in ARM_JOINTS)
        move(robot, q, step["gripper"], seconds=1.5 if step["joints"] is None else max(1.5, jump / 15.0), steps=60)
        err = settle(robot, q, step["gripper"]) if step["joints"] else 0.0
        log(f"  {step['name']}: settled to {err:.1f} deg, gripper {read_pose(robot)[1]:.0f}")


def _snap(cfg: RigConfig | None, name: str, which: tuple[str, ...], log) -> None:
    """Save a camera frame after selected waypoints so a run can be checked afterwards."""
    if cfg is None or name not in which:
        return
    path = f"last_{name.split()[0]}.jpg"
    try:
        cv2.imwrite(path, grab_frame(cfg))
        log(f"  (frame saved: {path})")
    except RuntimeError as e:
        log(f"  (no frame: {e})")


def looks_successful(cfg: RigConfig, target: str, dest: str) -> tuple[bool | None, np.ndarray | None]:
    """Camera check after a run: is the target now inside the destination's box? None if it can't tell."""
    try:
        frame = grab_frame(cfg)
    except RuntimeError:
        return None, None
    t = detect_named(frame, target, cfg.perceptron_model)
    d = detect_named(frame, dest, cfg.perceptron_model)
    if d is None:
        return None, frame
    if t is None:
        return False, frame
    x0, y0, x1, y1 = d.box_px
    u, v = t.center_px
    return (x0 <= u <= x1 and y0 <= v <= y1), frame


def go_home(robot, home: dict[str, float], gripper: float) -> None:
    """Move to the raised home pose (wrist camera overlooking the table) and settle there."""
    pose, _ = read_pose(robot)
    jump = max(abs(home[j] - pose[j]) for j in ARM_JOINTS)
    move(robot, home, gripper, seconds=max(2.0, jump / 20.0), steps=60)
    settle(robot, home, gripper)


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
    ap.add_argument("command", nargs="?", default="", help='e.g. "pick up the gum and put it on the plate"')
    ap.add_argument("--dry", action="store_true", help="plan only (connects to read the current pose)")
    ap.add_argument("--step", action="store_true", help="wait for Enter before each waypoint")
    ap.add_argument("--height", type=float, default=0.05, help="object height in metres (standing gum pack: 0.05)")
    ap.add_argument("--save", metavar="NAME", help="after the run, save it as a replayable recipe")
    ap.add_argument("--replay", metavar="NAME", help="re-run a saved recipe exactly (no camera / planning)")
    ap.add_argument(
        "--record",
        nargs="?",
        const="auto",
        metavar="PATH",
        help="record the overhead camera to an MP4 (default picky/runs/videos/<time>.mp4)",
    )
    args = ap.parse_args()
    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    cam = CameraModel.load()
    rec = None
    if args.record:
        from .camera import start_recording

        vid = (
            Path(args.record)
            if args.record != "auto"
            else (RUNS_DIR / "videos" / f"{time.strftime('%Y%m%d-%H%M%S')}.mp4")
        )
        vid.parent.mkdir(parents=True, exist_ok=True)
        rec = start_recording(cfg, str(vid), title=f'Picky  |  "{args.command or args.replay}"')
        rec.caption = "connecting"
        print(f"recording to {vid}")
    robot = connect_robot(cfg)
    try:
        set_limits_from_calibration(kin, robot)
        rest = dict(cfg.home_deg)
        go_home(robot, rest, GRIP_OPEN)
        if args.replay:
            replay(robot, args.replay)
            print("done")
            return
        from .train_loop import DATA_DIR, QPolicy  # late import: train_loop imports this module

        policy = QPolicy.load()
        actions = policy.greedy()  # best-known tweaks (both "base" = the routine that succeeded); no exploration
        steer = policy.steer(actions)
        scene = grab_frame(cfg)
        p = plan(cfg, kin, cam, args.command, rest, args.height, frame=scene.copy(), steer=steer)
        print(describe(p))
        print(f"annotated camera frame: {p.frame_path}")
        if p.error or args.dry:
            return
        if rec:
            rec.caption = f"found {p.target} and {p.dest} - planning done"
        traj = execute(
            robot,
            kin,
            p,
            step=args.step,
            cfg=cfg,
            on_step=(lambda name: setattr(rec, "caption", name)) if rec else None,
        )
        if rec:
            rec.caption = "returning home"
        go_home(robot, rest, GRIP_OPEN)
        save_recipe("last_run", p, traj, rest)  # every run is replayable; --save keeps a named copy
        if args.save:
            print(f"saved recipe: {save_recipe(args.save, p, traj, rest)}")
        guess, after = looks_successful(cfg, p.target, p.dest)
        if rec:
            rec.caption = {True: f"done: {p.target} is on the {p.dest}", False: "done", None: "done"}[guess]
            time.sleep(2.0)  # hold the final shot
            print(f"video saved: {rec.stop()}  ({rec.frames} frames)")
            rec = None
        hint = {True: "looks like a SUCCESS", False: "looks like a FAILURE", None: "can't tell from the camera"}[guess]
        default = {True: "s", False: "f", None: ""}[guess]
        label = ""
        while label not in ("s", "f"):
            ans = input(f"{hint} - Enter to accept, or type s / f: ").strip().lower()
            label = ans or default
        success = label == "s"
        policy.update(actions, success)  # TD update of both decisions' Q-values
        policy.save()
        ep = DATA_DIR / time.strftime("%Y%m%d-%H%M%S")
        ep.mkdir(parents=True, exist_ok=True)
        if success:  # failures keep only episode.json: no video or frames
            cv2.imwrite(str(ep / "scene.jpg"), scene)
            if after is not None:
                cv2.imwrite(str(ep / "after.jpg"), after)
        elif args.record:
            vid.unlink(missing_ok=True)
            print("failure: video not kept")
        record = {
            "command": args.command,
            "success": success,
            "auto_guess": guess,
            "actions": actions,
            "steer": steer,
            "target_xy": p.target_xy,
            "dest_xy": p.dest_xy,
            "roll": p.roll,
            "trajectory": traj,
            "waypoints": [{"name": w.name, "xyz": w.xyz, "gripper": w.gripper} for w in p.waypoints],
        }
        (ep / "episode.json").write_text(json.dumps(record, indent=1, default=float))
        if success:
            save_recipe(f"success_{ep.name}", p, traj, rest)
        print(
            f"{'SUCCESS' if success else 'failure'} saved to {ep} | policy {policy.successes} successes / "
            f"{policy.episodes} runs | {policy.summary()}"
        )
    finally:
        go_home(robot, dict(cfg.home_deg), GRIP_OPEN)
        robot.disconnect()
        if rec:
            print(f"video saved: {rec.stop()}  ({rec.frames} frames)")


if __name__ == "__main__":
    main()
