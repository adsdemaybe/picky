"""Closed-loop pick & place: plan one phase at a time and let Perceptron Mk1.5 check each phase before the next.

    look -> plan grasp -> GRASP (hover, straddle, close, lift) -> check: holding it?
         -> look at the destination again, re-plan the carry -> CARRY (transit, over destination)
         -> check: still holding?
         -> PLACE (lower, release, retreat) -> home -> check: on the destination?

A confident "no" at any check (asked twice, on two frames, before acting on it) opens the gripper, returns home and
starts a new attempt from a fresh camera frame, so a missed grasp or a drop mid-carry is recovered instead of
finishing the run blind. "Unsure" never aborts. Each check is also a reward for the Q-learning: the grasp decision is
scored by whether the object stayed in the gripper, the drop decision by whether it ended up on the destination.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .camera import grab_frame
from .judge import Verdict, ask
from .pick_routine import (
    GRIP_OPEN,
    ScenePlan,
    describe,
    detect_named,
    execute,
    go_home,
    plan,
    plan_drop,
)

GRASP = ("hover over target", "straddle", "close", "lift straight up", "lift high")
CARRY = ("transit", "over destination")
PLACE = ("lower", "release", "retreat")
NO_CONFIDENCE = 0.7  # act on a "no" only at or above this confidence, and only if a second look agrees


@dataclass
class Attempt:
    actions: dict[str, str]
    plan: ScenePlan
    scene: np.ndarray
    traj: list[dict] = field(default_factory=list)
    held: bool | None = None  # object stayed in the gripper through lift and carry (False: missed or dropped)
    placed: bool | None = None  # object ended up on the destination
    after: np.ndarray | None = None
    note: str = ""
    checks: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "actions": self.actions,
            "held": self.held,
            "placed": self.placed,
            "note": self.note,
            "checks": self.checks,
            "target_xy": self.plan.target_xy,
            "dest_xy": self.plan.dest_xy,
            "roll": self.plan.roll,
            "waypoints": [{"name": w.name, "xyz": w.xyz, "gripper": w.gripper} for w in self.plan.waypoints],
            "trajectory": self.traj,
        }


def _only(p: ScenePlan, names: tuple[str, ...]) -> ScenePlan:
    return ScenePlan(**{**p.__dict__, "waypoints": [w for w in p.waypoints if w.name in names]})


def check(cfg, what: str, p: ScenePlan, a: Attempt, log, caption) -> tuple[bool | None, np.ndarray | None]:
    """Ask Mk1.5; a "no" only counts if it is confident and a second frame (more reasoning) agrees."""
    q = {"holding": f"is the {p.target} in the gripper?", "placed": f"is the {p.target} on the {p.dest}?"}[what]
    caption(f"checking: {q}")
    try:
        frame = grab_frame(cfg)
    except RuntimeError as e:
        log(f"  check '{what}': no camera frame ({e}) - carrying on")
        return None, None
    v: Verdict = ask(frame, what, p.target, p.dest, cfg.perceptron_model)
    log(f"  check {q} -> {v}")
    if v.answer is False and v.confidence >= NO_CONFIDENCE:
        frame2 = grab_frame(cfg)
        v2 = ask(frame2, what, p.target, p.dest, cfg.perceptron_model, effort="medium")
        log(f"  second look -> {v2}")
        if v2.answer is False:
            a.checks.append(f"{what}: no ({v.reason} | {v2.reason})")
            return False, frame2
        v = v2
    a.checks.append(f"{what}: {v}")
    return (True if v.answer else None), frame


def run(
    robot,
    kin,
    cam,
    cfg,
    command: str,
    home: dict[str, float],
    height: float,
    choose,
    attempts: list[Attempt],
    learn=None,
    log=print,
    caption=lambda s: None,
    max_attempts: int = 3,
    step: bool = False,
) -> None:
    """Runs up to `max_attempts` attempts, appending each to `attempts` (a list the caller owns, so it keeps them if
    the run is interrupted). `choose()` -> (actions, steer) per attempt; `learn(attempt)` is called on each failed
    attempt that is retried, so the next attempt already uses the updated Q-values. The last attempt is left for the
    caller to score (it may have a human label)."""
    for n in range(1, max_attempts + 1):
        actions, steer = choose()
        caption("looking at the table" + (f" (attempt {n})" if n > 1 else ""))
        scene = grab_frame(cfg)
        p = plan(cfg, kin, cam, command, home, height, frame=scene.copy(), steer=steer)
        log(f"attempt {n}: grasp={actions.get('grasp')} drop={actions.get('drop')}\n{describe(p)}")
        a = Attempt(actions, p, scene)
        attempts.append(a)
        if p.error:
            a.note = p.error
            if n > 1 and "not found" in p.error:  # after a retry: maybe it already landed on the destination
                a.placed, a.after = check(cfg, "placed", p, a, log, caption)
            return

        _phase(robot, kin, cfg, a, GRASP, step, log, caption)
        held, _ = check(cfg, "holding", p, a, log, caption)
        if held is False:
            a.held, a.note = False, "missed the grasp"
        else:
            _replan_carry(cfg, kin, cam, a, home, log)
            _phase(robot, kin, cfg, a, CARRY, step, log, caption)
            held, _ = check(cfg, "holding", p, a, log, caption)
            if held is False:
                a.held, a.note = False, "dropped it during the carry"
            else:
                a.held = True if held else None
                _phase(robot, kin, cfg, a, PLACE, step, log, caption)
                caption("returning home")
                go_home(robot, home, GRIP_OPEN)
                time.sleep(0.5)
                a.placed, a.after = check(cfg, "placed", p, a, log, caption)
                if a.placed is not False:
                    return  # done (placed, or the camera can't tell: leave it to the label)
                a.note = f"the {p.target} is not on the {p.dest}"
        if n == max_attempts:
            log(f">>> {a.note} - out of attempts")
            caption(f"{a.note} - giving up")
            go_home(robot, home, GRIP_OPEN)
            return
        log(f">>> {a.note} - going home and trying again")
        caption(f"{a.note} - trying again")
        if learn:
            learn(a)
        go_home(robot, home, GRIP_OPEN)
        time.sleep(1.0)  # let the object settle before the next look


def _phase(robot, kin, cfg, a: Attempt, names: tuple[str, ...], step, log, caption) -> None:
    a.traj += execute(robot, kin, _only(a.plan, names), step=step, cfg=cfg, log=log, on_step=caption)


def _replan_carry(cfg, kin, cam, a: Attempt, home, log) -> None:
    """With the object in hand, look at the destination again and re-plan the carry if it moved."""
    p = a.plan
    try:
        frame = grab_frame(cfg)
    except RuntimeError:
        return
    d = detect_named(frame, p.dest, cfg.perceptron_model)
    if d is None:
        log(f"  {p.dest} not visible right now - keeping the original carry plan")
        return
    old = p.dest_xy
    drop = plan_drop(kin, cam, p, d.box_px, (frame.shape[1], frame.shape[0]), home)
    if isinstance(drop, str):
        p.dest_xy = old
        log(f"  re-plan failed ({drop}) - keeping the original carry plan")
        return
    moved = float(np.hypot(p.dest_xy[0] - old[0], p.dest_xy[1] - old[1]))
    p.waypoints = [w for w in p.waypoints if w.name in GRASP] + drop
    log(f"  re-planned the carry from a fresh look: drop point moved {moved * 100:.1f} cm")
