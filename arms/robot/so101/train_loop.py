"""Human-in-the-loop RL for the SO-101 pick & place: press Enter to run, type s / f to label, it learns.

Policy-steering in the spirit of DSRL: the camera-guided pick routine is a frozen base policy, and a small Gaussian
"steering" policy learns offsets to its key decisions (grasp x/y/depth, wrist roll, drop x/y) from successful runs
only: each success pulls the steering toward what worked and narrows the search; failures are logged, never learned
from. Every episode is saved (frames, plan, offsets, joint trajectory, label) as a dataset for later BC / DSRL on a
learned policy, and every success also becomes a replayable recipe.

    python -m arms.robot.so101.train_loop "pick up the gum and put it on the plate"

Keys (type, then Enter):  Enter = run an episode   s = success   f = failure   q = quit
s / f can be typed any time during a run; if you haven't labelled by the end, it asks.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .calibrate_camera import set_limits_from_calibration
from .camera import grab_frame
from .config import RigConfig
from .kinematics import SO101Kinematics
from .pick_place import connect_robot, read_pose
from .pick_routine import GRIP_OPEN, GripLost, describe, execute, go_home, plan, save_recipe
from .scene import CameraModel

DATA_DIR = Path.home() / "picky_runs"
POLICY_FILE = DATA_DIR / "steer_policy.json"

# name: (initial std, bound) - metres, except roll in degrees
DIMS = {
    "grasp_dx": (0.010, 0.04),
    "grasp_dy": (0.010, 0.04),
    "grasp_dz": (0.006, 0.02),
    "roll": (8.0, 35.0),
    "drop_dx": (0.015, 0.06),
    "drop_dy": (0.015, 0.06),
}


@dataclass
class SteeringPolicy:
    mean: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in DIMS})
    std: dict[str, float] = field(default_factory=lambda: {k: s for k, (s, _) in DIMS.items()})
    episodes: int = 0
    successes: int = 0

    @classmethod
    def load(cls, path: Path = POLICY_FILE) -> SteeringPolicy:
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path = POLICY_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=1))

    def sample(self, rng: np.random.Generator) -> dict[str, float]:
        return {
            k: float(np.clip(self.mean[k] + self.std[k] * rng.standard_normal(), -b, b)) for k, (_, b) in DIMS.items()
        }

    def update(self, theta: dict[str, float], success: bool) -> None:
        """Learns from successes only: a success pulls the mean toward what worked and narrows the search around it.
        Failures are counted (and kept in the episode log) but never move the policy."""
        self.episodes += 1
        if not success:
            return
        self.successes += 1
        for k, (s0, b) in DIMS.items():
            d = theta[k] - self.mean[k]
            self.mean[k] = float(np.clip(self.mean[k] + 0.6 * d, -b, b))
            self.std[k] = max(0.25 * s0, self.std[k] * 0.8)

    def summary(self) -> str:
        def fmt(k: str, v: float) -> str:
            return f"{k}={v:+.0f}deg" if k == "roll" else f"{k}={v * 100:+.1f}cm"

        return "  ".join(fmt(k, self.mean[k]) for k in DIMS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help='e.g. "pick up the gum and put it on the plate"')
    ap.add_argument("--height", type=float, default=0.05, help="object height in metres (standing gum pack: 0.05)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    cam = CameraModel.load()
    policy = SteeringPolicy.load()
    rng = np.random.default_rng(args.seed)
    keys: queue.Queue[str] = queue.Queue()

    def read_keys() -> None:
        for line in sys.stdin:
            keys.put(line.strip().lower())
        keys.put("q")

    threading.Thread(target=read_keys, daemon=True).start()
    robot = connect_robot(cfg)
    set_limits_from_calibration(kin, robot)
    _, rest_grip = read_pose(robot)
    rest = dict(cfg.home_deg)  # raised pose: the wrist camera sees the whole workspace
    go_home(robot, rest, rest_grip)
    print(f"steering policy: {policy.episodes} episodes, {policy.successes} successes | {policy.summary()}")
    try:
        while True:
            print("\nReset the scene, then press Enter to run (q to quit).", flush=True)
            k = keys.get()
            if k == "q":
                break
            if k in ("s", "f"):
                print("(no run in progress - press Enter to start one)")
                continue
            theta = policy.sample(rng)
            frame = grab_frame(cfg)
            p = plan(cfg, kin, cam, args.command, rest, args.height, frame=frame.copy(), steer=theta)
            print(describe(p), flush=True)
            if p.error:
                print("not running this episode (fix the scene and press Enter)")
                continue
            ep = DATA_DIR / time.strftime("%Y%m%d-%H%M%S")
            ep.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(ep / "scene.jpg"), frame)
            label: str | None = None
            print("running... type s or f + Enter any time", flush=True)
            t0 = time.time()
            grip_after_close = None
            traj: list[dict] = []
            try:
                for w in p.waypoints:
                    traj += execute(
                        robot, kin, type(p)(**{**p.__dict__, "waypoints": [w]}), log=lambda m: print(m, flush=True)
                    )
                    if w.name == "close":
                        grip_after_close = read_pose(robot)[1]
                    while not keys.empty():
                        kk = keys.get()
                        if kk in ("s", "f"):
                            label = kk
                        elif kk == "q":
                            raise KeyboardInterrupt
            except GripLost as e:  # empty gripper: an automatic failure, no need to ask
                print(f"{e} - counting this run as a failure", flush=True)
                label = "f"
            finally:
                go_home(robot, rest, GRIP_OPEN)  # reset pose
            try:
                cv2.imwrite(str(ep / "after.jpg"), grab_frame(cfg))
            except RuntimeError:
                pass
            while label is None:
                print("success or failure? (s/f + Enter)", flush=True)
                kk = keys.get()
                if kk in ("s", "f"):
                    label = kk
                elif kk == "q":
                    label = "f"
            success = label == "s"
            policy.update(theta, success)
            policy.save()
            record = {
                "command": args.command,
                "success": success,
                "steer": theta,
                "policy_mean_after": policy.mean,
                "policy_std_after": policy.std,
                "target_xy": p.target_xy,
                "dest_xy": p.dest_xy,
                "roll": p.roll,
                "jaw_yaw_deg": p.jaw_yaw_deg,
                "gripper_after_close": grip_after_close,  # ~5 = closed on nothing, >8 = holding something
                "duration_s": round(time.time() - t0, 1),
                "waypoints": [{"name": w.name, "xyz": w.xyz, "gripper": w.gripper} for w in p.waypoints],
                "trajectory": traj,  # joint targets actually commanded: replayable (pick_routine --replay)
            }
            (ep / "episode.json").write_text(json.dumps(record, indent=1, default=float))
            if success:  # every success becomes a replayable recipe
                save_recipe(f"success_{ep.name}", p, traj, rest)
            rate = policy.successes / max(1, policy.episodes)
            print(
                f"saved {ep.name}: {'SUCCESS' if success else 'failure'} | {policy.successes}/{policy.episodes} "
                f"({rate:.0%}) | steering now {policy.summary()}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        go_home(robot, rest, GRIP_OPEN)
        robot.disconnect()
        print("arm released")


if __name__ == "__main__":
    main()
