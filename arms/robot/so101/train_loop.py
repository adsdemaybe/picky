"""Human-in-the-loop RL for the SO-101 pick & place: press Enter to run, type s / f to label, it learns.

Q-learning over the closed-loop pick routine (closed_loop.py). Each attempt makes two decisions, each a small tweak to
the base routine: a grasp tweak (shift / depth / wrist roll), then a drop tweak (shift over the plate). Perceptron
Mk1.5 checks each phase and those checks are the step rewards; your s / f label, when you give one, overrides the
final one:

    missed / dropped:  Q_grasp[g] += alpha * (0 - Q_grasp[g])                                  (terminal)
    held, then placed: Q_drop[d]  += alpha * (r - Q_drop[d])           r = 1 on the plate, else 0
                       Q_grasp[g] += alpha * (gamma * max_d' Q_drop[d'] - Q_grasp[g])      (Q-learning backup)

A failed attempt is learned from immediately and retried (up to 3 per episode) with the updated values. Actions are
epsilon-greedy. Every episode is logged; successes also keep their video, frames and a replayable recipe.

    python -m arms.robot.so101.train_loop "pick up the gum and put it on the plate" [--record]

Keys (type, then Enter):  Enter = run an episode   s = success   f = failure   q = quit
s / f mid-run stops the arm right away, records the label and resets; otherwise the camera's verdict is suggested.
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

from . import closed_loop, pick_place
from .calibrate_camera import set_limits_from_calibration
from .config import RUNS_DIR, RigConfig
from .kinematics import SO101Kinematics
from .pick_place import Interrupted, connect_robot, read_pose
from .pick_routine import GRIP_OPEN, go_home, save_recipe
from .scene import CameraModel

DATA_DIR = RUNS_DIR
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

    def learn(self, actions: dict[str, str], held: bool | None, placed: bool | None) -> None:
        """TD(0) backups for one attempt from its step rewards.
        held False (missed / dropped): the grasp decision ends the attempt with reward 0.
        Otherwise the drop decision gets r = placed; the grasp bootstraps from the drop values: the best one when the
        hold was confirmed (Q-learning), the one actually taken when it wasn't (credit can't be split)."""
        g, d = actions["grasp"], actions["drop"]
        if held is False:
            self.q_grasp[g] += self.alpha * (0.0 - self.q_grasp[g])
        elif placed is not None:
            self.q_drop[d] += self.alpha * (float(placed) - self.q_drop[d])
            nxt = max(self.q_drop.values()) if held else self.q_drop[d]
            self.q_grasp[g] += self.alpha * (self.gamma * nxt - self.q_grasp[g])
        self.epsilon = max(0.05, self.epsilon * 0.95)  # explore less as it learns

    def finish(self, success: bool) -> None:
        self.episodes += 1
        self.successes += int(success)

    def update(self, actions: dict[str, str], success: bool) -> None:
        """End-of-episode label only (no step checks)."""
        self.learn(actions, None, success)
        self.finish(success)

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
    ap.add_argument("--attempts", type=int, default=3, help="retries per episode after a missed/dropped grasp")
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
            ep = DATA_DIR / time.strftime("%Y%m%d-%H%M%S")
            ep.mkdir(parents=True, exist_ok=True)
            rec = None
            if args.record:
                from .camera import start_recording

                rec = start_recording(cfg, str(ep / "video.mp4"), title=f'Picky  |  "{args.command}"')
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

            def choose():
                a = policy.act(rng)
                return a, policy.steer(a)

            def learn(att) -> None:  # a failed attempt that will be retried: learn before the retry
                policy.learn(att.actions, att.held, att.placed)
                print(f"  learned from the failed attempt | {policy.summary()}", flush=True)

            print("running... type s or f + Enter any time to label it, stop and reset", flush=True)
            t0 = time.time()
            attempts: list = []
            pick_place.ABORT_CHECK = labelled
            try:
                closed_loop.run(
                    robot,
                    kin,
                    cam,
                    cfg,
                    args.command,
                    rest,
                    args.height,
                    choose,
                    attempts,
                    learn=learn,
                    log=lambda m: print(m, flush=True),
                    caption=(lambda c, r=rec: setattr(r, "caption", c)) if rec else (lambda c: None),
                    max_attempts=args.attempts,
                )
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
            last = attempts[-1] if attempts else None
            if last is None or (last.plan.error and len(attempts) == 1 and label is None):
                print(f"not running this episode: {last.note if last else 'no plan'} (fix the scene, press Enter)")
                if rec:
                    rec.stop()
                for f in ("video.mp4",):
                    (ep / f).unlink(missing_ok=True)
                if not any(ep.iterdir()):
                    ep.rmdir()
                if quit_after:
                    break
                continue
            if rec:
                rec.caption = {True: f"done: the {last.plan.target} is on the {last.plan.dest}", False: "failed"}.get(
                    last.placed, "done"
                )
                time.sleep(1.5)  # hold the final shot
                rec.stop()
            if quit_after and label is None:
                label = "f"  # quit mid-run without a label: log it as a failure, then exit
            if label is None:
                hint = {True: "camera says SUCCESS", False: "camera says FAILURE", None: "camera can't tell"}
                default = {True: "s", False: "f", None: None}[last.placed]
                print(f"{hint[last.placed]} - Enter to accept, or type s / f", flush=True)
                while label is None:
                    kk = keys.get()
                    if kk in ("s", "f"):
                        label = kk
                    elif kk == "" and default:
                        label = default
                    elif kk == "q":
                        label, quit_after = (default or "f"), True
                    else:
                        print("type s or f + Enter", flush=True)
            success = label == "s"
            # the final attempt: your label is the final reward; the camera's "held" check stays the grasp reward
            held = last.held if not success else (last.held if last.held is not None else True)
            policy.learn(last.actions, held, success if held is not False else None)
            policy.finish(success)
            policy.save()
            record = {
                "command": args.command,
                "success": success,
                "label_source": "human",
                "camera_verdict": last.placed,
                "q_grasp_after": policy.q_grasp,
                "q_drop_after": policy.q_drop,
                "duration_s": round(time.time() - t0, 1),
                "attempts": [a.summary() for a in attempts],
            }
            (ep / "episode.json").write_text(json.dumps(record, indent=1, default=float))
            if success:  # every success becomes a replayable recipe, with its first view and final view
                cv2.imwrite(str(ep / "scene.jpg"), last.scene)
                if last.after is not None:
                    cv2.imwrite(str(ep / "after.jpg"), last.after)
                save_recipe(f"success_{ep.name}", last.plan, last.traj, rest)
            else:  # failures keep only episode.json (what the Q-learning used): no video or frames
                (ep / "video.mp4").unlink(missing_ok=True)
            rate = policy.successes / max(1, policy.episodes)
            print(
                f"saved {ep.name}: {'SUCCESS' if success else 'failure'} after {len(attempts)} attempt(s) | "
                f"{policy.successes}/{policy.episodes} ({rate:.0%}) | {policy.summary()}",
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
