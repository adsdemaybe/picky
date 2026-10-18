"""Human-in-the-loop RL for the SO-101 pick & place: press Enter to run, type s / f to label, it learns.

Q-learning over the camera-guided pick routine. Each episode makes two decisions, each a small tweak to the base
routine (the version that succeeded): a grasp tweak (shift / depth / wrist roll), then a drop tweak (shift over the
plate). The only reward is your label at the end (s = 1, f = 0). TD updates:

    Q_drop[a_drop]   += alpha * (r - Q_drop[a_drop])                          (terminal)
    Q_grasp[a_grasp] += alpha * (gamma * Q_drop[a_drop] - Q_grasp[a_grasp])   (bootstraps from the next decision)

Actions are chosen epsilon-greedily, so it mostly runs the best-known routine and sometimes tries a tweak. Every
episode is saved (frames, plan, actions, joint trajectory, label) and every success becomes a replayable recipe.

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

from . import pick_place
from .calibrate_camera import set_limits_from_calibration
from .camera import grab_frame
from .config import RigConfig
from .kinematics import SO101Kinematics
from .pick_place import Interrupted, connect_robot, read_pose
from .pick_routine import GRIP_OPEN, describe, execute, go_home, plan, save_recipe
from .scene import CameraModel

DATA_DIR = Path.home() / "picky_runs"
POLICY_FILE = DATA_DIR / "q_policy.json"

STEER_KEYS = ("grasp_dx", "grasp_dy", "grasp_dz", "roll", "drop_dx", "drop_dy")  # metres, roll in degrees
GRASP_ACTIONS: dict[str, dict[str, float]] = {
    "base": {},
    "x+1cm": {"grasp_dx": 0.01},
    "x-1cm": {"grasp_dx": -0.01},
    "y+1cm": {"grasp_dy": 0.01},
    "y-1cm": {"grasp_dy": -0.01},
    "deeper": {"grasp_dz": -0.008},
    "higher": {"grasp_dz": 0.008},
    "roll+15": {"roll": 15.0},
    "roll-15": {"roll": -15.0},
}
DROP_ACTIONS: dict[str, dict[str, float]] = {
    "base": {},
    "x+2cm": {"drop_dx": 0.02},
    "x-2cm": {"drop_dx": -0.02},
    "y+2cm": {"drop_dy": 0.02},
    "y-2cm": {"drop_dy": -0.02},
}
BASE_Q, TWEAK_Q = 0.6, 0.4  # the base routine has succeeded before; tweaks start a bit lower


def _init_q(actions: dict) -> dict[str, float]:
    return {k: BASE_Q if k == "base" else TWEAK_Q for k in actions}


@dataclass
class QPolicy:
    q_grasp: dict[str, float] = field(default_factory=lambda: _init_q(GRASP_ACTIONS))
    q_drop: dict[str, float] = field(default_factory=lambda: _init_q(DROP_ACTIONS))
    alpha: float = 0.3
    gamma: float = 0.95
    epsilon: float = 0.2
    episodes: int = 0
    successes: int = 0

    @classmethod
    def load(cls, path: Path = POLICY_FILE) -> QPolicy:
        return cls(**json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path = POLICY_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=1))

    def greedy(self) -> dict[str, str]:
        return {"grasp": max(self.q_grasp, key=self.q_grasp.get), "drop": max(self.q_drop, key=self.q_drop.get)}

    def act(self, rng: np.random.Generator) -> dict[str, str]:
        """Epsilon-greedy per decision."""
        a = self.greedy()
        for k, table in (("grasp", self.q_grasp), ("drop", self.q_drop)):
            if rng.random() < self.epsilon:
                a[k] = str(rng.choice(list(table)))
        return a

    @staticmethod
    def steer(actions: dict[str, str]) -> dict[str, float]:
        s = dict.fromkeys(STEER_KEYS, 0.0)
        s.update(GRASP_ACTIONS[actions["grasp"]])
        s.update(DROP_ACTIONS[actions["drop"]])
        return s

    def update(self, actions: dict[str, str], success: bool) -> None:
        """One TD(0) backup per decision, last decision first so the grasp bootstraps from the updated drop value."""
        r = 1.0 if success else 0.0
        self.episodes += 1
        self.successes += int(success)
        d, g = actions["drop"], actions["grasp"]
        self.q_drop[d] += self.alpha * (r - self.q_drop[d])
        self.q_grasp[g] += self.alpha * (self.gamma * self.q_drop[d] - self.q_grasp[g])
        self.epsilon = max(0.05, self.epsilon * 0.95)  # explore less as it learns

    def summary(self) -> str:
        a = self.greedy()
        return (
            f"best grasp={a['grasp']} (Q {self.q_grasp[a['grasp']]:.2f}), "
            f"best drop={a['drop']} (Q {self.q_drop[a['drop']]:.2f}), epsilon {self.epsilon:.2f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", help='e.g. "pick up the gum and put it on the plate"')
    ap.add_argument("--height", type=float, default=0.05, help="object height in metres (standing gum pack: 0.05)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--record", action="store_true", help="film every episode (overhead camera) to its folder")
    args = ap.parse_args()

    cfg = RigConfig()
    kin = SO101Kinematics(cfg)
    cam = CameraModel.load()
    policy = QPolicy.load()
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
    print(f"Q policy: {policy.episodes} episodes, {policy.successes} successes | {policy.summary()}")
    try:
        while True:
            print("\nReset the scene, then press Enter to run (q to quit).", flush=True)
            k = keys.get()
            if k == "q":
                break
            if k in ("s", "f"):
                print("(no run in progress - press Enter to start one)")
                continue
            actions = policy.act(rng)
            theta = policy.steer(actions)
            ep = DATA_DIR / time.strftime("%Y%m%d-%H%M%S")
            ep.mkdir(parents=True, exist_ok=True)
            rec = None
            if args.record:
                from .camera import start_recording

                rec = start_recording(cfg, str(ep / "video.mp4"), title=f'Picky  |  "{args.command}"')
                rec.caption = "looking at the table"
            print(f"actions: grasp={actions['grasp']}  drop={actions['drop']}", flush=True)
            frame = grab_frame(cfg)
            p = plan(cfg, kin, cam, args.command, rest, args.height, frame=frame.copy(), steer=theta)
            print(describe(p), flush=True)
            if p.error:
                print("not running this episode (fix the scene and press Enter)")
                if rec:
                    rec.stop()
                continue
            cv2.imwrite(str(ep / "scene.jpg"), frame)
            label: str | None = None
            quit_after = False

            def labelled() -> bool:  # polled during every move: s / f / q stops the run right away
                nonlocal label, quit_after
                while not keys.empty():
                    kk = keys.get()
                    if kk in ("s", "f"):
                        label = kk
                    elif kk == "q":
                        quit_after = True
                return label is not None or quit_after

            print("running... type s or f + Enter any time to label it, stop and reset", flush=True)
            t0 = time.time()
            grip_after_close = None
            traj: list[dict] = []
            pick_place.ABORT_CHECK = labelled
            try:
                for w in p.waypoints:
                    if rec:
                        rec.caption = w.name
                    traj += execute(
                        robot, kin, type(p)(**{**p.__dict__, "waypoints": [w]}), log=lambda m: print(m, flush=True)
                    )
                    if w.name == "close":
                        grip_after_close = read_pose(robot)[1]
                    if labelled():
                        raise Interrupted
            except Interrupted:
                if label:
                    print(
                        f"\n>>> marked as {'SUCCESS' if label == 's' else 'FAILURE'} - stopping and resetting",
                        flush=True,
                    )
                else:
                    print("\n>>> stopping", flush=True)
            finally:
                pick_place.ABORT_CHECK = None
                if rec:
                    rec.caption = "returning home"
                go_home(robot, rest, GRIP_OPEN)  # reset pose
            try:
                cv2.imwrite(str(ep / "after.jpg"), grab_frame(cfg))
            except RuntimeError:
                pass
            if rec:
                rec.caption = "done"
                time.sleep(1.5)  # hold the final shot
                print(f"video: {rec.stop()}  ({rec.frames} frames)", flush=True)
            if quit_after and label is None:
                label = "f"  # quit mid-run without a label: log it as a failure, then exit
            while label is None:
                print("success or failure? (s/f + Enter)", flush=True)
                kk = keys.get()
                if kk in ("s", "f"):
                    label = kk
                elif kk == "q":
                    label = "f"
            success = label == "s"
            policy.update(actions, success)
            policy.save()
            record = {
                "command": args.command,
                "success": success,
                "actions": actions,
                "steer": theta,
                "q_grasp_after": policy.q_grasp,
                "q_drop_after": policy.q_drop,
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
                f"({rate:.0%}) | {policy.summary()}",
                flush=True,
            )
            if quit_after:
                break
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        from . import camera

        if camera._ACTIVE is not None:  # quit mid-episode: close the video so it stays playable
            print(f"video: {camera._ACTIVE.stop()}")
        go_home(robot, rest, GRIP_OPEN)
        robot.disconnect()
        print("arm released")


if __name__ == "__main__":
    main()
